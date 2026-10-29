from __future__ import annotations

from urllib.parse import quote_plus, urlencode

from app.markets.base import Market
from app.models import GiftKey, Listing, canon

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
    )


class Portals(Market):
    name = "portals"
    title = "Portals"
    app_url = "https://t.me/portals/market"

    def __init__(self, auth, proxy=None, sell_fee: float = 0.05):
        super().__init__(auth, proxy)
        self.sell_fee = sell_fee

    async def _search(self, params: dict, limit: int) -> list[Listing]:
        query = {"offset": 0, "limit": limit, **params, "status": "listed"}
        url = API + "nfts/search?" + urlencode(query, quote_via=quote_plus)
        headers = {**HEADERS, "Authorization": "tma " + await self.auth.init_data(self.name)}
        data = self.check(await self.http.get(url, headers=headers))
        return [parse_portals(i, self.sell_fee) for i in data.get("results") or [] if i.get("price")]

    async def _latest(self, limit: int) -> list[Listing]:
        return await self._search({"sort_by": "listed_at desc"}, limit)

    async def _cheapest(self, key: GiftKey, limit: int) -> list[Listing]:
        params = {"sort_by": "price asc", "filter_by_collections": canon(key.collection)}
        if key.model:
            params["filter_by_models"] = canon(key.model)
        if key.backdrop:
            params["filter_by_backdrops"] = canon(key.backdrop)
        return await self._search(params, limit)
