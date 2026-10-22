import asyncio

import pytest

from app.arbitrage import Arbitrage, best_deal
from app.markets.base import Market
from app.markets.mrkt import parse_mrkt
from app.markets.portals import parse_portals
from app.markets.tonnel import parse_tonnel
from app.models import GiftKey, Listing, canon, nft_slug, norm, strip_rarity
from app.scanner import Scanner
from app.storage import Storage, UserSettings
from app.tg_auth import extract_init_data


def L(market, id, cost, net=None, collection="Plush Pepe", model="Cozy Galaxy"):
    return Listing(market, id, collection, model, None, None, 1, cost, cost if net is None else net)


# --- названия ---------------------------------------------------------------

def test_names():
    assert strip_rarity("Wizard (1.5%)") == "Wizard"
    assert norm("Durov's Cap") == norm("durovs cap")
    assert norm("Wizard (1.5%)") == norm("wizard")
    assert canon("plush pepe") == "Plush Pepe"
    assert canon("Jack-in-the-Box") == "Jack-in-the-Box"
    assert nft_slug("Jack-in-the-Box", 7) == "JackInTheBox-7"
    assert nft_slug("Durov's Cap", 12) == "DurovsCap-12"
    assert GiftKey("Plush Pepe", "Cozy Galaxy").id == GiftKey("plush pepe", "COZY galaxy").id


def test_extract_init_data():
    url = "https://x/#tgWebAppData=query_id%3DAA%26user%3D%257B%2522id%2522%253A1%257D%26hash%3Dab&tgWebAppVersion=8.0"
    assert extract_init_data(url) == "query_id=AA&user=%7B%22id%22%3A1%7D&hash=ab"


# --- парсинг ответов бирж ----------------------------------------------------

def test_parse_portals():
    item = {
        "id": "uuid-1", "name": "Plush Pepe", "price": "100", "external_collection_number": 42,
        "attributes": [
            {"type": "model", "value": "Cozy Galaxy", "rarity_per_mille": 5},
            {"type": "backdrop", "value": "Black", "rarity_per_mille": 10},
            {"type": "symbol", "value": "Star", "rarity_per_mille": 3},
        ],
    }
    x = parse_portals(item, sell_fee=0.05)
    assert (x.collection, x.model, x.backdrop, x.symbol, x.number) == ("Plush Pepe", "Cozy Galaxy", "Black", "Star", 42)
    assert x.buy_cost == 100 and x.seller_net == pytest.approx(95)
    assert x.nft_url == "https://t.me/nft/PlushPepe-42"


def test_parse_tonnel_adds_buyer_fee():
    item = {"gift_id": 555, "gift_num": 42, "name": "Plush Pepe", "model": "Cozy Galaxy (0.5%)",
            "backdrop": "Black (1%)", "symbol": "Star (0.3%)", "price": 100}
    x = parse_tonnel(item, buy_fee=0.10, sell_fee=0.0)
    assert x.model == "Cozy Galaxy" and x.backdrop == "Black"
    assert x.buy_cost == pytest.approx(110) and x.seller_net == pytest.approx(100)


def test_parse_mrkt_nanoton():
    item = {"id": "m1", "collectionName": "Plush Pepe", "modelName": "Cozy Galaxy", "number": 42,
            "salePrice": 100_000_000_000, "salePriceWithoutFee": 95_000_000_000}
    x = parse_mrkt(item, sell_fee=0.05)
    assert x.buy_cost == pytest.approx(100) and x.seller_net == pytest.approx(95)
    item.pop("salePriceWithoutFee")
    assert parse_mrkt(item, sell_fee=0.1).seller_net == pytest.approx(90)


# --- расчёт сделки -----------------------------------------------------------

def test_best_deal_picks_cheapest_buy_and_best_sell():
    key = GiftKey("Plush Pepe", "Cozy Galaxy")
    quotes = {
        "portals": [L("portals", "p1", 120, 114)],
        "tonnel": [L("tonnel", "t1", 99, 90)],
        "mrkt": [L("mrkt", "m1", 140, 133)],
    }
    op = best_deal(key, quotes)
    assert (op.buy.market, op.sell.market) == ("tonnel", "mrkt")
    assert op.profit == pytest.approx(34)


def test_best_deal_skips_bought_listing_and_errors():
    key = GiftKey("Plush Pepe")
    new = L("portals", "p1", 50, 47.5)
    quotes = {
        "portals": [new, L("portals", "p2", 80, 76)],  # продать на той же бирже по следующему лоту
        "tonnel": RuntimeError("down"),
        "mrkt": [],
    }
    op = best_deal(key, quotes, buy=new)
    assert op.sell.id == "p2" and op.profit == pytest.approx(26)
    assert best_deal(key, {"portals": [new]}, buy=new) is None


def test_user_thresholds():
    s = UserSettings(min_profit_ton=2, min_profit_pct=5, max_buy_ton=100)
    assert s.accepts(profit=3, pct=6, buy_cost=50)
    assert not s.accepts(profit=1, pct=6, buy_cost=50)
    assert not s.accepts(profit=3, pct=4, buy_cost=50)
    assert not s.accepts(profit=3, pct=6, buy_cost=150)


def test_storage_roundtrip(tmp_path):
    p = tmp_path / "users.json"
    st = Storage(str(p), UserSettings(min_profit_ton=3))
    st.get(1).min_profit_pct = 9
    st.save()
    again = Storage(str(p), UserSettings())
    assert again.get(1).min_profit_ton == 3 and again.get(1).min_profit_pct == 9


# --- движок и сканер на фейковых биржах --------------------------------------

class FakeMarket(Market):
    def __init__(self, name, floors):
        super().__init__(auth=None)
        self.name = self.title = name
        self.floors = floors  # list[Listing]
        self.feed: list[Listing] = []
        self.cheapest_calls = 0

    async def _latest(self, limit):
        return list(self.feed)

    async def _cheapest(self, key, limit):
        self.cheapest_calls += 1
        await asyncio.sleep(0.01)
        return [x for x in self.floors if x.key.id == key.id][:limit]


async def test_engine_cache_and_resolve():
    m = FakeMarket("portals", [L("portals", "p1", 100)])
    engine = Arbitrage({"portals": m}, floor_ttl=60)
    key = GiftKey("Plush Pepe", "Cozy Galaxy")
    await asyncio.gather(engine.quotes(key), engine.quotes(key))
    await engine.quotes(key)
    assert m.cheapest_calls == 1  # параллельные запросы схлопнулись, потом кеш
    await engine.quotes(key, max_age=0)
    assert m.cheapest_calls == 2
    assert engine.resolve("plush pepe cozy galaxy") == GiftKey("Plush Pepe", "Cozy Galaxy")
    assert engine.resolve("PLUSH PEPE | cozy galaxy") == GiftKey("Plush Pepe", "Cozy Galaxy")
    assert engine.resolve("Toy Bear") == GiftKey("Toy Bear", None)
    await m.close()


async def test_scanner_alerts_only_new_profitable_listings():
    portals = FakeMarket("portals", [L("portals", "p-floor", 150, 142.5)])
    tonnel = FakeMarket("tonnel", [])
    tonnel.feed = [L("tonnel", "old", 200, 180)]
    engine = Arbitrage({"portals": portals, "tonnel": tonnel}, floor_ttl=60)
    found = []

    async def on_op(op):
        found.append(op)

    scanner = Scanner(engine, interval=0.02, on_opportunity=on_op)
    scanner.start()
    await asyncio.sleep(0.05)  # первый проход: старые лоты запоминаются, без алертов
    tonnel.feed.insert(0, L("tonnel", "cheap", 110, 100))
    tonnel.floors = [tonnel.feed[0]]
    await asyncio.sleep(0.15)
    await scanner.stop()
    for m in (portals, tonnel):
        await m.close()

    assert len(found) == 1
    op = found[0]
    assert op.buy.id == "cheap" and op.sell.market == "portals"
    assert op.profit == pytest.approx(32.5)
