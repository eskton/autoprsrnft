"""Минимальный клиент Crypto Pay API (@CryptoBot)."""
from __future__ import annotations

from typing import Any

import aiohttp

MAINNET = "https://pay.crypt.bot/api/"
TESTNET = "https://testnet-pay.crypt.bot/api/"


class CryptoPayError(Exception):
    def __init__(self, name: str, code: int | None = None):
        super().__init__(f"Crypto Pay: {name}" + (f" ({code})" if code else ""))
        self.name = name
        self.code = code


class CryptoPay:
    def __init__(self, token: str, testnet: bool = False, accepted_assets: str = "USDT,TON,BTC"):
        self.token = token
        self.base = TESTNET if testnet else MAINNET
        self.accepted_assets = accepted_assets
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _call(self, method: str, **params: Any) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        query = {}
        for key, value in params.items():
            if value is None:
                continue
            query[key] = str(value).lower() if isinstance(value, bool) else str(value)
        headers = {"Crypto-Pay-API-Token": self.token}
        async with self._session.get(self.base + method, params=query, headers=headers) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            err = data.get("error") or {}
            raise CryptoPayError(str(err.get("name", "UNKNOWN_ERROR")), err.get("code"))
        return data["result"]

    async def get_me(self) -> dict:
        return await self._call("getMe")

    async def create_invoice(
        self,
        amount_rub: str,
        description: str,
        payload: str,
        expires_in: int,
        return_url: str | None = None,
    ) -> dict:
        """Счёт в рублях; человек платит любой из accepted_assets по курсу CryptoBot."""
        return await self._call(
            "createInvoice",
            currency_type="fiat",
            fiat="RUB",
            amount=amount_rub,
            accepted_assets=self.accepted_assets,
            description=description[:1024],
            payload=payload,
            expires_in=expires_in,
            allow_comments=False,
            allow_anonymous=False,
            paid_btn_name="openBot" if return_url else None,
            paid_btn_url=return_url,
        )

    async def get_invoices(self, invoice_ids: list[int]) -> list[dict]:
        if not invoice_ids:
            return []
        result = await self._call(
            "getInvoices", invoice_ids=",".join(map(str, invoice_ids)), count=len(invoice_ids)
        )
        return result.get("items", []) if isinstance(result, dict) else result

    @staticmethod
    def pay_url(invoice: dict) -> str:
        return (
            invoice.get("mini_app_invoice_url")
            or invoice.get("bot_invoice_url")
            or invoice.get("pay_url")
            or ""
        )
