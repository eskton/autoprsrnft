from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from app.markets.base import Market
from app.models import GiftKey, Listing, norm

log = logging.getLogger(__name__)

Quotes = dict[str, "list[Listing] | Exception"]

# «… фон Onyx Black», «… bg: Black», «… backdrop Black» в конце запроса
_BACKDROP_RE = re.compile(r"(?:^|[\s|/,;])(?:фон|bg|backdrop)(?:\s*:\s*|\s+)(.+)$", re.IGNORECASE)


@dataclass
class Opportunity:
    key: GiftKey
    buy: Listing
    sell: Listing  # лот-ориентир на бирже, где выгоднее всего продать
    floor: Listing  # самый дешёвый другой такой же лот на всех биржах
    others: int  # сколько других таких лотов нашли (до 3 на биржу)
    quotes: Quotes
    basis: str = "model"  # model — сравнение по модели, backdrop — по фону

    @property
    def sell_net(self) -> float:
        return self.sell.seller_net

    @property
    def profit(self) -> float:
        return self.sell_net - self.buy.buy_cost

    @property
    def pct(self) -> float:
        return self.profit / self.buy.buy_cost * 100 if self.buy.buy_cost else 0.0

    @property
    def discount_pct(self) -> float:
        """На сколько % лот дешевле флора (цены покупки с комиссиями)."""
        if not self.floor.buy_cost:
            return 0.0
        return (self.floor.buy_cost - self.buy.buy_cost) / self.floor.buy_cost * 100


def best_deal(key: GiftKey, quotes: Quotes, buy: Listing | None = None, basis: str = "model") -> Opportunity | None:
    """Где купить дешевле всего и где выгоднее всего продать.

    buy — конкретный лот (например, только что выставленный). Если не задан,
    берём самый дешёвый лот среди всех бирж.
    Продажа оценивается по флору биржи: выставляемся по цене самого дешёвого
    лота (не считая того, что покупаем) и получаем его seller_net.
    """
    ok = {m: ls for m, ls in quotes.items() if isinstance(ls, list)}
    if buy is None:
        candidates = [ls[0] for ls in ok.values() if ls]
        if not candidates:
            return None
        buy = min(candidates, key=lambda x: x.buy_cost)

    def same(x: Listing) -> bool:
        return x.market == buy.market and x.id == buy.id

    refs = [ref for ls in ok.values() if (ref := next((x for x in ls if not same(x)), None))]
    if not refs:
        return None
    return Opportunity(
        key=key,
        buy=buy,
        sell=max(refs, key=lambda x: x.seller_net),
        floor=min(refs, key=lambda x: x.buy_cost),
        others=sum(1 for ls in ok.values() for x in ls if not same(x)),
        quotes=quotes,
        basis=basis,
    )


class Arbitrage:
    def __init__(self, markets: dict[str, Market], floor_ttl: float = 20.0):
        self.markets = markets
        self.floor_ttl = floor_ttl
        self._cache: dict[tuple[str, str], tuple[float, list[Listing]]] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}
        # Точные названия, подсмотренные в лотах: norm -> как пишет биржа
        self.collections: dict[str, str] = {}
        self.models: dict[str, dict[str, str]] = {}
        self.backdrops: dict[str, str] = {}  # фоны общие для всех коллекций

    def learn(self, listings: list[Listing]) -> None:
        for x in listings:
            c = norm(x.collection)
            if not c:
                continue
            self.collections.setdefault(c, x.collection)
            if x.model:
                self.models.setdefault(c, {}).setdefault(norm(x.model), x.model)
            if x.backdrop:
                self.backdrops.setdefault(norm(x.backdrop), x.backdrop)

    def resolve(self, text: str) -> GiftKey | None:
        """Разбирает запрос пользователя:
        'plush pepe' / 'Plush Pepe | Cozy Galaxy' / 'Plush Pepe Cozy Galaxy'
        'Toy Bear фон Onyx Black' / 'Toy Bear | Wizard | Onyx Black'.
        """
        text = text.strip()
        backdrop = None
        m = _BACKDROP_RE.search(text)
        if m:
            backdrop = m.group(1).strip(" |/,;") or None
            text = text[: m.start()].strip(" |/,;")
        parts = [p.strip() for p in re.split(r"[|/,;]", text) if p.strip()]
        if not parts:
            return None
        collection = parts[0]
        model = parts[1] if len(parts) > 1 else None
        if len(parts) > 2 and backdrop is None:
            backdrop = parts[2]
        if model is None:
            # «Plush Pepe Cozy Galaxy» без разделителя: ищем самое длинное известное начало
            words = collection.split()
            for i in range(len(words) - 1, 0, -1):
                head = " ".join(words[:i])
                if norm(head) in self.collections:
                    collection, model = head, " ".join(words[i:])
                    break
        c = norm(collection)
        known_models = self.models.get(c, {})
        # «Toy Bear | Onyx Black»: второе слово — известный фон, а не модель
        if model and backdrop is None and norm(model) in self.backdrops and norm(model) not in known_models:
            model, backdrop = None, model
        return GiftKey(
            self.collections.get(c, collection),
            known_models.get(norm(model), model) if model else None,
            self.backdrops.get(norm(backdrop), backdrop) if backdrop else None,
        )

    async def _fetch(self, market: Market, key: GiftKey) -> list[Listing]:
        listings = await market.cheapest(key, limit=3)
        self._cache[(market.name, key.id)] = (time.monotonic(), listings)
        self.learn(listings)
        return listings

    async def floor(self, market: Market, key: GiftKey, max_age: float | None = None) -> list[Listing]:
        ttl = self.floor_ttl if max_age is None else max_age
        ck = (market.name, key.id)
        cached = self._cache.get(ck)
        if cached and time.monotonic() - cached[0] <= ttl:
            return cached[1]
        # Схлопываем одновременные запросы одного и того же флора в один
        task = self._inflight.get(ck)
        if task is None:
            task = asyncio.create_task(self._fetch(market, key))
            self._inflight[ck] = task
            task.add_done_callback(lambda _: self._inflight.pop(ck, None))
        return await task

    async def quotes(self, key: GiftKey, max_age: float | None = None) -> Quotes:
        names = list(self.markets)
        results = await asyncio.gather(
            *(self.floor(self.markets[n], key, max_age) for n in names), return_exceptions=True
        )
        return dict(zip(names, results))

    async def check(
        self, key: GiftKey, buy: Listing | None = None, max_age: float | None = None, basis: str = "model"
    ) -> Opportunity | None:
        quotes = await self.quotes(key, max_age)
        return best_deal(key, quotes, buy, basis)
