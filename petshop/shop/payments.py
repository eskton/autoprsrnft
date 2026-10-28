"""Фоновая проверка счетов в CryptoBot: оплаченные — выдаём, истёкшие — отменяем."""
from __future__ import annotations

import asyncio
import logging

from shop.cryptopay import CryptoPay
from shop.service import Shop

log = logging.getLogger(__name__)


class PaymentPoller:
    def __init__(self, shop: Shop, cryptopay: CryptoPay, interval: float = 4.0):
        self.shop = shop
        self.cp = cryptopay
        self.interval = interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="payment-poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        errors = 0
        while True:
            try:
                await self.tick()
                errors = 0
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                errors += 1
                delay = min(60, self.interval * 2**errors)
                log.warning("Ошибка проверки оплат (%s), повтор через %.0fс", e, delay)
                await asyncio.sleep(delay)

    async def tick(self) -> None:
        await self.shop.cleanup_pending()
        ids = await self.shop.active_invoice_ids()
        for i in range(0, len(ids), 100):
            for inv in await self.cp.get_invoices(ids[i : i + 100]):
                if inv.get("status") == "paid":
                    await self.shop.on_invoice_paid(int(inv["invoice_id"]))
                elif inv.get("status") == "expired":
                    await self.shop.on_invoice_expired(int(inv["invoice_id"]))
