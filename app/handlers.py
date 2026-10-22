from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, TelegramObject

from app.arbitrage import Arbitrage, best_deal
from app.formatting import format_price_check, ton
from app.storage import Storage

router = Router()

HELP = (
    "Я сравниваю цены NFT-подарков на <b>Portals</b>, <b>Tonnel</b> и <b>MRKT</b>.\n\n"
    "<b>Проверить подарок</b> — просто напиши название:\n"
    "<code>Plush Pepe</code> — вся коллекция\n"
    "<code>Plush Pepe | Cozy Galaxy</code> — конкретная модель\n\n"
    "<b>Автопоиск</b>: я слежу за новыми лотами на всех трёх биржах и пишу, "
    "когда что-то можно купить на одной и продать дороже на другой.\n\n"
    "/alerts — включить/выключить уведомления\n"
    "/settings — текущие пороги\n"
    "/minprofit 2 — мин. профит в TON\n"
    "/minpct 7 — мин. профит в %\n"
    "/maxbuy 100 — не показывать лоты дороже (0 = без лимита)\n"
    "/status — состояние бирж\n\n"
    "Цена покупки — с комиссией биржи, «на руки» — после комиссии при продаже."
)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed: list[int]):
        self.allowed = set(allowed)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if self.allowed and (user is None or user.id not in self.allowed):
            if isinstance(event, Message):
                await event.answer(f"Нет доступа. Твой id: <code>{user.id if user else '?'}</code>")
            return None
        return await handler(event, data)


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message, storage: Storage) -> None:
    storage.get(message.from_user.id)
    await message.answer(HELP)


@router.message(Command("alerts"))
async def toggle_alerts(message: Message, storage: Storage) -> None:
    s = storage.get(message.from_user.id)
    s.alerts = not s.alerts
    storage.save()
    await message.answer("🔔 Уведомления включены" if s.alerts else "🔕 Уведомления выключены")


@router.message(Command("settings"))
async def show_settings(message: Message, storage: Storage) -> None:
    s = storage.get(message.from_user.id)
    await message.answer(
        f"Уведомления: {'вкл' if s.alerts else 'выкл'}\n"
        f"Мин. профит: {ton(s.min_profit_ton)} TON и {s.min_profit_pct:g}%\n"
        f"Макс. цена покупки: {ton(s.max_buy_ton) + ' TON' if s.max_buy_ton else 'без лимита'}"
    )


async def _set_number(message: Message, command: CommandObject, storage: Storage, field: str) -> None:
    try:
        value = float((command.args or "").replace(",", "."))
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer(f"Укажи число, например: <code>/{command.command} 5</code>")
        return
    s = storage.get(message.from_user.id)
    setattr(s, field, value)
    storage.save()
    await message.answer("✅ Сохранено")


@router.message(Command("minprofit"))
async def set_min_profit(message: Message, command: CommandObject, storage: Storage) -> None:
    await _set_number(message, command, storage, "min_profit_ton")


@router.message(Command("minpct"))
async def set_min_pct(message: Message, command: CommandObject, storage: Storage) -> None:
    await _set_number(message, command, storage, "min_profit_pct")


@router.message(Command("maxbuy"))
async def set_max_buy(message: Message, command: CommandObject, storage: Storage) -> None:
    await _set_number(message, command, storage, "max_buy_ton")


@router.message(Command("status"))
async def status(message: Message, engine: Arbitrage) -> None:
    lines = []
    for m in engine.markets.values():
        if m.last_error:
            lines.append(f"⚠️ {m.title}: {m.last_error}")
        elif m.last_ok:
            lines.append(f"✅ {m.title}: ок ({time.time() - m.last_ok:.0f}с назад)")
        else:
            lines.append(f"⏳ {m.title}: ещё не опрашивалась")
    lines.append(f"\nИзвестно коллекций: {len(engine.collections)}")
    await message.answer("\n".join(lines))


@router.message(F.text & ~F.text.startswith("/"))
async def price_check(message: Message, engine: Arbitrage) -> None:
    key = engine.resolve(message.text)
    if key is None:
        await message.answer(HELP)
        return
    wait = await message.answer(f"🔎 Смотрю {key} на всех биржах…")
    # По запросу пользователя всегда берём свежие данные
    quotes = await engine.quotes(key, max_age=0)
    op = best_deal(key, quotes)
    await wait.edit_text(
        format_price_check(key, op, quotes, engine.markets), disable_web_page_preview=True
    )
