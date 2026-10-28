from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from shop.bot import BotNotifier, router, setup_menu_button
from shop.config import Settings
from shop.cryptopay import CryptoPay
from shop.db import Database
from shop.payments import PaymentPoller
from shop.seed import seed_examples
from shop.service import Shop
from shop.web import create_app

log = logging.getLogger("petshop")

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Settings()  # type: ignore[call-arg]
    if not cfg.webapp_url.startswith("https://"):
        raise SystemExit("WEBAPP_URL должен начинаться с https:// — Telegram открывает мини-аппы только по https")

    db = Database(cfg.db_path)
    await db.connect()
    await seed_examples(db)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    cryptopay = CryptoPay(cfg.cryptopay_token, testnet=cfg.cryptopay_testnet, accepted_assets=cfg.cryptopay_assets)
    app_info = await cryptopay.get_me()
    log.info("Crypto Pay: приложение «%s» (%s)", app_info.get("name"), "testnet" if cfg.cryptopay_testnet else "mainnet")

    shop = Shop(
        db,
        cryptopay,
        BotNotifier(bot, cfg.admin_ids, cfg.webapp_url),
        invoice_ttl=cfg.invoice_ttl,
        max_active_invoices=cfg.max_active_invoices,
        return_url=f"https://t.me/{me.username}",
    )

    app = create_app(
        shop,
        bot_token=cfg.bot_token,
        admin_ids=cfg.admin_ids,
        static_dir=WEBAPP_DIR,
        init_data_ttl=cfg.init_data_ttl,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, cfg.web_host, cfg.web_port).start()
    log.info("Мини-апп: http://%s:%s (снаружи: %s)", cfg.web_host, cfg.web_port, cfg.webapp_url)

    poller = PaymentPoller(shop, cryptopay)
    poller.start()

    await setup_menu_button(bot, cfg.webapp_url)
    dp = Dispatcher(shop=shop, admin_ids=cfg.admin_ids, webapp_url=cfg.webapp_url)
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await poller.stop()
        await runner.cleanup()
        await cryptopay.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
