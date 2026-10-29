from __future__ import annotations

import time
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, TelegramObject

from app.arbitrage import Arbitrage, best_deal
from app.formatting import format_price_check, ton
from app.storage import MODES, Storage

router = Router()

HELP = (
    "Я сравниваю цены NFT-подарков на <b>Portals</b>, <b>Tonnel</b> и <b>MRKT</b>.\n\n"
    "<b>Проверить подарок</b> — просто напиши название:\n"
    "<code>Plush Pepe</code> — вся коллекция\n"
    "<code>Plush Pepe | Cozy Galaxy</code> — конкретная модель\n"
    "<code>Toy Bear фон Onyx Black</code> — коллекция на конкретном фоне\n"
    "<code>Toy Bear | Wizard | Onyx Black</code> — модель на фоне\n\n"
    "<b>Автопоиск</b> — слежу за новыми лотами на всех трёх биржах:\n"
    "• <b>по фону</b>: лот дешевле флора того же фона (например, Toy Bear на Onyx Black "
    "за 52 TON при флоре 57 — это −9%)\n"
    "• <b>по модели</b>: можно купить на одной бирже и продать дороже на другой\n\n"
    "/mode фон | модель | оба — что искать\n"
    "/discount 10 — на сколько % ниже флора фона\n"
    "/minprofit 2 — мин. профит в TON (по модели)\n"
    "/minpct 7 — мин. профит в % (по модели)\n"
    "/maxbuy 100 — не показывать лоты дороже (0 = без лимита)\n"
    "/alerts — включить/выключить уведомления\n"
    "/settings — текущие настройки\n"
    "/status — состояние бирж\n\n"
    "Цена покупки — с комиссией биржи, «на руки» — после комиссии при продаже."
)

MODE_ALIASES = {
    "фон": "backdrop", "фоны": "backdrop", "backdrop": "backdrop", "bg": "backdrop",
    "модель": "model", "модели": "model", "model": "model",
    "оба": "both", "все": "both", "both": "both",
}


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
        f"Ищу: {MODES[s.mode]}\n"
        f"По фону: лот дешевле флора фона на {s.min_discount_pct:g}%+\n"
        f"По модели: профит от {ton(s.min_profit_ton)} TON и {s.min_profit_pct:g}%\n"
        f"Макс. цена покупки: {ton(s.max_buy_ton) + ' TON' if s.max_buy_ton else 'без лимита'}"
    )


@router.message(Command("mode"))
async def set_mode(message: Message, command: CommandObject, storage: Storage) -> None:
    mode = MODE_ALIASES.get((command.args or "").strip().lower())
    s = storage.get(message.from_user.id)
    if mode is None:
        await message.answer(
            f"Сейчас ищу: <b>{MODES[s.mode]}</b>\n\n"
            "<code>/mode фон</code> — лоты дешевле флора того же фона\n"
            "<code>/mode модель</code> — перепродажа по флору модели на другой бирже\n"
            "<code>/mode оба</code> — и то и другое"
        )
        return
    s.mode = mode
    storage.save()
    await message.answer(f"✅ Теперь ищу {MODES[mode]}")


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


@router.message(Command("discount"))
async def set_min_discount(message: Message, command: CommandObject, storage: Storage) -> None:
    await _set_number(message, command, storage, "min_discount_pct")


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
    lines.append(f"\nИзвестно коллекций: {len(engine.collections)}, фонов: {len(engine.backdrops)}")
    await message.answer("\n".join(lines))


@router.message(F.text & ~F.text.startswith("/"))
async def price_check(message: Message, engine: Arbitrage) -> None:
    key = engine.resolve(message.text)
    if key is None:
        await message.answer(HELP)
        return
    wait = await message.answer(f"🔎 Смотрю {escape(str(key))} на всех биржах…")
    # По запросу пользователя всегда берём свежие данные
    quotes = await engine.quotes(key, max_age=0)
    op = best_deal(key, quotes, basis="backdrop" if key.backdrop else "model")
    await wait.edit_text(
        format_price_check(key, op, quotes, engine.markets), disable_web_page_preview=True
    )
