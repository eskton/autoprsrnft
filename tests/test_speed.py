"""Скорость снайпинга: лента активности, приоритет покупки, кеш флоров, адаптивный опрос."""
import asyncio
import time

import pytest
from test_core import FakeAuth, FakeHttp, FakeMarket, FakeResp, L

from app.arbitrage import Arbitrage, best_deal
from app.markets.base import RateLimited
from app.markets.portals import FeedFormatError, Portals, parse_actions
from app.models import GiftKey, parse_ts
from app.scanner import Scanner


def nft(id, price, status="listed"):
    return {
        "id": id, "name": "Toy Bear", "price": price, "status": status, "external_collection_number": 5,
        "listed_at": "2025-07-01T12:00:00Z",
        "attributes": [{"type": "model", "value": "Sheriff"}, {"type": "backdrop", "value": "Onyx Black"}],
    }


def test_parse_ts():
    assert parse_ts("2025-07-01T12:00:00Z") == 1751371200
    assert parse_ts("2025-07-01T15:00:00.5+03:00") == 1751371200.5
    assert parse_ts(1751371200000) == 1751371200
    assert parse_ts("вчера") is None and parse_ts(None) is None


def test_parse_actions_listing_and_price_drop():
    data = {"actions": [
        {"type": "listing", "created_at": "2025-07-01T12:00:01Z", "nft": nft("a", "57")},
        {"type": "price_update", "amount": "51", "created_at": "2025-07-01T12:00:02Z", "nft": nft("b", None)},
        {"type": "buy", "nft": nft("c", "40")},  # продажи пропускаем
        {"type": "listing", "nft": nft("d", "30", status="sold")},  # уже купили
    ]}
    out = parse_actions(data, sell_fee=0.05)
    assert [(x.id, x.buy_cost) for x in out] == [("a", 57.0), ("b", 51.0)]
    assert out[1].backdrop == "Onyx Black" and out[1].event_at == parse_ts("2025-07-01T12:00:02Z")
    assert out[1].raw["price"] == "51"  # при покупке отправим ровно эту цену

    with pytest.raises(FeedFormatError):
        parse_actions({"results": []}, 0.05)
    with pytest.raises(FeedFormatError):
        parse_actions({"actions": [{"type": "listing", "nft": {"weird": 1}}]}, 0.05)


class RoutedHttp(FakeHttp):
    """Отвечает по-разному на ленту активности и на поиск."""

    def __init__(self, activity, search):
        super().__init__(None)
        self.activity, self.search = activity, search

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.activity if "market/actions" in url else self.search


def resp(status, data=None):
    r = FakeResp(data or {})
    r.status_code, r.text = status, "x"
    return r


async def test_portals_feed_uses_activity_and_falls_back():
    p = Portals(FakeAuth())
    p.http = RoutedHttp(resp(200, {"actions": [{"type": "listing", "nft": nft("a", "57")}]}),
                        resp(200, {"results": [nft("s", "60")]}))
    assert [x.id for x in await p.latest()] == ["a"] and p.feed == "activity"
    assert "action_types=listing%2Cprice_update" in p.http.calls[0][1]

    p.http.activity = resp(404)  # эндпоинта нет — переходим на обычную ленту
    assert [x.id for x in await p.latest()] == ["s"] and p.feed == "search"
    calls = len(p.http.calls)
    await p.latest()  # 10 минут активность не трогаем
    assert len(p.http.calls) == calls + 1 and "nfts/search" in p.http.calls[-1][1]

    p._activity_off_until = 0
    p.http.activity = resp(429)  # лимит запросов — это не поломка ленты, падаем как обычно
    with pytest.raises(RateLimited):
        await p.latest()
    assert p._activity_off_until == 0
    await p.close()


async def test_buy_skips_queue_and_cooldown():
    p = Portals(FakeAuth())
    p.http = FakeHttp({})
    for _ in range(4):  # очередь занята фоновыми запросами
        await p._sem.acquire()
    p._cooldown_until = time.monotonic() + 30  # и биржа недавно ответила 429
    await asyncio.wait_for(p.buy(L("portals", "x", 50)), timeout=1)
    assert p.http.calls[-1][1].endswith("/api/nfts")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(p.latest(), timeout=0.2)  # обычные запросы ждут
    await p.close()


async def test_observe_keeps_cached_floor_fresh():
    key = GiftKey("Toy Bear", backdrop="Onyx Black")
    m = FakeMarket("portals", [L("portals", "f", 57, collection="Toy Bear", backdrop="Onyx Black")])
    engine = Arbitrage({"portals": m}, floor_ttl=60)
    await engine.quotes(key)
    # В ленте появился лот за 52 — флор в памяти сразу его учитывает
    engine.observe(L("portals", "n", 52, collection="Toy Bear", model="X", backdrop="Onyx Black"))
    quotes = await engine.quotes(key)
    assert [x.id for x in quotes["portals"]] == ["n", "f"] and m.cheapest_calls == 1
    # Тот же лот подешевел ещё раз — старая цена заменяется
    engine.observe(L("portals", "n", 50, collection="Toy Bear", model="X", backdrop="Onyx Black"))
    quotes = await engine.quotes(key)
    assert [(x.id, x.buy_cost) for x in quotes["portals"]] == [("n", 50), ("f", 57)]
    newcomer = L("portals", "z", 45, collection="Toy Bear", backdrop="Onyx Black")
    op = best_deal(key, quotes, newcomer, "backdrop")
    assert op.floor.id == "n"  # следующий лот после нового — уже учтённый 50, а не устаревший 57
    await m.close()


class FlakyMarket(FakeMarket):
    def __init__(self):
        super().__init__("portals", [])
        self.fail = 3

    async def _latest(self, limit):
        if self.fail:
            self.fail -= 1
            raise RateLimited("429", 429)
        return list(self.feed)


async def test_adaptive_polling_slows_down_and_recovers():
    m = FlakyMarket()
    scanner = Scanner(Arbitrage({"portals": m}), interval=0.01, on_opportunity=None, max_interval=0.08)
    seen = []
    task = asyncio.create_task(scanner._loop(m))
    for _ in range(40):
        await asyncio.sleep(0.01)
        if "portals" in scanner.delays:
            seen.append(scanner.delays["portals"])
    task.cancel()
    await m.close()
    assert max(seen) == pytest.approx(0.08)  # после 429 опрашиваем реже (до max_interval)
    assert seen[-1] < 0.05  # а потом снова ускоряемся


async def test_fast_mode_buys_without_refetch_and_tracks_lag():
    floor = L("portals", "f", 57, collection="Toy Bear", model="A", backdrop="Onyx Black")
    floor2 = L("portals", "f2", 58, collection="Toy Bear", model="B", backdrop="Onyx Black")
    m = FakeMarket("portals", [floor, floor2])
    engine = Arbitrage({"portals": m}, floor_ttl=60)
    await engine.quotes(GiftKey("Toy Bear", backdrop="Onyx Black"))  # флор уже в памяти
    calls_before = m.cheapest_calls
    found = []

    async def on_op(op):
        found.append(op)

    scanner = Scanner(engine, interval=0.01, on_opportunity=on_op, bases=lambda: {"backdrop"},
                      want=lambda op: op.discount_pct >= 10, confirm_age=lambda op: 60)
    scanner.start()
    await asyncio.sleep(0.03)
    cheap = L("portals", "c", 50, collection="Toy Bear", model="C", backdrop="Onyx Black")
    cheap.event_at = time.time() - 0.4
    m.feed.insert(0, cheap)
    await asyncio.sleep(0.05)
    await scanner.stop()
    await m.close()
    assert [op.buy.id for op in found] == ["c"]
    assert m.cheapest_calls == calls_before  # ни одного лишнего запроса флора
    assert 0.3 < scanner.lag["portals"] < 1.0


async def test_background_auth_refresh_does_not_block():
    from app.tg_auth import TelegramAuth

    auth = TelegramAuth.__new__(TelegramAuth)
    auth._cache, auth._lock = {}, asyncio.Lock()
    calls = []

    async def slow_request(market):
        calls.append(market)
        await asyncio.sleep(0.1)
        return f"data{len(calls)}"

    auth._request = slow_request
    assert await auth.init_data("portals") == "data1"
    refresh = asyncio.create_task(auth.refresh("portals"))
    await asyncio.sleep(0.01)
    # Пока идёт обновление, запросы берут старый initData и не ждут
    assert await asyncio.wait_for(auth.init_data("portals"), timeout=0.05) == "data1"
    await refresh
    assert await auth.init_data("portals") == "data2"
