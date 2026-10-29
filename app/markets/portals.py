from __future__ import annotations

import logging
import time
from urllib.parse import quote_plus, urlencode

from app.markets.base import Market, MarketError
from app.models import GiftKey, Listing, canon, parse_ts

log = logging.getLogger(__name__)

API = "https://portals-market.com/api/"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://portals-market.com",
    "Referer": "https://portals-market.com/",
}


def parse_portals(item: dict, sell_fee: float) -> Listing:
    attrs = {a.get("type"): a.get("value") for a in item.get("attributes") or []}
    price = float(item["price"])
    number = item.get("external_collection_number")
    return Listing(
        market="portals",
        id=str(item["id"]),
        collection=item.get("name") or "",
        model=attrs.get("model"),
        backdrop=attrs.get("backdrop"),
        symbol=attrs.get("symbol"),
        number=int(number) if number is not None else None,
        # На Portals покупатель платит ровно указанную цену, комиссия берётся с продавца
        buy_cost=price,
        seller_net=price * (1 - sell_fee),
        raw=item,
        event_at=parse_ts(item.get("listed_at")),
    )


def parse_actions(data: dict, sell_fee: float) -> list[Listing]:
    """Лента активности: новые лоты и снижения/изменения цены (listing, price_update)."""
    actions = data.get("actions")
    if not isinstance(actions, list):
        raise FeedFormatError("в ответе нет списка actions")
    out = []
    relevant = 0
    for action in actions:
        if action.get("type") not in ("listing", "price_update"):
            continue
        relevant += 1
        nft = action.get("nft") or {}
        price = nft.get("price") or action.get("amount")
        if not nft.get("id") or not price or not nft.get("attributes"):
            continue
        if nft.get("status") not in (None, "listed"):
            continue  # уже продан или снят с продажи
        x = parse_portals({**nft, "price": price}, sell_fee)
        x.event_at = parse_ts(action.get("created_at")) or x.event_at
        out.append(x)
    if relevant and not out:
        raise FeedFormatError("не удалось разобрать ни одного события")
    return out


class FeedFormatError(Exception):
    pass


# Если лента активности недоступна, столько секунд берём обычную ленту лотов
ACTIVITY_RETRY = 600


class Portals(Market):
    name = "portals"
    title = "Portals"
    app_url = "https://t.me/portals/market"

    def __init__(self, auth, proxy=None, sell_fee: float = 0.05, request_gap: float = 0.0):
        super().__init__(auth, proxy, request_gap)
        self.sell_fee = sell_fee
        self._activity_off_until = 0.0
        self.feed = "activity"

    async def _search(self, params: dict, limit: int) -> list[Listing]:
        query = {"offset": 0, "limit": limit, **params, "status": "listed"}
        url = API + "nfts/search?" + urlencode(query, quote_via=quote_plus)
        headers = {**HEADERS, "Authorization": "tma " + await self.auth.init_data(self.name)}
        data = self.check(await self.http.get(url, headers=headers))
        return [parse_portals(i, self.sell_fee) for i in data.get("results") or [] if i.get("price")]

    async def _latest(self, limit: int) -> list[Listing]:
        # Лента активности видит и новые лоты, и снижения цены у старых — на них снайперы ловят больше всего
        if time.monotonic() >= self._activity_off_until:
            try:
                listings = await self._activity(limit)
                self.feed = "activity"
                return listings
            except (FeedFormatError, KeyError, TypeError, ValueError) as e:
                self._activity_failed(e)
            except MarketError as e:
                if e.status not in (400, 404, 405, 422):
                    raise  # 429, 5xx и авторизацию обрабатываем как обычно
                self._activity_failed(e)
        self.feed = "search"
        return await self._search({"sort_by": "listed_at desc"}, limit)

    def _activity_failed(self, error: Exception) -> None:
        log.warning("portals: лента активности не работает (%s), %d с беру обычную ленту", error, ACTIVITY_RETRY)
        self._activity_off_until = time.monotonic() + ACTIVITY_RETRY

    async def _activity(self, limit: int) -> list[Listing]:
        query = {
            "offset": 0,
            "limit": limit,
            "sort_by": "listed_at desc",
            "action_types": "listing,price_update",
        }
        url = API + "market/actions/?" + urlencode(query, quote_via=quote_plus)
        headers = {**HEADERS, "Authorization": "tma " + await self.auth.init_data(self.name)}
        return parse_actions(self.check(await self.http.get(url, headers=headers)), self.sell_fee)

    async def buy(self, listing: Listing) -> None:
        """Покупает лот по его текущей цене. Если цену успели поменять, Portals откажет."""
        await self.call(self._buy, listing, priority=True)

    async def _buy(self, listing: Listing) -> None:
        # Цену отправляем ровно той строкой, что пришла от API ("57" / "57.5")
        price = str(listing.raw.get("price") or listing.buy_cost)
        payload = {"nft_details": [{"id": listing.id, "price": price}]}
        headers = {**HEADERS, "Authorization": "tma " + await self.auth.init_data(self.name)}
        data = self.check(await self.http.post(API + "nfts", json=payload, headers=headers))
        # Если лот уже купил кто-то другой, Portals отвечает 200, но total_purchased = 0
        if not data.get("total_purchased"):
            raise MarketError(f"{self.name}: не куплено — лот уже продан или цена изменилась: {str(data)[:200]}")

    async def balance(self) -> float:
        """Баланс TON на Portals."""
        return await self.call(self._balance)

    async def _balance(self) -> float:
        headers = {**HEADERS, "Authorization": "tma " + await self.auth.init_data(self.name)}
        data = self.check(await self.http.get(API + "users/wallets/", headers=headers))
        return float(data.get("balance") or 0)

    async def _cheapest(self, key: GiftKey, limit: int) -> list[Listing]:
        params = {"sort_by": "price asc", "filter_by_collections": canon(key.collection)}
        if key.model:
            params["filter_by_models"] = canon(key.model)
        if key.backdrop:
            params["filter_by_backdrops"] = canon(key.backdrop)
        return await self._search(params, limit)
