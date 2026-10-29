from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.arbitrage import Arbitrage, Opportunity
from app.autobuy import AutoBuyer
from app.config import Settings
from app.formatting import format_opportunity
from app.handlers import AccessMiddleware, router
from app.markets.base import Market
from app.markets.mrkt import Mrkt
from app.markets.portals import Portals
from app.markets.tonnel import Tonnel
from app.scanner import Scanner
from app.storage import Storage, UserSettings
from app.tg_auth import TelegramAuth

log = logging.getLogger("autoprsrnft")


def build_markets(cfg: Settings, auth: TelegramAuth | None) -> dict[str, Market]:
    """Создаёт клиенты только для бирж из MARKETS."""
    factories = {
        "portals": lambda: Portals(auth, cfg.proxy, sell_fee=cfg.portals_sell_fee),
        "tonnel": lambda: Tonnel(auth, cfg.proxy, buy_fee=cfg.tonnel_buy_fee, sell_fee=cfg.tonnel_sell_fee),
        "mrkt": lambda: Mrkt(auth, cfg.proxy, sell_fee=cfg.mrkt_sell_fee),
    }
    return {name: factories[name]() for name in cfg.markets}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Settings()  # type: ignore[call-arg]

    auth = TelegramAuth(cfg.session_name, cfg.api_id, cfg.api_hash)
    await auth.start()

    markets = build_markets(cfg, auth)
    log.info("Биржи: %s", ", ".join(m.title for m in markets.values()))
    engine = Arbitrage(markets, floor_ttl=cfg.floor_ttl)
    storage = Storage(
        "data/users.json",
        UserSettings(
            min_profit_ton=cfg.min_profit_ton,
            min_profit_pct=cfg.min_profit_pct,
            max_buy_ton=cfg.max_buy_ton,
            mode=cfg.default_mode,
            min_discount_pct=cfg.min_discount_pct,
        ),
    )

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def notify_owners(text: str) -> None:
        for user_id in cfg.allowed_users:
            try:
                await bot.send_message(user_id, text, disable_web_page_preview=True)
            except Exception as e:
                log.warning("Не удалось отправить %s: %s", user_id, e)

    # Автопокупка есть только на Portals; управляют ей владельцы из ALLOWED_USERS
    portals = markets.get("portals")
    autobuyer = AutoBuyer("data/autobuy.json", portals, notify_owners) if portals else None
    if autobuyer and autobuyer.settings.enabled:
        if not cfg.allowed_users:
            # Без владельца некому сообщать о покупках и некому их остановить
            autobuyer.settings.enabled = False
            autobuyer.save()
            log.warning("Автопокупка выключена: не задан ALLOWED_USERS")
        else:
            log.info("Автопокупка включена (%s)", "тест" if autobuyer.settings.test else "по-настоящему")

    dp = Dispatcher(engine=engine, storage=storage, autobuyer=autobuyer, owners=cfg.allowed_users)
    dp.update.outer_middleware(AccessMiddleware(cfg.allowed_users))
    dp.include_router(router)

    async def notify(op: Opportunity) -> None:
        log.info(
            "Сделка (%s): %s %s %.2f, флор %.2f (%+.1f%%), профит %+.2f TON",
            op.basis, op.key, op.buy.market, op.buy.buy_cost, op.floor.buy_cost, -op.discount_pct, op.profit,
        )
        text = format_opportunity(op, markets)
        for user_id, s in list(storage.users.items()):
            if s.accepts(op):
                try:
                    await bot.send_message(user_id, text, disable_web_page_preview=True)
                except Exception as e:
                    log.warning("Не удалось отправить %s: %s", user_id, e)

    def autobuy_on() -> bool:
        return autobuyer is not None and autobuyer.settings.enabled

    async def on_opportunity(op: Opportunity) -> None:
        if autobuyer is not None:
            await autobuyer.consider(op)  # сначала покупка — тут решает скорость
        await notify(op)

    def prefilter(listing) -> bool:
        # Не тратим запросы на лоты, которые не интересны ни пользователям, ни автопокупке
        if autobuy_on() and listing.market == autobuyer.market.name:
            limit = autobuyer.settings.max_price
            if not limit or listing.buy_cost <= limit:
                return True
        limits = [s.max_buy_ton for s in storage.users.values() if s.alerts]
        return any(not lim or listing.buy_cost <= lim for lim in limits)

    def bases() -> set[str]:
        found = {b for b in ("model", "backdrop") if any(s.wants(b) for s in storage.users.values())}
        if autobuy_on():
            found.add("backdrop")
        return found

    def want(op: Opportunity) -> bool:
        if autobuyer is not None and autobuyer.wants(op):
            return True
        return any(s.accepts(op) for s in storage.users.values())

    scanner = Scanner(engine, cfg.poll_interval, on_opportunity, prefilter, bases, want)
    scanner.start()
    try:
        await dp.start_polling(bot)
    finally:
        await scanner.stop()
        for m in markets.values():
            await m.close()
        await auth.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
