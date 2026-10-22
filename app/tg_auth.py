"""Получение initData мини-приложений бирж через user-аккаунт Telegram (Telethon)."""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import unquote

from telethon import TelegramClient, utils
from telethon.tl.functions.messages import RequestAppWebViewRequest, RequestMainWebViewRequest
from telethon.tl.types import InputBotAppShortName

log = logging.getLogger(__name__)

# bot username -> short name мини-приложения (None = главное приложение бота)
APPS: dict[str, tuple[str, str | None]] = {
    "portals": ("portals", "market"),
    "mrkt": ("mrkt", "app"),
    "tonnel": ("Tonnel_Network_bot", None),
}

# initData живёт ограниченное время; обновляем заранее
INIT_DATA_TTL = 30 * 60


def extract_init_data(url: str) -> str:
    """Достаёт tgWebAppData из URL мини-приложения и декодирует его."""
    if "tgWebAppData=" not in url:
        raise ValueError("В URL мини-приложения нет tgWebAppData")
    data = url.split("tgWebAppData=", 1)[1]
    data = data.split("&tgWebAppVersion", 1)[0]
    return unquote(data)


class TelegramAuth:
    def __init__(self, session: str, api_id: int, api_hash: str):
        self.client = TelegramClient(session, api_id, api_hash)
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Сессия Telegram не авторизована. Сначала запусти: python login.py")

    async def stop(self) -> None:
        await self.client.disconnect()

    def invalidate(self, market: str) -> None:
        self._cache.pop(market, None)

    async def init_data(self, market: str) -> str:
        cached = self._cache.get(market)
        if cached and time.monotonic() - cached[1] < INIT_DATA_TTL:
            return cached[0]
        async with self._lock:
            cached = self._cache.get(market)
            if cached and time.monotonic() - cached[1] < INIT_DATA_TTL:
                return cached[0]
            data = await self._request(market)
            self._cache[market] = (data, time.monotonic())
            log.info("Получен новый initData для %s", market)
            return data

    async def _request(self, market: str) -> str:
        username, short_name = APPS[market]
        peer = await self.client.get_input_entity(username)
        bot = utils.get_input_user(peer)
        if short_name:
            req = RequestAppWebViewRequest(
                peer=peer,
                app=InputBotAppShortName(bot_id=bot, short_name=short_name),
                platform="android",
            )
        else:
            req = RequestMainWebViewRequest(peer=peer, bot=bot, platform="android")
        result = await self.client(req)
        return extract_init_data(result.url)
