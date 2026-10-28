"""HTTP-сервер: отдаёт мини-апп и JSON API для него.

Каждый запрос к /api/ подписан Telegram (initData), поэтому подделать
пользователя нельзя: подпись проверяется токеном бота.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from shop.money import parse_rub
from shop.service import Shop, ShopError

log = logging.getLogger(__name__)

SHOP = web.AppKey("shop", Shop)
CONF = web.AppKey("conf", dict)
USER_ID = web.RequestKey("user_id", int)
IS_ADMIN = web.RequestKey("is_admin", bool)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise ShopError("Неверный запрос") from None
    if not isinstance(data, dict):
        raise ShopError("Неверный запрос")
    return data


def _id(data: dict, key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ShopError("Неверный запрос")
    return value


def _rub_fields(data: dict, *fields: str) -> dict:
    """Админка присылает суммы в рублях ('1200' / '1 200,50'), сервис работает в копейках."""
    out = dict(data)
    for field in fields:
        value = out.get(field)
        if value in (None, ""):
            out[field] = None
            continue
        try:
            out[field] = parse_rub(value)
        except ValueError:
            raise ShopError(f"Неверная сумма: {value}") from None
    return out


@web.middleware
async def api_middleware(request: web.Request, handler) -> web.StreamResponse:
    if not request.path.startswith("/api/"):
        return await handler(request)
    conf = request.app[CONF]
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("tma "):
        return _error(401, "Открой магазин через кнопку в боте")
    try:
        data = safe_parse_webapp_init_data(conf["bot_token"], auth[4:])
    except ValueError:
        return _error(401, "Неверная подпись Telegram — перезапусти мини-апп")
    if data.user is None:
        return _error(401, "Нет данных пользователя")
    if time.time() - data.auth_date.timestamp() > conf["init_data_ttl"]:
        return _error(401, "Сессия устарела — закрой и снова открой мини-апп")

    user = data.user
    request[USER_ID] = user.id
    request[IS_ADMIN] = user.id in conf["admin_ids"]
    if request.path.startswith("/api/admin/") and not request[IS_ADMIN]:
        return _error(403, "Только для админа")

    shop = request.app[SHOP]
    seen = conf["seen_users"]
    if seen.get(user.id) != (user.username, user.first_name):
        await shop.touch_user(user.id, user.username, user.first_name)
        seen[user.id] = (user.username, user.first_name)
    try:
        return await handler(request)
    except ShopError as e:
        return _error(400, str(e))
    except web.HTTPException:
        raise
    except Exception:
        log.exception("Ошибка в %s %s", request.method, request.path)
        return _error(500, "Что-то пошло не так, попробуй ещё раз")


routes = web.RouteTableDef()


def _json(data: Any) -> web.Response:
    return web.json_response(data)


@routes.get("/api/me")
async def me(request: web.Request) -> web.Response:
    shop = request.app[SHOP]
    return _json({
        "user_id": request[USER_ID],
        "is_admin": request[IS_ADMIN],
        "balance": await shop.balance(request[USER_ID]),
        "now": shop.now(),
    })


@routes.get("/api/pets")
async def pets(request: web.Request) -> web.Response:
    shop = request.app[SHOP]
    return _json({
        "catalog": await shop.pet_catalog(),
        "mine": await shop.my_pets(request[USER_ID]),
        "now": shop.now(),
    })


@routes.post("/api/pets/buy")
async def buy_pet(request: web.Request) -> web.Response:
    data = await _body(request)
    invoice = await request.app[SHOP].buy_pet(request[USER_ID], _id(data, "type_id"))
    return _json({"invoice": invoice})


@routes.post("/api/pets/claim")
async def claim(request: web.Request) -> web.Response:
    return _json(await request.app[SHOP].claim(request[USER_ID]))


@routes.get("/api/shop")
async def products(request: web.Request) -> web.Response:
    return _json({"products": await request.app[SHOP].products()})


@routes.post("/api/orders")
async def create_order(request: web.Request) -> web.Response:
    data = await _body(request)
    result = await request.app[SHOP].create_order(
        request[USER_ID], _id(data, "product_id"), data.get("details", ""), bool(data.get("use_bonus"))
    )
    return _json(result)


@routes.get("/api/profile")
async def profile(request: web.Request) -> web.Response:
    return _json(await request.app[SHOP].profile(request[USER_ID]))


@routes.get(r"/api/invoices/{id:\d+}")
async def invoice_status(request: web.Request) -> web.Response:
    return _json(await request.app[SHOP].invoice_status(request[USER_ID], int(request.match_info["id"])))


# --- админка ---------------------------------------------------------------

@routes.get("/api/admin/stats")
async def admin_stats(request: web.Request) -> web.Response:
    return _json(await request.app[SHOP].admin_stats())


@routes.get("/api/admin/orders")
async def admin_orders(request: web.Request) -> web.Response:
    status = request.query.get("status", "paid")
    return _json({"orders": await request.app[SHOP].admin_orders(status)})


@routes.post(r"/api/admin/orders/{id:\d+}/done")
async def admin_order_done(request: web.Request) -> web.Response:
    return _json({"order": await request.app[SHOP].complete_order(int(request.match_info["id"]))})


@routes.post(r"/api/admin/orders/{id:\d+}/reject")
async def admin_order_reject(request: web.Request) -> web.Response:
    data = await _body(request)
    order = await request.app[SHOP].reject_order(int(request.match_info["id"]), data.get("note", ""))
    return _json({"order": order})


@routes.get("/api/admin/products")
async def admin_products(request: web.Request) -> web.Response:
    return _json({"products": await request.app[SHOP].admin_products()})


@routes.post("/api/admin/products")
@routes.post(r"/api/admin/products/{id:\d+}")
async def admin_save_product(request: web.Request) -> web.Response:
    data = _rub_fields(await _body(request), "price")
    row_id = int(request.match_info["id"]) if "id" in request.match_info else None
    return _json({"product": await request.app[SHOP].save_product(data, row_id)})


@routes.get("/api/admin/pets")
async def admin_pets(request: web.Request) -> web.Response:
    return _json({"pets": await request.app[SHOP].admin_pet_types()})


@routes.post("/api/admin/pets")
@routes.post(r"/api/admin/pets/{id:\d+}")
async def admin_save_pet(request: web.Request) -> web.Response:
    data = _rub_fields(await _body(request), "price", "total_return")
    row_id = int(request.match_info["id"]) if "id" in request.match_info else None
    return _json({"pet": await request.app[SHOP].save_pet_type(data, row_id)})


@routes.post("/api/admin/balance")
async def admin_balance(request: web.Request) -> web.Response:
    data = await _body(request)
    try:
        amount = parse_rub(data.get("amount", ""))
    except ValueError:
        raise ShopError("Неверная сумма") from None
    balance = await request.app[SHOP].adjust_balance(_id(data, "user_id"), amount, data.get("note", ""))
    return _json({"balance": balance})


# --- статика ---------------------------------------------------------------

@routes.get("/health")
async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


@routes.get("/")
async def index(request: web.Request) -> web.Response:
    return web.Response(
        text=request.app[CONF]["index_html"],
        content_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


def create_app(
    shop: Shop,
    *,
    bot_token: str,
    admin_ids: list[int],
    static_dir: Path,
    init_data_ttl: int = 86400,
) -> web.Application:
    app = web.Application(middlewares=[api_middleware], client_max_size=64 * 1024)
    # Версия файлов для сброса кеша Telegram после обновления
    digest = hashlib.sha1()
    for name in ("app.js", "style.css"):
        digest.update((static_dir / name).read_bytes())
    index_html = (static_dir / "index.html").read_text("utf-8").replace("{{v}}", digest.hexdigest()[:10])

    app[SHOP] = shop
    app[CONF] = {
        "bot_token": bot_token,
        "admin_ids": set(admin_ids),
        "init_data_ttl": init_data_ttl,
        "index_html": index_html,
        "seen_users": {},
    }
    app.add_routes(routes)
    app.router.add_static("/static", static_dir)
    return app
