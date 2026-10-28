from __future__ import annotations

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Бот от @BotFather
    bot_token: str
    # Telegram id админов (через запятую): им приходят заказы и открыта админка
    admin_ids: Annotated[list[int], NoDecode] = []
    # Адрес мини-аппа, обязательно https (например https://petshop.duckdns.org/)
    webapp_url: str

    # Crypto Pay API: @CryptoBot -> Crypto Pay -> Create App -> API Token
    cryptopay_token: str
    # true = тестовая сеть (@CryptoTestnetBot), чтобы проверить оплату без настоящих денег
    cryptopay_testnet: bool = False
    # Чем можно оплатить счёт в рублях
    cryptopay_assets: str = "USDT,TON,BTC"
    # Сколько секунд живёт счёт на оплату
    invoice_ttl: int = 3600
    # Сколько неоплаченных счетов одновременно может быть у одного человека
    max_active_invoices: int = 5

    web_host: str = "127.0.0.1"
    web_port: int = 8080
    db_path: str = "data/shop.db"
    # Сколько секунд действительна авторизация мини-аппа (initData)
    init_data_ttl: int = 86400

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v
