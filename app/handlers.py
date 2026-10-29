from __future__ import annotations

import time
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, TelegramObject

from app.arbitrage import Arbitrage, best_deal
from app.autobuy import AutoBuyer
from app.scanner import Scanner
from app.formatting import format_price_check, ton
from app.storage import MODES, Storage

router = Router()

HELP = (
    "Я слежу за NFT-подарками на {markets}.\n\n"
    "<b>Проверить подарок</b> — просто напиши название:\n"
    "<code>Plush Pepe</code> — вся коллекция\n"
    "<code>Plush Pepe | Cozy Galaxy</code> — конкретная модель\n"
    "<code>Toy Bear фон Onyx Black</code> — коллекция на конкретном фоне\n"
    "<code>Toy Bear | Wizard | Onyx Black</code> — модель на фоне\n\n"
    "<b>Автопоиск</b> — проверяю каждый новый лот:\n"
    "• <b>по фону</b>: лот дешевле флора того же фона (например, Toy Bear на Onyx Black "
    "за 52 TON при флоре 57 — это −9%)\n"
    "• <b>по модели</b>: лот можно перепродать дороже по флору той же модели\n\n"
    "/mode фон | модель | оба — что искать\n"
    "/discount 10 — на сколько % ниже флора фона\n"
    "/minprofit 2 — мин. профит в TON (по модели)\n"
    "/minpct 7 — мин. профит в % (по модели)\n"
    "/maxbuy 100 — не показывать лоты дороже (0 = без лимита)\n"
    "/autobuy — автопокупка лотов ниже флора фона (тест, лимиты, бюджет)\n"
    "/alerts — включить/выключить уведомления\n"
    "/settings — текущие настройки\n"
    "/status — состояние бирж\n\n"
    "Цена покупки — с комиссией биржи, «на руки» — после комиссии при продаже."
)

def help_text(engine: Arbitrage) -> str:
    return HELP.format(markets=", ".join(f"<b>{m.title}</b>" for m in engine.markets.values()))


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
async def start(message: Message, storage: Storage, engine: Arbitrage) -> None:
    storage.get(message.from_user.id)
    await message.answer(help_text(engine))


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


AUTOBUY_NUMBERS = {
    # команда: (поле, мин, макс, целое?)
    "max": ("max_price", 0, 1_000_000, False),
    "budget": ("daily_budget", 0, 10_000_000, False),
    "discount": ("min_discount", 1, 90, False),
    "others": ("min_others", 0, 50, True),
    "profit": ("min_profit", 0, 1_000_000, False),
}


@router.message(Command("autobuy"))
async def autobuy_cmd(
    message: Message, command: CommandObject, autobuyer: AutoBuyer | None, owners: list[int]
) -> None:
    if autobuyer is None:
        await message.answer("Автопокупка работает только с Portals — добавь его в MARKETS в .env")
        return
    if message.from_user.id not in owners:
        await message.answer(
            "Автопокупкой управляет только владелец: впиши свой id в ALLOWED_USERS в .env.\n"
            f"Твой id: <code>{message.from_user.id}</code>"
        )
        return
    s = autobuyer.settings
    args = (command.args or "").split()
    if not args:
        try:
            balance = await autobuyer.market.balance()
        except Exception:
            balance = None
        await message.answer(autobuyer.status_text(balance), disable_web_page_preview=True)
        return

    cmd = args[0].lower()
    if cmd in ("off", "выкл", "stop", "стоп"):
        s.enabled = False
        autobuyer.save()
        await message.answer("⏸ Автопокупка выключена")
    elif cmd in ("test", "тест"):
        s.enabled, s.test = True, True
        autobuyer.save()
        await message.answer(
            "🧪 Тестовый режим: ничего не покупаю, только пишу, что купил бы. "
            "Когда убедишься, что находки нормальные — /autobuy on"
        )
    elif cmd in ("on", "вкл", "start"):
        problem = autobuyer.ready_for_real()
        if problem:
            await message.answer(problem)
            return
        s.enabled, s.test = True, False
        autobuyer.reset_failures()
        autobuyer.save()
        await message.answer(
            f"🟢 Автопокупка включена: покупаю лоты дешевле флора фона на {s.min_discount:g}%+, "
            f"не дороже {s.max_price:g} TON, не больше {s.daily_budget:g} TON за сутки. Выключить: /autobuy off"
        )
    elif cmd in ("fast", "быстро"):
        value = (args[1] if len(args) > 1 else "").lower()
        if value not in ("on", "off", "вкл", "выкл"):
            await message.answer("Укажи: <code>/autobuy fast on</code> или <code>/autobuy fast off</code>")
            return
        s.fast = value in ("on", "вкл")
        autobuyer.save()
        await message.answer(
            "⚡️ Быстрый режим включён: покупаю сразу, флор беру из памяти"
            if s.fast else "🐢 Быстрый режим выключен: перед покупкой перепроверяю флор (+1 запрос)"
        )
    elif cmd in AUTOBUY_NUMBERS:
        field, lo, hi, whole = AUTOBUY_NUMBERS[cmd]
        try:
            value = float((args[1] if len(args) > 1 else "").replace(",", "."))
            if whole:
                value = int(value)
            if not lo <= value <= hi:
                raise ValueError
        except ValueError:
            await message.answer(f"Укажи число от {lo} до {hi}, например: <code>/autobuy {cmd} 10</code>")
            return
        setattr(s, field, value)
        autobuyer.save()
        await message.answer("✅ Сохранено")
    else:
        await message.answer(autobuyer.status_text(None), disable_web_page_preview=True)


@router.message(Command("status"))
async def status(message: Message, engine: Arbitrage, scanner: Scanner | None = None) -> None:
    lines = []
    for m in engine.markets.values():
        if m.last_error:
            lines.append(f"⚠️ <b>{m.title}</b>: {escape(m.last_error)}")
        elif m.last_ok:
            lines.append(f"✅ <b>{m.title}</b>: ок ({time.time() - m.last_ok:.0f}с назад)")
        else:
            lines.append(f"⏳ <b>{m.title}</b>: ещё не опрашивалась")
        speed = []
        if m.rtt_ms is not None:
            speed.append(f"ответ биржи ~{m.rtt_ms:.0f} мс")
        if scanner and m.name in scanner.delays:
            speed.append(f"опрос раз в {scanner.delays[m.name]:.2f} с")
        if scanner and m.name in scanner.lag:
            speed.append(f"вижу лоты через ~{max(0.0, scanner.lag[m.name]):.2f} с после выставления")
        feed = getattr(m, "feed", None)
        if feed:
            speed.append("лента: новые лоты + снижения цены" if feed == "activity" else "лента: только новые лоты")
        if speed:
            lines.append("    " + "\n    ".join(speed))
    lines.append(f"\nИзвестно коллекций: {len(engine.collections)}, фонов: {len(engine.backdrops)}")
    await message.answer("\n".join(lines))


@router.message(F.text & ~F.text.startswith("/"))
async def price_check(message: Message, engine: Arbitrage) -> None:
    key = engine.resolve(message.text)
    if key is None:
        await message.answer(help_text(engine))
        return
    wait = await message.answer(f"🔎 Смотрю {escape(str(key))}…")
    # По запросу пользователя всегда берём свежие данные
    quotes = await engine.quotes(key, max_age=0)
    op = best_deal(key, quotes, basis="backdrop" if key.backdrop else "model")
    await wait.edit_text(
        format_price_check(key, op, quotes, engine.markets), disable_web_page_preview=True
    )
