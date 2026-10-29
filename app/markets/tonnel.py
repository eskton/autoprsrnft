from __future__ import annotations

import json
import logging
import re

from app.markets.base import Market
from app.models import GiftKey, Listing, canon, strip_rarity

log = logging.getLogger(__name__)

API = "https://gifts2.tonnel.network/api/pageGifts"
HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://market.tonnel.network",
    "referer": "https://market.tonnel.network/",
}
SORT_PRICE = '{"price":1,"gift_id":-1}'
SORT_LATEST = '{"message_post_time":-1,"gift_id":-1}'


def parse_tonnel(item: dict, buy_fee: float, sell_fee: float) -> Listing:
    # API отдаёт «сырую» цену; покупатель платит сверху комиссию Tonnel (по умолчанию +10%)
    price = float(item["price"])
    number = item.get("gift_num")
    return Listing(
        market="tonnel",
        id=str(item["gift_id"]),
        collection=item.get("name") or "",
        model=strip_rarity(item.get("model") or "") or None,
        backdrop=strip_rarity(item.get("backdrop") or "") or None,
        symbol=strip_rarity(item.get("symbol") or "") or None,
        number=int(number) if number is not None else None,
        buy_cost=price * (1 + buy_fee),
        seller_net=price * (1 - sell_fee),
        raw=item,
    )


def base_filter() -> dict:
    return {
        "price": {"$exists": True},
        "buyer": {"$exists": False},
        "asset": "TON",
        "refunded": {"$ne": True},
        "export_at": {"$exists": True},
    }


class Tonnel(Market):
    name = "tonnel"
    title = "Tonnel"
    app_url = "https://t.me/Tonnel_Network_bot"

    def __init__(self, auth, proxy=None, buy_fee: float = 0.10, sell_fee: float = 0.0):
        super().__init__(auth, proxy)
        self.buy_fee = buy_fee
        self.sell_fee = sell_fee

    async def _user_auth(self) -> str:
        # Для просмотра лотов Tonnel авторизация не обязательна
        if not self.auth:
            return ""
        try:
            return await self.auth.init_data(self.name)
        except Exception as e:
            log.warning("tonnel: не удалось получить initData (%s), работаем без него", e)
            return ""

    async def _page(self, flt: dict, sort: str, limit: int) -> list[Listing]:
        payload = {
            "page": 1,
            "limit": limit,
            "sort": sort,
            "filter": json.dumps(flt),
            "price_range": None,
            "ref": 0,
            "user_auth": await self._user_auth(),
        }
        data = self.check(await self.http.post(API, headers=HEADERS, json=payload))
        items = data if isinstance(data, list) else data.get("gifts") or []
        return [parse_tonnel(i, self.buy_fee, self.sell_fee) for i in items if i.get("price")]

    async def _latest(self, limit: int) -> list[Listing]:
        return await self._page(base_filter(), SORT_LATEST, limit)

    async def _cheapest(self, key: GiftKey, limit: int) -> list[Listing]:
        flt = base_filter()
        name = canon(key.collection)
        flt["gift_name"] = "Jack-in-the-Box" if name.lower() == "jack-in-the-box" else name
        # Tonnel хранит атрибуты с редкостью: 'Onyx Black (1%)', поэтому ищем по началу строки
        if key.model:
            flt["model"] = {"$regex": f"^{_rx(canon(key.model))} \\("}
        if key.backdrop:
            flt["backdrop"] = {"$regex": f"^{_rx(canon(key.backdrop))} \\("}
        return await self._page(flt, SORT_PRICE, limit)


def _rx(text: str) -> str:
    """Экранирует спецсимволы регулярки, но не пробелы (как в проверенном клиенте tonnelmp)."""
    return re.sub(r"([.^$*+?{}\[\]\\|()])", r"\\\1", text)
