import asyncio
from types import SimpleNamespace

import pytest

from app.arbitrage import Arbitrage, best_deal
from app.autobuy import MAX_FAILS, AutoBuyer
from app.markets.base import MarketError
from app.models import GiftKey, Listing
from app.scanner import Scanner


def lot(id, price, market="portals", fee=0.05, number=1):
    return Listing(market, id, "Toy Bear", "Sheriff", "Onyx Black", "Star", number, price, price * (1 - fee),
                   raw={"price": f"{price:g}"})


def deal(price=51, floors=(57, 58), basis="backdrop", market="portals"):
    buy = lot("new", price, market=market)
    others = [lot(f"f{i}", p) for i, p in enumerate(floors)]
    return best_deal(GiftKey("Toy Bear", backdrop="Onyx Black"), {"portals": [buy, *others]}, buy, basis)


class FakePortals:
    name = "portals"
    title = "Portals"

    def __init__(self):
        self.bought = []
        self.fail_with = None

    async def buy(self, listing):
        if self.fail_with:
            raise self.fail_with
        self.bought.append(listing.id)

    async def balance(self):
        return 123.0


class Clock:
    t = 1_700_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def ab(tmp_path):
    sent = []

    async def notify(text):
        sent.append(text)

    buyer = AutoBuyer(str(tmp_path / "autobuy.json"), FakePortals(), notify, clock=Clock())
    buyer.sent = sent
    return buyer


def real(buyer, max_price=60, budget=100):
    s = buyer.settings
    s.enabled, s.test, s.max_price, s.daily_budget = True, False, max_price, budget


async def test_disabled_and_test_mode(ab):
    await ab.consider(deal())
    assert ab.market.bought == [] and ab.sent == []

    ab.settings.enabled = True  # по умолчанию включается тестовый режим
    await ab.consider(deal())
    await ab.consider(deal())  # тот же лот второй раз не сообщаем
    assert ab.market.bought == [] and len(ab.sent) == 1
    assert "Тест: купил бы" in ab.sent[0] and "−10.5%" in ab.sent[0]


async def test_real_buy_and_daily_budget(ab):
    assert ab.ready_for_real() is not None  # без лимитов настоящую покупку не включить
    real(ab, max_price=60, budget=100)
    assert ab.ready_for_real() is None

    await ab.consider(deal(51))
    assert ab.market.bought == ["new"] and "Купил" in ab.sent[-1] and "51.00 из 100.00" in ab.sent[-1]

    second = deal(52, floors=(58, 59))  # −10.3%, проходит по скидке
    second.buy.id = "new2"
    assert ab.skip_reason(second) == "кончился бюджет на сутки"  # 51 + 52 > 100
    assert ab.wants(second)  # сканер всё равно отдаст сделку, чтобы пришло предупреждение
    await ab.consider(second)
    await ab.consider(second)  # предупреждение не чаще раза в 6 часов
    assert sum("Бюджет на сутки" in m for m in ab.sent) == 1
    assert ab.market.bought == ["new"] and "Бюджет на сутки" in ab.sent[-1]

    ab.clock.t += 86400 + 1  # через сутки бюджет освобождается
    await ab.consider(second)
    assert ab.market.bought == ["new", "new2"]


@pytest.mark.parametrize("op, why", [
    (deal(56), "маленькая скидка"),  # −1.8%
    (deal(51, floors=(57,)), "мало лотов на фоне"),
    (deal(61, floors=(70, 72)), "дороже лимита"),
    (deal(basis="model"), "покупаем только лоты ниже флора фона"),
    (deal(market="tonnel"), "покупка поддерживается только на Portals"),
])
async def test_skip_reasons(ab, op, why):
    real(ab)
    assert ab.skip_reason(op) == why
    await ab.consider(op)
    assert ab.market.bought == []


async def test_min_profit(ab):
    real(ab)
    ab.settings.min_profit = 3.5  # перепродажа по флору 57 даёт 54.15 − 51 = 3.15
    assert ab.skip_reason(deal(51)) == "мало профита"


async def test_exchange_refusal_and_breaker(ab):
    real(ab, budget=10_000)
    ab.market.fail_with = MarketError("portals: HTTP 400: already sold")
    for i in range(MAX_FAILS):
        op = deal(51)
        op.buy.id = f"x{i}"
        await ab.consider(op)
    assert ab.spent_today() == 0  # явный отказ — деньги не ушли
    assert "already sold" in ab.sent[0]
    assert not ab.settings.enabled and "автопокупка выключена" in ab.sent[-1]


async def test_lost_response_counts_as_spent(ab):
    real(ab)
    ab.market.fail_with = TimeoutError()
    await ab.consider(deal(51))
    assert ab.spent_today() == 51 and ab.history[-1]["status"] == "unknown"
    assert "проверь покупки" in ab.sent[-1]


async def test_settings_persist(ab, tmp_path):
    real(ab, max_price=70, budget=300)
    await ab.consider(deal(51))
    again = AutoBuyer(str(tmp_path / "autobuy.json"), FakePortals(), ab.notify, clock=ab.clock)
    assert again.settings.max_price == 70 and again.settings.enabled and not again.settings.test
    assert again.spent_today() == 51 and again.history[-1]["price"] == 51
    assert "Купил" not in again.status_text(5.0) and "5.00 TON" in again.status_text(5.0)


async def test_portals_buy_request():
    from test_core import FakeAuth, FakeHttp, FakeResp

    from app.markets.portals import Portals

    portals = Portals(FakeAuth())
    portals.http = FakeHttp({})
    await portals.buy(lot("uuid-1", 51.5))
    method, url, kw = portals.http.calls[0]
    assert method == "POST" and url.endswith("/api/nfts")
    assert kw["json"] == {"nft_details": [{"id": "uuid-1", "price": "51.5"}]}
    assert kw["headers"]["Authorization"].startswith("tma ")

    class Refuse(FakeHttp):
        async def post(self, url, **kw):
            resp = FakeResp({"error": "already sold"})
            resp.status_code, resp.text = 400, "already sold"
            return resp

    portals.http = Refuse({})
    with pytest.raises(MarketError, match="already sold"):
        await portals.buy(lot("uuid-2", 10))
    await portals.close()


async def test_autobuy_command(ab):
    from app.handlers import autobuy_cmd

    replies = []

    class Msg:
        def __init__(self, uid):
            self.from_user = SimpleNamespace(id=uid)

        async def answer(self, text, **kw):
            replies.append(text)

    async def run(uid, args):
        await autobuy_cmd(Msg(uid), SimpleNamespace(args=args), ab, owners=[7])

    await run(99, "on")
    assert "только владелец" in replies[-1] and not ab.settings.enabled
    await run(7, "on")
    assert "лимит цены" in replies[-1] and not ab.settings.enabled
    await run(7, "max 60")
    await run(7, "budget 200")
    await run(7, "discount 12")
    await run(7, "others 3")
    await run(7, "on")
    s = ab.settings
    assert s.enabled and not s.test and (s.max_price, s.daily_budget, s.min_discount, s.min_others) == (60, 200, 12, 3)
    await run(7, "max abc")
    assert "Укажи число" in replies[-1]
    await run(7, "")
    assert "покупаю по-настоящему" in replies[-1] and "123.00 TON" in replies[-1]
    await run(7, "test")
    assert s.test
    await run(7, "off")
    assert not s.enabled


async def test_scanner_buys_new_listing_below_backdrop_floor(ab):
    """Целиком: новый лот на Portals дешевле флора фона → бот его покупает."""
    from test_core import FakeMarket

    floors = [lot("f1", 57), lot("f2", 58)]
    market = FakeMarket("portals", floors)
    real(ab)
    engine = Arbitrage({"portals": market}, floor_ttl=60)
    scanner = Scanner(engine, interval=0.02, on_opportunity=ab.consider,
                      bases=lambda: {"backdrop"}, want=ab.wants)
    scanner.start()
    await asyncio.sleep(0.05)
    cheap = lot("cheap", 50, number=777)
    market.feed.insert(0, cheap)
    market.floors = [cheap, *floors]
    await asyncio.sleep(0.15)
    await scanner.stop()
    await market.close()
    assert ab.market.bought == ["cheap"]
    assert "Toy Bear — Sheriff #777" in ab.sent[-1]
