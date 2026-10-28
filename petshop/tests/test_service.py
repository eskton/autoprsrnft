import pytest

from shop.cryptopay import CryptoPayError
from shop.money import fmt_rub, parse_rub, rub_str
from shop.payments import PaymentPoller
from shop.service import ShopError, accrued


def test_money():
    assert parse_rub("1 200,50") == 120050
    assert parse_rub("1000") == 100000
    assert parse_rub(0.1) == 10
    assert rub_str(120050) == "1200.50"
    assert fmt_rub(120000) == "1 200 ₽"
    assert fmt_rub(4005) == "40,05 ₽"
    with pytest.raises(ValueError):
        parse_rub("abc")
    with pytest.raises(ValueError):
        parse_rub("nan")


def test_accrual_is_linear_and_capped():
    pet = {"started_at": 0, "ends_at": 30 * 86400, "total_return": 120000}
    assert accrued(pet, -5) == 0
    assert accrued(pet, 86400) == 4000  # 40 ₽ в день
    assert accrued(pet, 15 * 86400) == 60000
    assert accrued(pet, 31 * 86400) == 120000


async def buy_and_pay_pet(e, user_id=1):
    inv = await e.shop.buy_pet(user_id, e.dog["id"])
    cp_id = max(e.cp.invoices)
    e.cp.pay(cp_id)
    await PaymentPoller(e.shop, e.cp).tick()
    return inv


async def test_pet_purchase_accrual_and_claim(env):
    e = env
    inv = await e.shop.buy_pet(1, e.dog["id"])
    assert inv["pay_url"].startswith("https://t.me/")
    assert e.cp.invoices[max(e.cp.invoices)]["amount"] == "1000.00"
    assert await e.shop.my_pets(1) == []  # пока не оплачен — питомца нет

    e.cp.pay(max(e.cp.invoices))
    poller = PaymentPoller(e.shop, e.cp)
    await poller.tick()
    await poller.tick()  # повторная обработка не создаёт второго питомца
    pets = await e.shop.my_pets(1)
    assert len(pets) == 1 and pets[0]["emoji"] == "🐶"
    assert ("pet_bought", 1, pets[0]["id"]) in e.rec.events
    assert (await e.shop.invoice_status(1, inv["id"]))["status"] == "paid"

    e.clock.advance(e.DAY)
    assert (await e.shop.claim(1)) == {"claimed": 4000, "balance": 4000}
    assert (await e.shop.claim(1))["claimed"] == 0  # повторно собрать нечего

    e.clock.advance(100 * e.DAY)  # после окончания срока питомец отдаёт ровно 1200 ₽
    assert (await e.shop.claim(1))["balance"] == 120000
    assert (await e.shop.my_pets(1))[0]["finished"]


async def test_pet_terms_are_snapshotted(env):
    e = env
    await e.shop.buy_pet(1, e.dog["id"])
    # Админ поменял условия, пока счёт не оплачен — у покупателя остаются старые
    await e.shop.save_pet_type(
        {"name": "Пёс", "emoji": "🐶", "price": 100000, "total_return": 999999, "duration_days": 1}, e.dog["id"]
    )
    e.cp.pay(max(e.cp.invoices))
    await PaymentPoller(e.shop, e.cp).tick()
    pet = (await e.shop.my_pets(1))[0]
    assert pet["total_return"] == 120000 and pet["ends_at"] - pet["started_at"] == 30 * e.DAY


async def test_order_paid_fully_with_bonus(env):
    e = env
    await buy_and_pay_pet(e)
    e.clock.advance(30 * e.DAY)
    await e.shop.claim(1)  # 1200 ₽ бонусов
    res = await e.shop.create_order(1, e.robux["id"], "alice_rbx", use_bonus=True)
    assert res["invoice"] is None and res["order"]["status"] == "paid"
    assert res["order"]["bonus_used"] == 50000 and res["order"]["crypto_amount"] == 0
    assert await e.shop.balance(1) == 70000
    assert ("order_paid", res["order"]["id"]) in e.rec.events

    done = await e.shop.complete_order(res["order"]["id"])
    assert done["status"] == "done"
    with pytest.raises(ShopError):
        await e.shop.complete_order(res["order"]["id"])


async def test_order_mixed_payment_and_expiry_refund(env):
    e = env
    await e.shop.adjust_balance(1, 20000, "подарок")  # 200 ₽ бонусов
    res = await e.shop.create_order(1, e.robux["id"], "alice_rbx", use_bonus=True)
    assert res["order"]["bonus_used"] == 20000 and res["order"]["crypto_amount"] == 30000
    assert e.cp.invoices[max(e.cp.invoices)]["amount"] == "300.00"
    assert await e.shop.balance(1) == 0
    assert res["order"]["status"] == "awaiting_payment"

    e.cp.expire(max(e.cp.invoices))
    await PaymentPoller(e.shop, e.cp).tick()
    assert await e.shop.balance(1) == 20000  # бонусы вернулись
    profile = await e.shop.profile(1)
    assert profile["orders"] == []  # отменённые заказы не показываем


async def test_order_paid_with_crypto_then_rejected(env):
    e = env
    res = await e.shop.create_order(1, e.robux["id"], "alice_rbx", use_bonus=True)
    assert res["order"]["crypto_amount"] == 50000
    e.cp.pay(max(e.cp.invoices))
    await PaymentPoller(e.shop, e.cp).tick()
    orders = await e.shop.admin_orders("paid")
    assert [o["id"] for o in orders] == [res["order"]["id"]] and orders[0]["username"] == "alice"

    rejected = await e.shop.reject_order(res["order"]["id"], "нет в наличии")
    assert rejected["status"] == "rejected" and rejected["admin_note"] == "нет в наличии"
    assert await e.shop.balance(1) == 50000  # вся сумма вернулась бонусами
    assert ("order_finished", res["order"]["id"], "rejected") in e.rec.events


async def test_cryptobot_failure_refunds_bonus(env):
    e = env
    await e.shop.adjust_balance(1, 10000, "")
    e.cp.fail_with = CryptoPayError("AMOUNT_TOO_SMALL", 400)
    with pytest.raises(ShopError, match="AMOUNT_TOO_SMALL"):
        await e.shop.create_order(1, e.robux["id"], "nick", use_bonus=True)
    assert await e.shop.balance(1) == 10000
    async with e.db.read() as t:
        assert await t.val("SELECT status FROM orders") == "cancelled"
        assert await t.val("SELECT status FROM invoices") == "failed"


async def test_limits_and_validation(env):
    e = env
    for _ in range(3):
        await e.shop.buy_pet(1, e.dog["id"])
    with pytest.raises(ShopError, match="неоплаченных"):
        await e.shop.buy_pet(1, e.dog["id"])
    with pytest.raises(ShopError, match="Ник в Roblox"):
        await e.shop.create_order(1, e.robux["id"], "  ", use_bonus=False)
    with pytest.raises(ShopError, match="минус"):
        await e.shop.adjust_balance(1, -1, "")
    with pytest.raises(ShopError):
        await e.shop.save_product({"game": "X", "title": "Y", "price": -5})
    with pytest.raises(ShopError):
        await e.shop.save_pet_type({"name": "Z", "price": 100, "total_return": 100, "duration_days": 0})
    await e.shop.save_product({**e.robux, "active": False}, e.robux["id"])
    assert await e.shop.products() == []
    with pytest.raises(ShopError, match="не продаётся"):
        await e.shop.create_order(1, e.robux["id"], "nick", use_bonus=False)


async def test_admin_stats_show_what_is_owed(env):
    e = env
    await buy_and_pay_pet(e)
    e.clock.advance(15 * e.DAY)
    await e.shop.claim(1)
    stats = await e.shop.admin_stats()
    assert stats["crypto_pets"] == 100000
    assert stats["owed_pets"] == 60000 and stats["owed_balances"] == 60000
    assert stats["pets_active"] == 1 and stats["users"] == 1
