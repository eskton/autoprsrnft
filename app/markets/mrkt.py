from __future__ import annotations

import asyncio

from app.markets.base import Market
from app.models import GiftKey, Listing, canon

API = "https://api.tgmrkt.io/api/v1/"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://cdn.tgmrkt.io",
    "Referer": "https://cdn.tgmrkt.io/",
}
NANO = 1_000_000_000


def parse_mrkt(item: dict, sell_fee: float) -> Listing:
    price = item["salePrice"] / NANO
    net = item.get("salePriceWithoutFee")
    number = item.get("number")
    return Listing(
        market="mrkt",
        id=str(item["id"]),
        collection=item.get("collectionTitle") or item.get("collectionName") or "",
        model=item.get("modelTitle") or item.get("modelName"),
        backdrop=item.get("backdropName"),
        symbol=item.get("symbolName"),
        number=int(number) if number is not None else None,
        buy_cost=price,
        seller_net=net / NANO if net else price * (1 - sell_fee),
        raw=item,
    )


class Mrkt(Market):
    name = "mrkt"
    title = "MRKT"
    app_url = "https://t.me/mrkt/app"

    def __init__(self, auth, proxy=None, sell_fee: float = 0.05):
        super().__init__(auth, proxy)
        self.sell_fee = sell_fee
        self._token: str | None = None
        self._token_lock = asyncio.Lock()

    async def reset_auth(self) -> None:
        self._token = None
        await super().reset_auth()

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        async with self._token_lock:
            if not self._token:
                init_data = await self.auth.init_data(self.name)
                resp = await self.http.post(API + "auth", headers=HEADERS, json={"data": init_data})
                token = self.check(resp).get("token")
                if not token:
                    raise RuntimeError(f"mrkt: в ответе auth нет токена: {resp.text[:200]}")
                self._token = token
        return self._token

    async def _saling(self, limit: int, ordering: str, low_to_high: bool, key: GiftKey | None = None):
        payload = {
            "count": limit,
            "cursor": "",
            "collectionNames": [canon(key.collection)] if key else [],
            "modelNames": [canon(key.model)] if key and key.model else [],
            "backdropNames": [],
            "symbolNames": [],
            "ordering": ordering,
            "lowToHigh": low_to_high,
            "maxPrice": None,
            "minPrice": None,
            "number": None,
            "query": None,
            "promotedFirst": False,
        }
        token = await self._get_token()
        headers = {**HEADERS, "Authorization": token, "Cookie": f"access_token={token}"}
        data = self.check(await self.http.post(API + "gifts/saling", headers=headers, json=payload))
        return [parse_mrkt(i, self.sell_fee) for i in data.get("gifts") or [] if i.get("salePrice")]

    async def _latest(self, limit: int) -> list[Listing]:
        return await self._saling(limit, "Date", False)

    async def _cheapest(self, key: GiftKey, limit: int) -> list[Listing]:
        return await self._saling(limit, "Price", True, key)
