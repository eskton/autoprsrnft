"""Автопокупка на Portals лотов, которые дешевле флора своего фона.

Предохранители:
- тестовый режим: бот только пишет, что купил бы;
- лимит цены за подарок и бюджет на сутки (без них реальная покупка не включается);
- не покупаем, если на фоне слишком мало других лотов (флор может быть случайным);
- после нескольких ошибок подряд автопокупка выключается сама.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from html import escape
from pathlib import Path
from typing import Awaitable, Callable

from app.arbitrage import Opportunity
from app.markets.base import MarketError

log = logging.getLogger(__name__)

DAY = 86400
# Быстрый режим: решаем по флору из памяти, если его запрашивали не раньше стольких секунд назад
# (новые лоты и снижения цены из ленты попадают в этот флор сразу)
FAST_FLOOR_AGE = 60.0
BUDGET_SPENT = "кончился бюджет на сутки"
# Проиграть гонку другому снайперу — нормально, поэтому выключаемся не после первой ошибки
MAX_FAILS = 5


@dataclass
class AutoBuySettings:
    enabled: bool = False
    test: bool = True  # только писать, что купил бы
    max_price: float = 0.0  # TON за один подарок
    daily_budget: float = 0.0  # TON за последние 24 часа
    min_discount: float = 10.0  # % ниже флора фона
    min_others: int = 2  # сколько ещё лотов должно быть на этом фоне
    min_profit: float = 0.0  # TON на руки после комиссии при перепродаже по флору
    fast: bool = True  # не перепроверять свежий флор перед покупкой


def _fmt(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def describe(op: Opportunity) -> str:
    x = op.buy
    name = escape(x.collection) + (f" — {escape(x.model)}" if x.model else "") + (
        f" #{x.number}" if x.number is not None else ""
    )
    if x.nft_url:
        name = f'<a href="{x.nft_url}">{name}</a>'
    return (
        f"{name}, фон {escape(x.backdrop or '?')}\n"
        f"Цена {_fmt(x.buy_cost)} TON, флор фона {_fmt(op.floor.buy_cost)} (−{op.discount_pct:.1f}%), "
        f"перепродажа по флору: {op.profit:+.2f} TON"
    )


def timing(x, sent_at: float | None = None, done_at: float | None = None) -> str:
    """⏱ через сколько после выставления увидели лот и сколько ушло на покупку."""
    parts = []
    if x.event_at and x.seen_at:
        parts.append(f"увидел через {max(0.0, x.seen_at - x.event_at):.2f} с после выставления")
    if sent_at and x.seen_at:
        parts.append(f"решил за {max(0.0, sent_at - x.seen_at):.2f} с")
    if sent_at and done_at:
        parts.append(f"ответ биржи {done_at - sent_at:.2f} с")
    return "⏱ " + ", ".join(parts) if parts else ""


class AutoBuyer:
    def __init__(
        self,
        path: str,
        market,
        notify: Callable[[str], Awaitable[None]],
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path)
        self.market = market  # Portals: умеет buy() и balance()
        self.notify = notify
        self.clock = clock
        self.settings = AutoBuySettings()
        self.spent: list[tuple[float, float]] = []  # (время, TON) за последние сутки
        self.history: list[dict] = []  # последние покупки для /autobuy
        self._attempted: set[str] = set()
        self._fails = 0
        self._budget_warned_at = 0.0
        self._lock = asyncio.Lock()
        self._load()

    # --- хранение -----------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text("utf-8"))
        known = {f.name for f in fields(AutoBuySettings)}
        self.settings = AutoBuySettings(**{k: v for k, v in raw.get("settings", {}).items() if k in known})
        self.spent = [tuple(x) for x in raw.get("spent", [])]
        self.history = raw.get("history", [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"settings": asdict(self.settings), "spent": self.spent, "history": self.history[-30:]}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.path)

    # --- проверки -----------------------------------------------------------

    def spent_today(self) -> float:
        since = self.clock() - DAY
        self.spent = [(t, p) for t, p in self.spent if t >= since]
        return sum(p for _, p in self.spent)

    def ready_for_real(self) -> str | None:
        """Почему нельзя включить реальную покупку (или None, если можно)."""
        s = self.settings
        if s.max_price <= 0:
            return "Сначала задай лимит цены: /autobuy max 60"
        if s.daily_budget <= 0:
            return "Сначала задай бюджет на сутки: /autobuy budget 200"
        return None

    def skip_reason(self, op: Opportunity) -> str | None:
        s = self.settings
        x = op.buy
        if not s.enabled:
            return "выключена"
        if op.basis != "backdrop":
            return "покупаем только лоты ниже флора фона"
        if x.market != self.market.name:
            return f"покупка поддерживается только на {self.market.title}"
        if x.id in self._attempted:
            return "уже пробовали"
        if s.max_price > 0 and x.buy_cost > s.max_price:
            return "дороже лимита"
        if s.max_price <= 0 and not s.test:
            return "не задан лимит цены"
        if op.discount_pct < s.min_discount:
            return "маленькая скидка"
        if op.others < s.min_others:
            return "мало лотов на фоне"
        if op.profit <= s.min_profit:
            return "мало профита"
        if not s.test and s.daily_budget > 0 and self.spent_today() + x.buy_cost > s.daily_budget:
            return BUDGET_SPENT
        return None

    def reset_failures(self) -> None:
        self._fails = 0

    def wants(self, op: Opportunity) -> bool:
        # Сделку без бюджета тоже берём, чтобы сообщить владельцу, что бюджет кончился
        return self.skip_reason(op) in (None, BUDGET_SPENT)

    # --- покупка ------------------------------------------------------------

    async def consider(self, op: Opportunity) -> None:
        if await self._skip(op):
            return
        async with self._lock:  # покупки строго по одной, чтобы не перерасходовать бюджет
            if await self._skip(op):  # пока ждали очередь, бюджет мог кончиться
                return
            self._attempted.add(op.buy.id)
            if self.settings.test:
                await self.notify(f"🧪 <b>Тест: купил бы</b>\n{describe(op)}\n{timing(op.buy, time.time())}")
                return
            await self._buy(op)

    async def _skip(self, op: Opportunity) -> bool:
        reason = self.skip_reason(op)
        if reason == BUDGET_SPENT:
            await self._warn_budget()
        return reason is not None

    async def _buy(self, op: Opportunity) -> None:
        x = op.buy
        sent_at = time.time()
        try:
            await self.market.buy(x)
        except MarketError as e:
            # Биржа явно отказала (лот уже купили, сменилась цена, не хватает баланса) — денег не списали
            await self._failed(op, str(e), timing(x, sent_at, time.time()))
            return
        except Exception as e:
            # Ответ не дошёл: покупка могла пройти. Считаем деньги потраченными, чтобы не превысить бюджет
            self._record(op, "unknown")
            await self._failed(op, f"нет ответа от биржи ({type(e).__name__}) — проверь покупки в Portals")
            return
        done_at = time.time()
        self._fails = 0
        self._record(op, "bought")
        log.info("Автопокупка: %s за %.2f TON (%s)", x.id, x.buy_cost, timing(x, sent_at, done_at))
        await self.notify(
            f"✅ <b>Купил</b>\n{describe(op)}\n{timing(x, sent_at, done_at)}\n"
            f"Потрачено за сутки: {_fmt(self.spent_today())} из {_fmt(self.settings.daily_budget)} TON"
        )

    def _record(self, op: Opportunity, status: str) -> None:
        x = op.buy
        self.spent.append((self.clock(), x.buy_cost))
        self.history.append({
            "ts": int(self.clock()),
            "status": status,
            "id": x.id,
            "name": f"{x.collection} — {x.model}" + (f" #{x.number}" if x.number is not None else ""),
            "backdrop": x.backdrop,
            "price": x.buy_cost,
            "floor": op.floor.buy_cost,
        })
        self.save()

    async def _failed(self, op: Opportunity, reason: str, timing_line: str = "") -> None:
        self._fails += 1
        log.warning("Автопокупка не удалась (%s): %s %s", op.buy.id, reason, timing_line)
        text = f"❌ <b>Не купил</b>\n{describe(op)}\nПричина: {escape(reason)}"
        if timing_line:
            text += f"\n{timing_line}"
        if self._fails >= MAX_FAILS:
            self.settings.enabled = False
            self.save()
            text += f"\n\n⛔️ {MAX_FAILS} ошибок подряд — автопокупка выключена. Включить: /autobuy on"
        await self.notify(text)

    async def _warn_budget(self) -> None:
        # Не чаще раза в 6 часов
        if self.clock() - self._budget_warned_at < 6 * 3600:
            return
        self._budget_warned_at = self.clock()
        await self.notify(
            f"💰 Бюджет на сутки ({_fmt(self.settings.daily_budget)} TON) потрачен — "
            "пропускаю покупки, пока не освободится. Поднять: /autobuy budget 300"
        )

    def status_text(self, balance: float | None) -> str:
        s = self.settings
        if not s.enabled:
            state = "⏸ выключена"
        elif s.test:
            state = "🧪 тестовый режим (ничего не покупаю, только пишу)"
        else:
            state = "🟢 покупаю по-настоящему"
        lines = [
            f"<b>Автопокупка на {self.market.title}</b>: {state}",
            "",
            f"Покупаю лоты дешевле флора фона на <b>{s.min_discount:g}%</b>+",
            f"Лимит за подарок: <b>{_fmt(s.max_price) + ' TON' if s.max_price else 'не задан'}</b>",
            f"Бюджет на сутки: <b>{_fmt(s.daily_budget) + ' TON' if s.daily_budget else 'не задан'}</b>, "
            f"потрачено {_fmt(self.spent_today())}",
            f"Мин. других лотов на фоне: {s.min_others}, мин. профит: {s.min_profit:g} TON",
            "Быстрый режим: " + (
                f"вкл — решаю по флору из памяти (до {FAST_FLOOR_AGE:.0f} с), без лишнего запроса"
                if s.fast else "выкл — перед каждой покупкой перепроверяю флор (медленнее)"
            ),
        ]
        if balance is not None:
            lines.append(f"Баланс {self.market.title}: <b>{_fmt(balance)} TON</b>")
        if self.history:
            lines.append("\nПоследние покупки:")
            for h in self.history[-5:][::-1]:
                mark = "✅" if h["status"] == "bought" else "❓"
                lines.append(f"{mark} {escape(h['name'])} ({escape(h.get('backdrop') or '')}) — {_fmt(h['price'])} TON")
        lines += [
            "",
            "/autobuy test — тестовый режим  •  /autobuy on — покупать  •  /autobuy off",
            "/autobuy max 60 — лимит за подарок, TON",
            "/autobuy budget 200 — бюджет на сутки, TON",
            "/autobuy discount 10 — скидка от флора фона, %",
            "/autobuy others 2 — сколько ещё лотов должно быть на фоне",
            "/autobuy profit 1 — мин. профит при перепродаже, TON",
            "/autobuy fast on / off — быстрый режим",
        ]
        return "\n".join(lines)
