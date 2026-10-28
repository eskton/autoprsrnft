"""Бизнес-логика магазина.

Правила:
- питомца покупают только за крипту (CryptoBot), бонусами — нельзя;
- питомец равномерно приносит total_return бонусов за duration_days;
- бонусы нельзя вывести или передать, их можно потратить только на товары (донат);
- заказ можно оплатить бонусами, криптой или частично тем и другим.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Protocol

from shop.cryptopay import CryptoPay, CryptoPayError
from shop.db import Database, Tx
from shop.money import rub_str

log = logging.getLogger(__name__)

DAY = 86400
MAX_RUB = 10_000_000 * 100  # защита от опечаток в админке


class ShopError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


class Notifier(Protocol):
    async def pet_bought(self, user_id: int, pet: dict) -> None: ...
    async def order_paid(self, order: dict, user: dict | None) -> None: ...
    async def order_finished(self, order: dict) -> None: ...
    async def bonus_adjusted(self, user_id: int, amount: int, note: str) -> None: ...


class NullNotifier:
    async def pet_bought(self, user_id: int, pet: dict) -> None: ...
    async def order_paid(self, order: dict, user: dict | None) -> None: ...
    async def order_finished(self, order: dict) -> None: ...
    async def bonus_adjusted(self, user_id: int, amount: int, note: str) -> None: ...


def accrued(pet: dict, now: int) -> int:
    """Сколько бонусов питомец принёс к моменту now (равномерно за весь срок)."""
    if now >= pet["ends_at"]:
        return pet["total_return"]
    if now <= pet["started_at"]:
        return 0
    return pet["total_return"] * (now - pet["started_at"]) // (pet["ends_at"] - pet["started_at"])


def pet_view(pet: dict, now: int) -> dict:
    earned = accrued(pet, now)
    return {
        **pet,
        "accrued": earned,
        "claimable": earned - pet["claimed"],
        "finished": now >= pet["ends_at"],
    }


def _text(value: Any, field: str, max_len: int, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ShopError(f"Заполни поле «{field}»")
    if len(text) > max_len:
        raise ShopError(f"Поле «{field}» длиннее {max_len} символов")
    return text


def _kop(value: Any, field: str, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ShopError(f"Неверная сумма в поле «{field}»")
    if value < 0 or (value == 0 and not allow_zero) or value > MAX_RUB:
        raise ShopError(f"Неверная сумма в поле «{field}»")
    return value


def _int(value: Any, field: str, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ShopError(f"Поле «{field}» должно быть числом") from None
    if not lo <= number <= hi:
        raise ShopError(f"Поле «{field}»: от {lo} до {hi}")
    return number


class Shop:
    def __init__(
        self,
        db: Database,
        cryptopay: CryptoPay,
        notifier: Notifier | None = None,
        *,
        invoice_ttl: int = 3600,
        max_active_invoices: int = 5,
        return_url: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db
        self.cp = cryptopay
        self.notifier: Notifier = notifier or NullNotifier()
        self.invoice_ttl = invoice_ttl
        self.max_active_invoices = max_active_invoices
        self.return_url = return_url
        self.clock = clock

    def now(self) -> int:
        return int(self.clock())

    # --- пользователи и баланс ---------------------------------------------

    async def touch_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        async with self.db.tx() as t:
            await t.run(
                "INSERT INTO users(id, username, first_name, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
                user_id, username, first_name, self.now(),
            )

    @staticmethod
    async def _balance(t: Tx, user_id: int) -> int:
        return await t.val("SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE user_id=?", user_id)

    async def balance(self, user_id: int) -> int:
        async with self.db.read() as t:
            return await self._balance(t, user_id)

    async def _ledger(self, t: Tx, user_id: int, amount: int, kind: str, ref_id: int | None, note: str) -> None:
        await t.run(
            "INSERT INTO ledger(user_id, amount, kind, ref_id, note, created_at) VALUES (?,?,?,?,?,?)",
            user_id, amount, kind, ref_id, note, self.now(),
        )

    async def profile(self, user_id: int) -> dict:
        async with self.db.read() as t:
            balance = await self._balance(t, user_id)
            ledger = await t.all(
                "SELECT id, amount, kind, note, created_at FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 50",
                user_id,
            )
            orders = await t.all(
                "SELECT o.id, o.title, o.price, o.bonus_used, o.crypto_amount, o.details, o.status, "
                "o.admin_note, o.created_at, "
                "(SELECT pay_url FROM invoices i WHERE i.kind='order' AND i.ref_id=o.id AND i.status='active') "
                "AS pay_url FROM orders o WHERE o.user_id=? AND o.status != 'cancelled' "
                "ORDER BY o.id DESC LIMIT 50",
                user_id,
            )
            invoices = await t.all(
                "SELECT id, kind, title, amount, pay_url, created_at FROM invoices "
                "WHERE user_id=? AND status='active' ORDER BY id DESC",
                user_id,
            )
            income = await t.val(
                "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE user_id=? AND kind='pet_income'", user_id
            )
            pets = await t.val("SELECT COUNT(*) FROM pets WHERE user_id=?", user_id)
        return {
            "balance": balance,
            "ledger": ledger,
            "orders": orders,
            "invoices": invoices,
            "stats": {"pets": pets, "income": income},
        }

    # --- питомцы ------------------------------------------------------------

    async def pet_catalog(self) -> list[dict]:
        async with self.db.read() as t:
            return await t.all(
                "SELECT id, name, emoji, description, price, total_return, duration_days "
                "FROM pet_types WHERE active=1 ORDER BY sort, price"
            )

    async def my_pets(self, user_id: int) -> list[dict]:
        now = self.now()
        async with self.db.read() as t:
            pets = await t.all("SELECT * FROM pets WHERE user_id=? ORDER BY id DESC", user_id)
        return [pet_view(p, now) for p in pets]

    async def buy_pet(self, user_id: int, type_id: int) -> dict:
        async with self.db.tx() as t:
            pt = await t.one("SELECT * FROM pet_types WHERE id=? AND active=1", type_id)
            if not pt:
                raise ShopError("Этого питомца больше нет в продаже")
            await self._check_invoice_limit(t, user_id)
            title = f"{pt['emoji']} {pt['name']}"
            meta = json.dumps({k: pt[k] for k in ("name", "emoji", "total_return", "duration_days")})
            inv_id = await t.run(
                "INSERT INTO invoices(user_id, kind, ref_id, amount, title, meta, status, created_at) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                user_id, "pet", pt["id"], pt["price"], title, meta, self.now(),
            )
        return await self._create_remote(inv_id, f"Питомец {title}", pt["price"])

    async def claim(self, user_id: int) -> dict:
        """Собирает накопленное со всех питомцев в бонусный баланс."""
        now = self.now()
        async with self.db.tx() as t:
            pets = await t.all("SELECT * FROM pets WHERE user_id=? AND claimed < total_return", user_id)
            total = 0
            for pet in pets:
                delta = accrued(pet, now) - pet["claimed"]
                if delta > 0:
                    await t.run("UPDATE pets SET claimed = claimed + ? WHERE id=?", delta, pet["id"])
                    total += delta
            if total:
                await self._ledger(t, user_id, total, "pet_income", None, "Собрано с питомцев")
            balance = await self._balance(t, user_id)
        return {"claimed": total, "balance": balance}

    # --- магазин ------------------------------------------------------------

    async def products(self) -> list[dict]:
        async with self.db.read() as t:
            return await t.all(
                "SELECT id, game, title, description, price, input_label FROM products "
                "WHERE active=1 ORDER BY game, sort, price"
            )

    async def create_order(self, user_id: int, product_id: int, details: str, use_bonus: bool) -> dict:
        details = str(details or "").strip()
        if len(details) > 300:
            raise ShopError("Слишком длинный текст — максимум 300 символов")
        now = self.now()
        async with self.db.tx() as t:
            p = await t.one("SELECT * FROM products WHERE id=? AND active=1", product_id)
            if not p:
                raise ShopError("Этот товар больше не продаётся")
            if not details:
                raise ShopError(f"Укажи: {p['input_label']}")
            bonus = max(0, min(await self._balance(t, user_id), p["price"])) if use_bonus else 0
            crypto = p["price"] - bonus
            if crypto:
                await self._check_invoice_limit(t, user_id)
            title = f"{p['game']} · {p['title']}"
            order_id = await t.run(
                "INSERT INTO orders(user_id, product_id, title, price, bonus_used, crypto_amount, details, "
                "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                user_id, p["id"], title, p["price"], bonus, crypto, details,
                "awaiting_payment" if crypto else "paid", now, now,
            )
            if bonus:
                await self._ledger(t, user_id, -bonus, "order", order_id, f"Заказ #{order_id}: {title}")
            inv_id = None
            if crypto:
                inv_id = await t.run(
                    "INSERT INTO invoices(user_id, kind, ref_id, amount, title, status, created_at) "
                    "VALUES (?,?,?,?,?,'pending',?)",
                    user_id, "order", order_id, crypto, f"Заказ #{order_id}: {title}", now,
                )
            order = await t.one("SELECT * FROM orders WHERE id=?", order_id)
        if inv_id is None:
            await self._notify_order_paid(order_id)
            return {"order": order, "invoice": None}
        invoice = await self._create_remote(inv_id, f"Заказ #{order_id}: {title}", crypto)
        return {"order": order, "invoice": invoice}

    async def invoice_status(self, user_id: int, invoice_id: int) -> dict:
        async with self.db.read() as t:
            inv = await t.one(
                "SELECT id, kind, status, result_id FROM invoices WHERE id=? AND user_id=?", invoice_id, user_id
            )
        if not inv:
            raise ShopError("Счёт не найден")
        return inv

    # --- оплата -------------------------------------------------------------

    async def _check_invoice_limit(self, t: Tx, user_id: int) -> None:
        n = await t.val(
            "SELECT COUNT(*) FROM invoices WHERE user_id=? AND status IN ('pending','active')", user_id
        )
        if n >= self.max_active_invoices:
            raise ShopError(
                f"У тебя уже {n} неоплаченных счетов. Оплати их (Профиль → Ждут оплаты) или подожди, пока истекут."
            )

    async def _create_remote(self, inv_id: int, description: str, amount: int) -> dict:
        try:
            remote = await self.cp.create_invoice(
                amount_rub=rub_str(amount),
                description=description,
                payload=str(inv_id),
                expires_in=self.invoice_ttl,
                return_url=self.return_url,
            )
        except CryptoPayError as e:
            log.warning("CryptoBot не создал счёт %s: %s", inv_id, e)
            await self._fail_invoice(inv_id)
            raise ShopError(f"CryptoBot не принял счёт ({e.name}). Попробуй позже или другую сумму.") from e
        except Exception as e:
            log.warning("CryptoBot недоступен (счёт %s): %s", inv_id, e)
            await self._fail_invoice(inv_id)
            raise ShopError("CryptoBot сейчас не отвечает, попробуй через минуту") from e
        url = CryptoPay.pay_url(remote)
        async with self.db.tx() as t:
            await t.change(
                "UPDATE invoices SET status='active', cp_invoice_id=?, pay_url=? WHERE id=? AND status='pending'",
                remote["invoice_id"], url, inv_id,
            )
        return {"id": inv_id, "pay_url": url, "amount": amount, "status": "active"}

    async def _cancel_order(self, t: Tx, order_id: int, reason: str) -> None:
        order = await t.one("SELECT * FROM orders WHERE id=?", order_id)
        if not order or order["status"] != "awaiting_payment":
            return
        await t.run("UPDATE orders SET status='cancelled', updated_at=? WHERE id=?", self.now(), order_id)
        if order["bonus_used"]:
            await self._ledger(t, order["user_id"], order["bonus_used"], "refund", order_id,
                               f"Заказ #{order_id} {reason}, бонусы возвращены")

    async def _fail_invoice(self, inv_id: int) -> None:
        async with self.db.tx() as t:
            inv = await t.one("SELECT * FROM invoices WHERE id=?", inv_id)
            if not inv or inv["status"] != "pending":
                return
            await t.run("UPDATE invoices SET status='failed' WHERE id=?", inv_id)
            if inv["kind"] == "order":
                await self._cancel_order(t, inv["ref_id"], "отменён")

    async def active_invoice_ids(self) -> list[int]:
        async with self.db.read() as t:
            rows = await t.all("SELECT cp_invoice_id FROM invoices WHERE status='active'")
        return [r["cp_invoice_id"] for r in rows]

    async def cleanup_pending(self, older_than: int = 600) -> None:
        """Счета, которые так и не создались в CryptoBot (например, упал процесс)."""
        async with self.db.read() as t:
            rows = await t.all(
                "SELECT id FROM invoices WHERE status='pending' AND created_at < ?", self.now() - older_than
            )
        for row in rows:
            await self._fail_invoice(row["id"])

    async def on_invoice_paid(self, cp_invoice_id: int) -> None:
        now = self.now()
        pet = order_id = None
        async with self.db.tx() as t:
            inv = await t.one("SELECT * FROM invoices WHERE cp_invoice_id=?", cp_invoice_id)
            if not inv or inv["status"] != "active":
                return  # уже обработан или не наш
            if inv["kind"] == "pet":
                meta = json.loads(inv["meta"])
                pet_id = await t.run(
                    "INSERT INTO pets(user_id, type_id, name, emoji, price, total_return, started_at, ends_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    inv["user_id"], inv["ref_id"], meta["name"], meta["emoji"], inv["amount"],
                    meta["total_return"], now, now + meta["duration_days"] * DAY,
                )
                await t.run(
                    "UPDATE invoices SET status='paid', paid_at=?, result_id=? WHERE id=?", now, pet_id, inv["id"]
                )
                pet = await t.one("SELECT * FROM pets WHERE id=?", pet_id)
            else:
                await t.run("UPDATE invoices SET status='paid', paid_at=? WHERE id=?", now, inv["id"])
                changed = await t.change(
                    "UPDATE orders SET status='paid', updated_at=? WHERE id=? AND status='awaiting_payment'",
                    now, inv["ref_id"],
                )
                if not changed:
                    log.error("Оплачен счёт %s, но заказ #%s не ждал оплаты", inv["id"], inv["ref_id"])
                order_id = inv["ref_id"]
        if pet:
            log.info("Куплен питомец #%s пользователем %s", pet["id"], pet["user_id"])
            await self._safe_notify(self.notifier.pet_bought(pet["user_id"], pet_view(pet, now)))
        if order_id:
            await self._notify_order_paid(order_id)

    async def on_invoice_expired(self, cp_invoice_id: int) -> None:
        async with self.db.tx() as t:
            inv = await t.one("SELECT * FROM invoices WHERE cp_invoice_id=?", cp_invoice_id)
            if not inv or inv["status"] != "active":
                return
            await t.run("UPDATE invoices SET status='expired' WHERE id=?", inv["id"])
            if inv["kind"] == "order":
                await self._cancel_order(t, inv["ref_id"], "не оплачен вовремя")

    async def _notify_order_paid(self, order_id: int) -> None:
        async with self.db.read() as t:
            order = await t.one("SELECT * FROM orders WHERE id=?", order_id)
            user = await t.one("SELECT * FROM users WHERE id=?", order["user_id"])
        if order["status"] == "paid":
            await self._safe_notify(self.notifier.order_paid(order, user))

    @staticmethod
    async def _safe_notify(coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("Не удалось отправить уведомление")

    # --- админка ------------------------------------------------------------

    async def admin_stats(self) -> dict:
        async with self.db.read() as t:
            v = t.val
            return {
                "users": await v("SELECT COUNT(*) FROM users"),
                "pets_sold": await v("SELECT COUNT(*) FROM pets"),
                "pets_active": await v("SELECT COUNT(*) FROM pets WHERE claimed < total_return"),
                "crypto_pets": await v(
                    "SELECT COALESCE(SUM(amount),0) FROM invoices WHERE status='paid' AND kind='pet'"
                ),
                "crypto_orders": await v(
                    "SELECT COALESCE(SUM(amount),0) FROM invoices WHERE status='paid' AND kind='order'"
                ),
                "orders_waiting": await v("SELECT COUNT(*) FROM orders WHERE status='paid'"),
                "orders_done": await v("SELECT COUNT(*) FROM orders WHERE status='done'"),
                # Сколько бонусов ещё «висит»: их обязательно придётся отдать донатом
                "owed_pets": await v("SELECT COALESCE(SUM(total_return - claimed),0) FROM pets"),
                "owed_balances": await v("SELECT COALESCE(SUM(amount),0) FROM ledger"),
            }

    async def admin_orders(self, status: str = "paid") -> list[dict]:
        if status not in ("paid", "done", "rejected", "awaiting_payment", "cancelled"):
            raise ShopError("Неизвестный статус")
        async with self.db.read() as t:
            return await t.all(
                "SELECT o.*, u.username, u.first_name FROM orders o LEFT JOIN users u ON u.id = o.user_id "
                "WHERE o.status=? ORDER BY o.id DESC LIMIT 100",
                status,
            )

    async def complete_order(self, order_id: int) -> dict:
        async with self.db.tx() as t:
            order = await t.one("SELECT * FROM orders WHERE id=?", order_id)
            self._check_processable(order)
            await t.run("UPDATE orders SET status='done', updated_at=? WHERE id=?", self.now(), order_id)
            order = await t.one("SELECT * FROM orders WHERE id=?", order_id)
        await self._safe_notify(self.notifier.order_finished(order))
        return order

    async def reject_order(self, order_id: int, note: str = "") -> dict:
        """Отказ: вся сумма заказа возвращается на бонусный баланс."""
        note = _text(note, "Причина", 300, required=False)
        async with self.db.tx() as t:
            order = await t.one("SELECT * FROM orders WHERE id=?", order_id)
            self._check_processable(order)
            await t.run(
                "UPDATE orders SET status='rejected', admin_note=?, updated_at=? WHERE id=?",
                note or None, self.now(), order_id,
            )
            await self._ledger(t, order["user_id"], order["price"], "refund", order_id,
                               f"Возврат за заказ #{order_id}")
            order = await t.one("SELECT * FROM orders WHERE id=?", order_id)
        await self._safe_notify(self.notifier.order_finished(order))
        return order

    @staticmethod
    def _check_processable(order: dict | None) -> None:
        if not order:
            raise ShopError("Заказ не найден")
        if order["status"] in ("done", "rejected"):
            raise ShopError(f"Заказ #{order['id']} уже обработан")
        if order["status"] != "paid":
            raise ShopError(f"Заказ #{order['id']} ещё не оплачен")

    async def admin_products(self) -> list[dict]:
        async with self.db.read() as t:
            return await t.all("SELECT * FROM products ORDER BY active DESC, game, sort, price")

    async def save_product(self, data: dict, product_id: int | None = None) -> dict:
        row = {
            "game": _text(data.get("game"), "Игра", 64),
            "title": _text(data.get("title"), "Название", 128),
            "description": _text(data.get("description"), "Описание", 500, required=False),
            "price": _kop(data.get("price"), "Цена"),
            "input_label": _text(data.get("input_label") or "Ник или ID в игре", "Что спросить у покупателя", 64),
            "active": 1 if data.get("active", True) else 0,
            "sort": _int(data.get("sort", 0), "Порядок", -10_000, 10_000),
        }
        return await self._save("products", row, product_id)

    async def admin_pet_types(self) -> list[dict]:
        async with self.db.read() as t:
            return await t.all("SELECT * FROM pet_types ORDER BY active DESC, sort, price")

    async def save_pet_type(self, data: dict, type_id: int | None = None) -> dict:
        row = {
            "name": _text(data.get("name"), "Имя", 32),
            "emoji": _text(data.get("emoji") or "🐾", "Эмодзи", 16),
            "description": _text(data.get("description"), "Описание", 300, required=False),
            "price": _kop(data.get("price"), "Цена"),
            "total_return": _kop(data.get("total_return"), "Принесёт всего", allow_zero=True),
            "duration_days": _int(data.get("duration_days"), "Срок, дней", 1, 3650),
            "active": 1 if data.get("active", True) else 0,
            "sort": _int(data.get("sort", 0), "Порядок", -10_000, 10_000),
        }
        return await self._save("pet_types", row, type_id)

    async def _save(self, table: str, row: dict, row_id: int | None) -> dict:
        cols = list(row)
        async with self.db.tx() as t:
            if row_id is None:
                row_id = await t.run(
                    f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    *row.values(),
                )
            else:
                changed = await t.change(
                    f"UPDATE {table} SET {', '.join(c + '=?' for c in cols)} WHERE id=?", *row.values(), row_id
                )
                if not changed:
                    raise ShopError("Запись не найдена")
            return await t.one(f"SELECT * FROM {table} WHERE id=?", row_id)

    async def adjust_balance(self, user_id: int, amount: int, note: str) -> int:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount == 0 or abs(amount) > MAX_RUB:
            raise ShopError("Неверная сумма")
        note = _text(note, "Комментарий", 200, required=False) or "Корректировка администратором"
        async with self.db.tx() as t:
            if not await t.val("SELECT 1 FROM users WHERE id=?", user_id):
                raise ShopError("Такой пользователь ещё не открывал бота")
            balance = await self._balance(t, user_id)
            if balance + amount < 0:
                raise ShopError("Баланс не может уйти в минус")
            await self._ledger(t, user_id, amount, "admin", None, note)
            balance += amount
        await self._safe_notify(self.notifier.bonus_adjusted(user_id, amount, note))
        return balance
