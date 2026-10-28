from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from shop.money import fmt_rub
from shop.service import Shop, ShopError

log = logging.getLogger(__name__)

router = Router()


def open_app_kb(url: str, text: str = "🐾 Открыть магазин") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]])


def order_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"ord:done:{order_id}"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"ord:rej:{order_id}"),
    ]])


def order_text(order: dict, user: dict | None = None) -> str:
    lines = [f"🧾 <b>Заказ #{order['id']}</b>: {escape(order['title'])}"]
    if user is not None:
        who = f"@{user['username']}" if user.get("username") else escape(user.get("first_name") or "")
        lines.append(f"👤 {who} (id <code>{order['user_id']}</code>)")
    lines.append(f"📝 {escape(order['details'])}")
    pay = [f"{fmt_rub(order['bonus_used'])} бонусами"] if order["bonus_used"] else []
    if order["crypto_amount"]:
        pay.append(f"{fmt_rub(order['crypto_amount'])} криптой")
    lines.append(f"💰 {fmt_rub(order['price'])} ({' + '.join(pay)})")
    return "\n".join(lines)


class BotNotifier:
    def __init__(self, bot: Bot, admin_ids: list[int], webapp_url: str):
        self.bot = bot
        self.admin_ids = admin_ids
        self.webapp_url = webapp_url

    async def _send(self, chat_id: int, text: str, **kwargs) -> None:
        try:
            await self.bot.send_message(chat_id, text, **kwargs)
        except Exception as e:  # пользователь мог заблокировать бота
            log.warning("Не удалось написать %s: %s", chat_id, e)

    async def pet_bought(self, user_id: int, pet: dict) -> None:
        days = (pet["ends_at"] - pet["started_at"]) // 86400
        per_day = pet["total_return"] // max(days, 1)
        await self._send(
            user_id,
            f"{pet['emoji']} <b>{escape(pet['name'])}</b> теперь твой!\n\n"
            f"Он принесёт <b>{fmt_rub(pet['total_return'])}</b> бонусами за {days} дн. "
            f"(≈ {fmt_rub(per_day)} в день). Заходи собирать 🐾",
            reply_markup=open_app_kb(self.webapp_url, "🐾 К питомцам"),
        )

    async def order_paid(self, order: dict, user: dict | None) -> None:
        await self._send(
            order["user_id"],
            f"✅ Заказ #{order['id']} оплачен: {escape(order['title'])}.\nСкоро выполним и напишем сюда.",
        )
        for admin_id in self.admin_ids:
            await self._send(admin_id, order_text(order, user), reply_markup=order_kb(order["id"]))

    async def order_finished(self, order: dict) -> None:
        if order["status"] == "done":
            text = f"🎉 Заказ #{order['id']} выполнен: {escape(order['title'])}. Приятной игры!"
        else:
            reason = f"\nПричина: {escape(order['admin_note'])}" if order.get("admin_note") else ""
            text = (
                f"😔 Заказ #{order['id']} отклонён.{reason}\n"
                f"{fmt_rub(order['price'])} вернулись на бонусный баланс — можно выбрать другой товар."
            )
        await self._send(order["user_id"], text)

    async def bonus_adjusted(self, user_id: int, amount: int, note: str) -> None:
        sign = "+" if amount > 0 else "−"
        await self._send(user_id, f"💎 Бонусный баланс: {sign}{fmt_rub(abs(amount))}\n{escape(note)}")


@router.message(CommandStart())
async def start(message: Message, webapp_url: str) -> None:
    await message.answer(
        "Привет! 🐾\n\n"
        "Здесь можно завести питомца — он каждый день приносит бонусы, "
        "а бонусами можно оплатить донат в играх.\n\n"
        "Жми кнопку, чтобы открыть магазин 👇",
        reply_markup=open_app_kb(webapp_url),
    )


@router.callback_query(F.data.startswith("ord:"))
async def order_action(call: CallbackQuery, shop: Shop, admin_ids: list[int]) -> None:
    if call.from_user.id not in admin_ids:
        await call.answer("Только для админа", show_alert=True)
        return
    _, action, raw_id = call.data.split(":")
    try:
        if action == "done":
            order = await shop.complete_order(int(raw_id))
        else:
            # Причину отказа можно указать в админке мини-аппа; из чата — без причины
            order = await shop.reject_order(int(raw_id))
    except ShopError as e:
        await call.answer(str(e), show_alert=True)
        return
    status = "✅ Выполнен" if order["status"] == "done" else "❌ Отклонён, сумма возвращена бонусами"
    who = call.from_user.username or call.from_user.first_name
    await call.message.edit_text(
        f"{call.message.html_text}\n\n<b>{status}</b> ({escape(who)})", reply_markup=None
    )
    await call.answer()


async def setup_menu_button(bot: Bot, webapp_url: str) -> None:
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=webapp_url)))
