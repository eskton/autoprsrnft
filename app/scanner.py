"""Быстрый поиск сделок: опрашиваем ленту новых лотов каждой биржи и
сразу сравниваем каждый новый лот с флорами."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable

from app.arbitrage import Arbitrage, Opportunity
from app.markets.base import Market, RateLimited
from app.models import Listing

log = logging.getLogger(__name__)

# Флор, на основе которого нашли сделку, перепроверяем, если он старше стольких секунд
CONFIRM_MAX_AGE = 3.0
# Сколько событий брать из ленты за один опрос
FEED_LIMIT = 20


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
        confirm_age: Callable[[Opportunity], float] = lambda op: CONFIRM_MAX_AGE,
        max_interval: float = 10.0,
    ):
        self.engine = engine
        # Опрашиваем ленту так часто, как позволяет биржа: от interval до max_interval
        self.min_interval = interval
        self.max_interval = max(max_interval, interval)
        self.on_opportunity = on_opportunity
        self.prefilter = prefilter
        # По чему сравнивать новые лоты: model — с флором той же модели, backdrop — того же фона
        self.bases = bases
        # Нужна ли кому-то такая сделка (чтобы не тратить запросы на перепроверку)
        self.want = want
        # Насколько свежим должен быть флор, чтобы действовать без перепроверки
        self.confirm_age = confirm_age
        # Для /status: пауза между опросами и через сколько секунд после выставления видим лот
        self.delays: dict[str, float] = {}
        self.lag: dict[str, float] = {}
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
        delay = self.min_interval
        while True:
            try:
                listings = await market.latest(limit=FEED_LIMIT)
                now = time.time()
                self.engine.learn(listings)
                for x in listings:
                    # Ключ с ценой: если лот перевыставили дешевле, он снова считается новым
                    if not seen.add((x.id, round(x.buy_cost, 4))):
                        continue
                    x.seen_at = now
                    self.engine.observe(x)
                    if first:
                        continue  # первый опрос: просто запоминаем то, что уже висит
                    self._track_lag(market.name, x)
                    if self.prefilter(x):
                        task = asyncio.create_task(self._handle(x))
                        self._pending.add(task)
                        task.add_done_callback(self._pending.discard)
                first = False
                # Биржа отвечает — понемногу возвращаемся к самому частому опросу
                delay = max(self.min_interval, delay * 0.85)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 429 или сбой: опрашиваем реже, чтобы биржа не забанила
                delay = min(self.max_interval, max(delay * 2, self.min_interval * 2))
                level = logging.INFO if isinstance(e, RateLimited) else logging.WARNING
                log.log(level, "%s: ошибка ленты (%s), следующий опрос через %.1fс", market.name, e, delay)
            self.delays[market.name] = delay
            await asyncio.sleep(delay)

    def _track_lag(self, market: str, x: Listing) -> None:
        if x.event_at is None or x.seen_at is None:
            return
        lag = x.seen_at - x.event_at
        # Старые лоты с новой ценой и сильно сбитые часы не считаем
        if -2 <= lag <= 120:
            prev = self.lag.get(market)
            self.lag[market] = lag if prev is None else prev * 0.8 + lag * 0.2

    async def _handle(self, listing: Listing) -> None:
        for basis in sorted(self.bases()):
            key = listing.backdrop_key if basis == "backdrop" else listing.key
            if key is None:
                continue
            try:
                op = await self.engine.check(key, buy=listing, basis=basis)
                if op is None or not self.want(op):
                    continue
                # Флор старше допустимого — перепроверяем на свежих данных
                op = await self.engine.check(key, buy=listing, max_age=self.confirm_age(op), basis=basis)
                if op is None or not self.want(op):
                    continue
                if self._alerted.add((basis, listing.market, listing.id, round(listing.buy_cost, 4))):
                    await self.on_opportunity(op)
            except Exception:
                log.exception("Ошибка при проверке лота %s/%s (%s)", listing.market, listing.id, basis)
