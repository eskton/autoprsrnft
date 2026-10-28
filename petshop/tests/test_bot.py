from types import SimpleNamespace

from shop.bot import BotNotifier, order_action


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


ORDER = {
    "id": 7, "user_id": 5, "title": "Roblox · 400 <Robux>", "details": "nick&co", "price": 50000,
    "bonus_used": 20000, "crypto_amount": 30000, "status": "paid", "admin_note": None,
}


async def test_order_paid_goes_to_buyer_and_admins_escaped():
    bot = FakeBot()
    await BotNotifier(bot, [10, 11], "https://shop.example/").order_paid(ORDER, {"username": "bob"})
    assert [chat for chat, *_ in bot.sent] == [5, 10, 11]
    text, kwargs = bot.sent[1][1], bot.sent[1][2]
    assert "&lt;Robux&gt;" in text and "nick&amp;co" in text  # пользовательский текст не ломает HTML
    assert "200 ₽ бонусами + 300 ₽ криптой" in text and "@bob" in text
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["ord:done:7", "ord:rej:7"]


async def test_order_finished_messages():
    bot = FakeBot()
    n = BotNotifier(bot, [], "https://shop.example/")
    await n.order_finished({**ORDER, "status": "done"})
    await n.order_finished({**ORDER, "status": "rejected", "admin_note": "нет <в> наличии"})
    assert "выполнен" in bot.sent[0][1]
    assert "нет &lt;в&gt; наличии" in bot.sent[1][1] and "500 ₽ вернулись" in bot.sent[1][1]


class FakeCall:
    def __init__(self, user_id, data):
        self.from_user = SimpleNamespace(id=user_id, username="admin", first_name="Admin")
        self.data = data
        self.answers = []
        self.edited = None
        self.message = SimpleNamespace(html_text="🧾 Заказ", edit_text=self._edit)

    async def _edit(self, text, reply_markup=None):
        self.edited = text

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


async def test_admin_buttons_in_chat(env):
    res = await env.shop.create_order(1, env.robux["id"], "nick", use_bonus=False)
    env.cp.pay(max(env.cp.invoices))
    await env.shop.on_invoice_paid(max(env.cp.invoices))
    order_id = res["order"]["id"]

    stranger = FakeCall(999, f"ord:done:{order_id}")
    await order_action(stranger, env.shop, admin_ids=[42])
    assert stranger.answers == ["Только для админа"] and stranger.edited is None

    admin = FakeCall(42, f"ord:done:{order_id}")
    await order_action(admin, env.shop, admin_ids=[42])
    assert "Выполнен" in admin.edited

    again = FakeCall(42, f"ord:rej:{order_id}")
    await order_action(again, env.shop, admin_ids=[42])
    assert "уже обработан" in again.answers[0]
