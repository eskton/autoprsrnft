import pytest

from shop.cryptopay import CryptoPayError
from shop.db import Database
from shop.service import DAY, Shop


class FakeCryptoPay:
    """Имитация CryptoBot: счета живут в памяти, тест сам решает, оплачен ли счёт."""

    def __init__(self):
        self.invoices: dict[int, dict] = {}
        self.next_id = 1000
        self.fail_with: Exception | None = None

    async def create_invoice(self, amount_rub, description, payload, expires_in, return_url=None):
        if self.fail_with:
            raise self.fail_with
        self.next_id += 1
        inv = {
            "invoice_id": self.next_id,
            "status": "active",
            "amount": amount_rub,
            "payload": payload,
            "description": description,
            "mini_app_invoice_url": f"https://t.me/CryptoBot/app?startapp=invoice-{self.next_id}",
        }
        self.invoices[self.next_id] = inv
        return inv

    async def get_invoices(self, invoice_ids):
        return [self.invoices[i] for i in invoice_ids if i in self.invoices]

    def pay(self, cp_id):
        self.invoices[cp_id]["status"] = "paid"

    def expire(self, cp_id):
        self.invoices[cp_id]["status"] = "expired"

    async def close(self):
        pass


class Recorder:
    """Notifier, который просто запоминает уведомления."""

    def __init__(self):
        self.events = []

    async def pet_bought(self, user_id, pet):
        self.events.append(("pet_bought", user_id, pet["id"]))

    async def order_paid(self, order, user):
        self.events.append(("order_paid", order["id"]))

    async def order_finished(self, order):
        self.events.append(("order_finished", order["id"], order["status"]))

    async def bonus_adjusted(self, user_id, amount, note):
        self.events.append(("bonus_adjusted", user_id, amount))


class Clock:
    def __init__(self, t=1_700_000_000):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
async def env():
    db = Database(":memory:")
    await db.connect()
    cp = FakeCryptoPay()
    rec = Recorder()
    clock = Clock()
    shop = Shop(db, cp, rec, invoice_ttl=3600, max_active_invoices=3, clock=clock)
    # Питомец из примера: 1000 ₽ -> 1200 ₽ бонусами за 30 дней
    dog = await shop.save_pet_type(
        {"name": "Пёс", "emoji": "🐶", "price": 100000, "total_return": 120000, "duration_days": 30}
    )
    robux = await shop.save_product(
        {"game": "Roblox", "title": "400 Robux", "price": 50000, "input_label": "Ник в Roblox"}
    )
    await shop.touch_user(1, "alice", "Alice")
    yield type("Env", (), dict(db=db, cp=cp, rec=rec, clock=clock, shop=shop, dog=dog, robux=robux, DAY=DAY))
    await db.close()


__all__ = ["CryptoPayError"]
