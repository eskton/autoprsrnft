import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from shop.payments import PaymentPoller
from shop.web import create_app

TOKEN = "123456:TEST-TOKEN"
ADMIN = 1
WEBAPP = Path(__file__).resolve().parent.parent / "webapp"


def init_data(user_id: int, token: str = TOKEN, auth_date: int | None = None) -> str:
    """initData в том виде, в каком его подписывает Telegram."""
    params = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": user_id, "first_name": f"User{user_id}", "username": f"user{user_id}"},
                           separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)


def auth(user_id: int, **kw) -> dict:
    return {"Authorization": "tma " + init_data(user_id, **kw)}


@pytest.fixture
async def client(env):
    # Подпись initData проверяется по реальным часам, поэтому и магазин живёт по ним
    env.clock.t = int(time.time())
    app = create_app(env.shop, bot_token=TOKEN, admin_ids=[ADMIN], static_dir=WEBAPP)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_index_and_static(client):
    r = await client.get("/")
    html = await r.text()
    assert r.status == 200 and "{{v}}" not in html and "/static/app.js?v=" in html
    assert (await client.get("/static/app.js")).status == 200


async def test_auth_is_required_and_checked(client):
    assert (await client.get("/api/me")).status == 401
    bad = {"Authorization": "tma " + init_data(2, token="999:OTHER")}
    assert (await client.get("/api/me", headers=bad)).status == 401
    old = auth(2, auth_date=int(time.time()) - 2 * 86400)
    r = await client.get("/api/me", headers=old)
    assert r.status == 401 and "устарела" in (await r.json())["error"]

    r = await client.get("/api/me", headers=auth(2))
    data = await r.json()
    assert r.status == 200 and data["user_id"] == 2 and data["is_admin"] is False
    assert (await client.get("/api/admin/stats", headers=auth(2))).status == 403
    assert (await (await client.get("/api/me", headers=auth(ADMIN))).json())["is_admin"] is True


async def test_full_flow_through_api(client, env):
    u = auth(2)
    pets = await (await client.get("/api/pets", headers=u)).json()
    dog = next(p for p in pets["catalog"] if p["name"] == "Пёс")

    r = await client.post("/api/pets/buy", json={"type_id": dog["id"]}, headers=u)
    invoice = (await r.json())["invoice"]
    assert r.status == 200 and invoice["pay_url"].startswith("https://t.me/")
    env.cp.pay(max(env.cp.invoices))
    await PaymentPoller(env.shop, env.cp).tick()
    status = await (await client.get(f"/api/invoices/{invoice['id']}", headers=u)).json()
    assert status["status"] == "paid"
    # Чужой счёт посмотреть нельзя
    assert (await client.get(f"/api/invoices/{invoice['id']}", headers=auth(3))).status == 400

    env.clock.advance(30 * env.DAY)
    claim = await (await client.post("/api/pets/claim", headers=u)).json()
    assert claim == {"claimed": 120000, "balance": 120000}

    products = (await (await client.get("/api/shop", headers=u)).json())["products"]
    r = await client.post("/api/orders", json={"product_id": products[0]["id"], "details": "nick",
                                               "use_bonus": True}, headers=u)
    order = (await r.json())["order"]
    assert r.status == 200 and order["status"] == "paid"

    r = await client.post(f"/api/admin/orders/{order['id']}/done", headers=auth(ADMIN))
    assert (await r.json())["order"]["status"] == "done"
    profile = await (await client.get("/api/profile", headers=u)).json()
    assert profile["orders"][0]["status"] == "done" and profile["stats"]["pets"] == 1


async def test_admin_edits_in_rubles(client):
    a = auth(ADMIN)
    r = await client.post("/api/admin/products", headers=a, json={
        "game": "Standoff 2", "title": "100 голды", "price": "1 200,50", "input_label": "ID", "sort": 0, "active": True,
    })
    product = (await r.json())["product"]
    assert r.status == 200 and product["price"] == 120050

    r = await client.post(f"/api/admin/products/{product['id']}", headers=a, json={**product, "price": "abc"})
    assert r.status == 400 and "Неверная сумма" in (await r.json())["error"]

    r = await client.post("/api/admin/pets", headers=a, json={
        "name": "Хомяк", "emoji": "🐹", "price": "100", "total_return": "110", "duration_days": 10, "active": True,
    })
    assert (await r.json())["pet"]["total_return"] == 11000

    await client.get("/api/me", headers=auth(5))  # пользователь открыл мини-апп
    r = await client.post("/api/admin/balance", headers=a, json={"user_id": 5, "amount": "+150", "note": "бонус"})
    assert (await r.json())["balance"] == 15000


async def test_bad_requests_return_json_errors(client):
    u = auth(2)
    r = await client.post("/api/pets/buy", data="not json", headers=u)
    assert r.status == 400 and (await r.json())["error"]
    r = await client.post("/api/pets/buy", json={"type_id": "1"}, headers=u)
    assert r.status == 400
    r = await client.post("/api/orders", json={"product_id": 999, "details": "x"}, headers=u)
    assert r.status == 400 and "не продаётся" in (await r.json())["error"]
