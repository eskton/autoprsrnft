from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,           -- Telegram user id
    username    TEXT,
    first_name  TEXT,
    created_at  INTEGER NOT NULL
);

-- Виды питомцев в магазине (настраиваются в админке)
CREATE TABLE IF NOT EXISTS pet_types (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    emoji         TEXT NOT NULL DEFAULT '🐾',
    description   TEXT NOT NULL DEFAULT '',
    price         INTEGER NOT NULL,            -- цена в копейках (платится криптой)
    total_return  INTEGER NOT NULL,            -- сколько бонусов принесёт за весь срок, коп.
    duration_days INTEGER NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    sort          INTEGER NOT NULL DEFAULT 0
);

-- Купленные питомцы. Условия копируются в момент покупки,
-- чтобы правки в админке не меняли уже купленное.
CREATE TABLE IF NOT EXISTS pets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    type_id       INTEGER NOT NULL REFERENCES pet_types(id),
    name          TEXT NOT NULL,
    emoji         TEXT NOT NULL,
    price         INTEGER NOT NULL,
    total_return  INTEGER NOT NULL,
    started_at    INTEGER NOT NULL,
    ends_at       INTEGER NOT NULL,
    claimed       INTEGER NOT NULL DEFAULT 0   -- сколько уже собрано в бонусный баланс
);
CREATE INDEX IF NOT EXISTS pets_user ON pets(user_id);

-- Товары магазина (донат)
CREATE TABLE IF NOT EXISTS products (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game         TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    price        INTEGER NOT NULL,
    input_label  TEXT NOT NULL DEFAULT 'Ник или ID в игре',
    active       INTEGER NOT NULL DEFAULT 1,
    sort         INTEGER NOT NULL DEFAULT 0
);

-- Заказы доната. status: awaiting_payment -> paid -> done | rejected; awaiting_payment -> cancelled
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    product_id     INTEGER NOT NULL REFERENCES products(id),
    title          TEXT NOT NULL,
    price          INTEGER NOT NULL,
    bonus_used     INTEGER NOT NULL,
    crypto_amount  INTEGER NOT NULL,
    details        TEXT NOT NULL,
    status         TEXT NOT NULL,
    admin_note     TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);

-- Счета Crypto Pay. kind: pet (ref_id = pet_types.id) | order (ref_id = orders.id)
-- status: pending (ещё не создан в CryptoBot) -> active -> paid | expired; pending -> failed
CREATE TABLE IF NOT EXISTS invoices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    kind           TEXT NOT NULL,
    ref_id         INTEGER NOT NULL,
    amount         INTEGER NOT NULL,
    title          TEXT NOT NULL,
    meta           TEXT,                       -- условия питомца на момент выставления счёта (JSON)
    cp_invoice_id  INTEGER UNIQUE,
    pay_url        TEXT,
    status         TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    paid_at        INTEGER,
    result_id      INTEGER                     -- id купленного питомца для kind=pet
);
CREATE INDEX IF NOT EXISTS invoices_status ON invoices(status);

-- Движение бонусного баланса. Баланс = сумма amount по пользователю.
-- kind: pet_income | order | refund | admin
CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    amount      INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    ref_id      INTEGER,
    note        TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id);
"""


class Database:
    """Одно соединение SQLite. Все обращения идут по очереди через lock,
    поэтому проверка баланса и списание никогда не перемешаются."""

    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[Tx]:
        """Транзакция на запись: всё внутри либо применяется целиком, либо откатывается."""
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield Tx(self.conn)
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            await self.conn.execute("COMMIT")

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Tx]:
        async with self._lock:
            yield Tx(self.conn)


class Tx:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def one(self, sql: str, *args: Any) -> dict | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def all(self, sql: str, *args: Any) -> list[dict]:
        async with self.conn.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def val(self, sql: str, *args: Any) -> Any:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def run(self, sql: str, *args: Any) -> int:
        """Выполняет запрос, возвращает lastrowid."""
        async with self.conn.execute(sql, args) as cur:
            return cur.lastrowid

    async def change(self, sql: str, *args: Any) -> int:
        """Выполняет UPDATE/DELETE, возвращает число изменённых строк."""
        async with self.conn.execute(sql, args) as cur:
            return cur.rowcount
