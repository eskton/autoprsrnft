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
        ),
    )

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(engine=engine, storage=storage)
    dp.update.outer_middleware(AccessMiddleware(cfg.allowed_users))
    dp.include_router(router)

    async def notify(op: Opportunity) -> None:
        log.info("Сделка: %s %s -> %s %+.2f TON", op.key, op.buy.market, op.sell.market, op.profit)
        text = format_opportunity(op, markets, header="⚡️ <b>Арбитраж</b>\n")
        for user_id, s in list(storage.users.items()):
            if s.accepts(op.profit, op.pct, op.buy.buy_cost):
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

    scanner = Scanner(engine, cfg.poll_interval, notify, prefilter)
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
