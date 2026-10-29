from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.arbitrage import Arbitrage, Opportunity
from app.config import Settings
from app.formatting import format_opportunity
from app.handlers import AccessMiddleware, router
from app.markets.mrkt import Mrkt
from app.markets.portals import Portals
from app.markets.tonnel import Tonnel
from app.scanner import Scanner
from app.storage import Storage, UserSettings
from app.tg_auth import TelegramAuth

log = logging.getLogger("autoprsrnft")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Settings()  # type: ignore[call-arg]

    auth = TelegramAuth(cfg.session_name, cfg.api_id, cfg.api_hash)
    await auth.start()

    markets = {
        "portals": Portals(auth, cfg.proxy, sell_fee=cfg.portals_sell_fee),
        "tonnel": Tonnel(auth, cfg.proxy, buy_fee=cfg.tonnel_buy_fee, sell_fee=cfg.tonnel_sell_fee),
        "mrkt": Mrkt(auth, cfg.proxy, sell_fee=cfg.mrkt_sell_fee),
    }
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
    dp = Dispatcher(engine=engine, storage=storage)
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

    def prefilter(listing) -> bool:
        # Не тратим запросы на лоты, которые не интересны ни одному пользователю
        limits = [s.max_buy_ton for s in storage.users.values() if s.alerts]
        if not limits:
            return False
        return any(not lim or listing.buy_cost <= lim for lim in limits)

    def bases() -> set[str]:
        return {b for b in ("model", "backdrop") if any(s.wants(b) for s in storage.users.values())}

    def want(op: Opportunity) -> bool:
        return any(s.accepts(op) for s in storage.users.values())

    scanner = Scanner(engine, cfg.poll_interval, notify, prefilter, bases, want)
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
