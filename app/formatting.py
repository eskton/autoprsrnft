from __future__ import annotations

from html import escape

from app.arbitrage import Opportunity, Quotes
from app.markets.base import Market
from app.models import GiftKey, Listing


def ton(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def _gift_line(x: Listing) -> str:
    name = escape(x.collection)
    if x.model:
        name += f" — {escape(x.model)}"
    if x.number is not None:
        name += f" #{x.number}"
    if x.nft_url:
        name = f'<a href="{x.nft_url}">{name}</a>'
    extras = ", ".join(escape(v) for v in (x.backdrop, x.symbol) if v)
    return f"🎁 <b>{name}</b>" + (f"\n<i>{extras}</i>" if extras else "")


def _market_link(markets: dict[str, Market], name: str) -> str:
    m = markets.get(name)
    return f'<a href="{m.app_url}">{m.title}</a>' if m else name


def quotes_table(markets: dict[str, Market], quotes: Quotes) -> str:
    lines = []
    for name, ls in quotes.items():
        title = _market_link(markets, name)
        if isinstance(ls, Exception):
            lines.append(f"• {title}: ⚠️ ошибка")
        elif not ls:
            lines.append(f"• {title}: нет лотов")
        else:
            f = ls[0]
            lines.append(
                f"• {title}: купить <b>{ton(f.buy_cost)}</b> / продать → <b>{ton(f.seller_net)}</b> TON"
            )
    return "\n".join(lines)


def format_opportunity(op: Opportunity, markets: dict[str, Market], header: str = "") -> str:
    sign = "🟢" if op.profit > 0 else "🔴"
    parts = [header] if header else []
    parts += [
        _gift_line(op.buy),
        "",
        f"🛒 Купить: {_market_link(markets, op.buy.market)} — <b>{ton(op.buy.buy_cost)} TON</b>",
        f"💸 Продать: {_market_link(markets, op.sell.market)} — флор {ton(op.sell.buy_cost)}, "
        f"на руки <b>{ton(op.sell_net)} TON</b>",
        f"{sign} Профит: <b>{op.profit:+.2f} TON ({op.pct:+.1f}%)</b>",
        "",
        "<b>Флоры:</b>",
        quotes_table(markets, op.quotes),
    ]
    return "\n".join(parts)


def format_price_check(key: GiftKey, op: Opportunity | None, quotes: Quotes, markets) -> str:
    if op is None:
        return f"🎁 <b>{escape(str(key))}</b>\n\nНичего не нашёл.\n\n" + quotes_table(markets, quotes)
    return format_opportunity(op, markets, header=f"🔎 <b>{escape(str(key))}</b>\n")
