"""Быстрый поиск арбитража: опрашиваем ленту новых лотов каждой биржи и
сразу сравниваем каждый новый лот с флорами на остальных биржах."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Awaitable, Callable

from app.arbitrage import Arbitrage, Opportunity
from app.markets.base import Market
from app.models import Listing

log = logging.getLogger(__name__)

# Флор, на основе которого нашли сделку, перепроверяем, если он старше стольких секунд
CONFIRM_MAX_AGE = 3.0


class SeenSet:
    def __init__(self, limit: int = 5000):
        self._d: OrderedDict[tuple, None] = OrderedDict()
        self.limit = limit

    def add(self, item: tuple) -> bool:
        """True, если элемент новый."""
        if item in self._d:
            return False
        self._d[item] = None
        if len(self._d) > self.limit:
            self._d.popitem(last=False)
        return True


class Scanner:
    def __init__(
        self,
        engine: Arbitrage,
        interval: float,
        on_opportunity: Callable[[Opportunity], Awaitable[None]],
        prefilter: Callable[[Listing], bool] = lambda _: True,
        bases: Callable[[], set[str]] = lambda: {"model"},
        want: Callable[[Opportunity], bool] = lambda op: op.profit > 0,
    ):
        self.engine = engine
        self.interval = interval
        self.on_opportunity = on_opportunity
        self.prefilter = prefilter
        # По чему сравнивать новые лоты: model — с флором той же модели, backdrop — того же фона
        self.bases = bases
        # Нужна ли кому-то такая сделка (чтобы не тратить запросы на перепроверку)
        self.want = want
        self._alerted = SeenSet()
        self._tasks: list[asyncio.Task] = []
        self._pending: set[asyncio.Task] = set()

    def start(self) -> None:
        for market in self.engine.markets.values():
            self._tasks.append(asyncio.create_task(self._loop(market), name=f"scan-{market.name}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self, market: Market) -> None:
        seen = SeenSet()
        first = True
        errors = 0
        while True:
            try:
                listings = await market.latest(limit=20)
                self.engine.learn(listings)
                # Ключ с ценой: если лот перевыставили дешевле, он снова считается новым
                fresh = [x for x in listings if seen.add((x.id, round(x.buy_cost, 4)))]
                if not first:
                    for x in fresh:
                        if self.prefilter(x):
                            task = asyncio.create_task(self._handle(x))
                            self._pending.add(task)
                            task.add_done_callback(self._pending.discard)
                first = False
                errors = 0
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                errors += 1
                delay = min(60, self.interval * 2**errors)
                log.warning("%s: ошибка ленты (%s), повтор через %.0fс", market.name, e, delay)
                await asyncio.sleep(delay)

    async def _handle(self, listing: Listing) -> None:
        for basis in sorted(self.bases()):
            key = listing.backdrop_key if basis == "backdrop" else listing.key
            if key is None:
                continue
            try:
                op = await self.engine.check(key, buy=listing, basis=basis)
                if op is None or not self.want(op):
                    continue
                # Нашли что-то интересное по кешу — перепроверяем на свежих данных
                op = await self.engine.check(key, buy=listing, max_age=CONFIRM_MAX_AGE, basis=basis)
                if op is None or not self.want(op):
                    continue
                if self._alerted.add((basis, listing.market, listing.id, round(listing.buy_cost, 4))):
                    await self.on_opportunity(op)
            except Exception:
                log.exception("Ошибка при проверке лота %s/%s (%s)", listing.market, listing.id, basis)
