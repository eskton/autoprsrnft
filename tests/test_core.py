import asyncio

import pytest

from app.arbitrage import Arbitrage, Opportunity, best_deal
from app.formatting import format_opportunity
from app.markets.base import Market
from app.markets.mrkt import parse_mrkt
from app.markets.portals import parse_portals
from app.markets.tonnel import parse_tonnel
from app.models import GiftKey, Listing, canon, nft_slug, norm, strip_rarity
from app.scanner import Scanner
from app.storage import Storage, UserSettings
from app.tg_auth import extract_init_data


def L(market, id, cost, net=None, collection="Plush Pepe", model="Cozy Galaxy", backdrop=None):
    return Listing(market, id, collection, model, backdrop, None, 1, cost, cost if net is None else net)


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


def deal(buy_cost, sell_net, floor_cost=None, basis="model"):
    buy = L("tonnel", "b", buy_cost)
    sell = L("portals", "s", floor_cost or sell_net, sell_net)
    return Opportunity(GiftKey("Toy Bear", backdrop="Onyx Black"), buy, sell, sell, 3, {}, basis)


def test_user_thresholds():
    s = UserSettings(min_profit_ton=2, min_profit_pct=5, max_buy_ton=100)
    assert s.accepts(deal(50, 53))  # +3 TON, +6%
    assert not s.accepts(deal(50, 51))  # мало TON
    assert not s.accepts(deal(100, 104))  # мало %
    assert not s.accepts(deal(150, 170))  # дороже лимита

    # По фону: важна скидка от флора, а не профит
    s = UserSettings(mode="backdrop", min_discount_pct=10)
    assert s.accepts(deal(52, 54, floor_cost=58, basis="backdrop"))  # −10.3%
    assert not s.accepts(deal(52, 54, floor_cost=57, basis="backdrop"))  # −8.8%
    assert not s.accepts(deal(50, 60))  # сравнение по модели выключено
    assert UserSettings(mode="both").wants("model") and not UserSettings(mode="model").wants("backdrop")


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
        def match(x):
            return key.id == x.key.id or (x.backdrop_key is not None and key.id == x.backdrop_key.id)
        return [x for x in self.floors if match(x)][:limit]


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


# --- поиск по фону ------------------------------------------------------------

def test_backdrop_keys():
    x = L("tonnel", "1", 52, collection="Toy Bear", model="Wizard", backdrop="Onyx Black")
    assert x.key == GiftKey("Toy Bear", "Wizard")
    assert x.backdrop_key == GiftKey("Toy Bear", backdrop="Onyx Black")
    assert L("tonnel", "2", 1).backdrop_key is None
    assert GiftKey("Toy Bear", backdrop="Onyx Black").id != GiftKey("Toy Bear", "Onyx Black").id
    assert str(GiftKey("Toy Bear", "Wizard", "Onyx Black")) == "Toy Bear — Wizard — фон Onyx Black"


def test_best_deal_floor_and_discount():
    key = GiftKey("Toy Bear", backdrop="Onyx Black")
    new = L("tonnel", "t-new", 52, 47.3, collection="Toy Bear", backdrop="Onyx Black")
    quotes = {
        "tonnel": [new, L("tonnel", "t2", 60, 54.5)],
        "portals": [L("portals", "p1", 57, 54.15), L("portals", "p2", 59, 56)],
        "mrkt": [],
    }
    op = best_deal(key, quotes, buy=new, basis="backdrop")
    assert op.floor.id == "p1" and op.discount_pct == pytest.approx((57 - 52) / 57 * 100)
    assert op.sell.id == "t2" and op.others == 3 and op.basis == "backdrop"


def test_resolve_with_backdrop():
    engine = Arbitrage({})
    engine.learn([L("portals", "1", 1, collection="Toy Bear", model="Wizard", backdrop="Onyx Black")])
    assert engine.resolve("toy bear фон onyx black") == GiftKey("Toy Bear", None, "Onyx Black")
    assert engine.resolve("Toy Bear | Wizard | Onyx Black") == GiftKey("Toy Bear", "Wizard", "Onyx Black")
    assert engine.resolve("toy bear wizard фон: onyx black") == GiftKey("Toy Bear", "Wizard", "Onyx Black")
    # Второе слово — известный фон, а не модель
    assert engine.resolve("Toy Bear | onyx black") == GiftKey("Toy Bear", None, "Onyx Black")
    assert engine.resolve("Toy Bear | wizard") == GiftKey("Toy Bear", "Wizard")
    assert engine.resolve("фон Black") is None  # без коллекции не понятно, что искать


class FakeResp:
    status_code = 200
    text = ""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return FakeResp(self.data)

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return FakeResp(self.data)

    async def close(self):
        pass


class FakeAuth:
    async def init_data(self, market):
        return "query_id=1&hash=x"

    def invalidate(self, market):
        pass


async def test_markets_send_backdrop_filter():
    import json
    from urllib.parse import parse_qs, urlparse

    from app.markets.mrkt import Mrkt
    from app.markets.portals import Portals
    from app.markets.tonnel import Tonnel

    key = GiftKey("toy bear", backdrop="onyx black")

    portals = Portals(FakeAuth())
    portals.http = FakeHttp({"results": []})
    await portals.cheapest(key)
    query = parse_qs(urlparse(portals.http.calls[0][1]).query)
    assert query["filter_by_collections"] == ["Toy Bear"] and query["filter_by_backdrops"] == ["Onyx Black"]

    tonnel = Tonnel(FakeAuth())
    tonnel.http = FakeHttp([])
    await tonnel.cheapest(key)
    flt = json.loads(tonnel.http.calls[0][2]["json"]["filter"])
    assert flt["gift_name"] == "Toy Bear" and flt["backdrop"] == {"$regex": "^Onyx Black \\("}
    assert "model" not in flt
    await tonnel.cheapest(GiftKey("Toy Bear", "Mr. (Bear)"))
    flt = json.loads(tonnel.http.calls[1][2]["json"]["filter"])
    assert flt["model"] == {"$regex": "^Mr\\. \\(Bear\\) \\("}  # спецсимволы экранированы

    mrkt = Mrkt(FakeAuth())
    mrkt.http = FakeHttp({"token": "tok", "gifts": []})
    await mrkt.cheapest(key)
    payload = mrkt.http.calls[-1][2]["json"]
    assert payload["collectionNames"] == ["Toy Bear"] and payload["backdropNames"] == ["Onyx Black"]
    assert payload["modelNames"] == []


async def test_scanner_finds_listing_below_backdrop_floor():
    """Пример из жизни: флор Toy Bear на Onyx Black — 57 TON, кто-то выставил за 51."""
    floor = L("portals", "p-floor", 57, 54.15, collection="Toy Bear", model="Wizard", backdrop="Onyx Black")
    model_floor = L("portals", "p-model", 40, 38, collection="Toy Bear", model="Sheriff", backdrop="Black")
    portals = FakeMarket("portals", [floor, model_floor])
    tonnel = FakeMarket("tonnel", [])
    engine = Arbitrage({"portals": portals, "tonnel": tonnel}, floor_ttl=60)
    found = []

    async def on_op(op):
        found.append(op)

    scanner = Scanner(
        engine, interval=0.02, on_opportunity=on_op,
        bases=lambda: {"backdrop", "model"},
        want=lambda op: op.discount_pct >= 10 if op.basis == "backdrop" else op.profit > 0,
    )
    scanner.start()
    await asyncio.sleep(0.05)
    cheap = L("tonnel", "t-cheap", 51, 46.4, collection="Toy Bear", model="Sheriff", backdrop="Onyx Black")
    tonnel.feed.insert(0, cheap)
    tonnel.floors = [cheap]
    await asyncio.sleep(0.15)
    await scanner.stop()
    for m in (portals, tonnel):
        await m.close()

    # По модели (Sheriff, флор 40) лот не интересен, а по фону — да
    assert [op.basis for op in found] == ["backdrop"]
    op = found[0]
    assert op.floor.id == "p-floor" and op.discount_pct == pytest.approx((57 - 51) / 57 * 100)

    text = format_opportunity(op, {})
    assert "Ниже флора фона на 11%" in text and "Флор фона Onyx Black" in text
    assert "всего 1" in text  # предупреждение о тонком рынке


async def test_price_check_handler_with_backdrop(tmp_path):
    from types import SimpleNamespace

    from app.handlers import price_check, set_mode, show_settings

    portals = FakeMarket("portals", [
        L("portals", "p1", 57, 54.15, collection="Toy Bear", model="Wizard", backdrop="Onyx Black"),
    ])
    tonnel = FakeMarket("tonnel", [
        L("tonnel", "t1", 52, 47.3, collection="Toy Bear", model="Sheriff", backdrop="Onyx Black"),
    ])
    engine = Arbitrage({"portals": portals, "tonnel": tonnel})
    engine.learn(portals.floors + tonnel.floors)
    sent = []

    class Msg:
        def __init__(self, text):
            self.text = text
            self.from_user = SimpleNamespace(id=1)

        async def answer(self, text, **kw):
            sent.append(text)
            return SimpleNamespace(edit_text=self.answer)

    await price_check(Msg("toy bear фон onyx black"), engine)
    result = sent[-1]
    assert "Toy Bear — фон Onyx Black" in result
    assert "Купить: <a href=\"\">tonnel</a> — <b>52.00 TON</b>" in result and "дешевле на <b>8.8%</b>" in result

    storage = Storage(str(tmp_path / "u.json"), UserSettings())
    await set_mode(Msg("/mode фон"), SimpleNamespace(args="фон", command="mode"), storage)
    assert storage.get(1).mode == "backdrop" and "по фону" in sent[-1]
    await show_settings(Msg("/settings"), storage)
    assert "Ищу: по фону" in sent[-1] and "10%" in sent[-1]
    for m in (portals, tonnel):
        await m.close()
