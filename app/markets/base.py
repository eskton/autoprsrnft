from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from curl_cffi.requests import AsyncSession

from app.models import GiftKey, Listing
from app.tg_auth import TelegramAuth

log = logging.getLogger(__name__)


class MarketError(Exception):
    pass


class AuthExpired(MarketError):
    pass


class Market(ABC):
    name: str = ""
    title: str = ""
    # Ссылка, по которой открыть маркет в Telegram
    app_url: str = ""

    def __init__(self, auth: TelegramAuth | None, proxy: str | None = None):
        self.auth = auth
        self.http = AsyncSession(impersonate="chrome", timeout=10, proxy=proxy)
        # Не пускаем к одной бирже больше N запросов одновременно, чтобы не словить бан
        self._sem = asyncio.Semaphore(4)
        self._cooldown_until = 0.0
        self.last_ok: float | None = None
        self.last_error: str | None = None

    async def close(self) -> None:
        await self.http.close()

    async def call(self, fn, *args, **kwargs) -> Any:
        """Выполняет запрос: ждёт cooldown после 429 и один раз переавторизуется при 401/403."""
        for attempt in (1, 2):
            wait = self._cooldown_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self._sem:
                    result = await fn(*args, **kwargs)
                self.last_ok = time.time()
                self.last_error = None
                return result
            except AuthExpired:
                await self.reset_auth()
                if attempt == 2:
                    self.last_error = "авторизация не проходит"
                    raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                raise

    def check(self, resp) -> Any:
        if resp.status_code in (401, 403):
            raise AuthExpired(f"{self.name}: {resp.status_code}")
        if resp.status_code == 429:
            self._cooldown_until = time.monotonic() + 5
            raise MarketError(f"{self.name}: 429 Too Many Requests")
        if resp.status_code != 200:
            raise MarketError(f"{self.name}: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def reset_auth(self) -> None:
        if self.auth:
            self.auth.invalidate(self.name)

    async def latest(self, limit: int = 20) -> list[Listing]:
        """Последние выставленные на продажу лоты."""
        return await self.call(self._latest, limit)

    async def cheapest(self, key: GiftKey, limit: int = 3) -> list[Listing]:
        """Самые дешёвые лоты коллекции (и модели, если указана), по возрастанию цены."""
        listings = await self.call(self._cheapest, key, limit)
        return sorted(listings, key=lambda x: x.buy_cost)

    @abstractmethod
    async def _latest(self, limit: int) -> list[Listing]: ...

    @abstractmethod
    async def _cheapest(self, key: GiftKey, limit: int) -> list[Listing]: ...
