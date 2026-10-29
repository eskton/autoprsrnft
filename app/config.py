from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


KNOWN_MARKETS = {"portals", "tonnel", "mrkt"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram-бот (токен от @BotFather)
    bot_token: str
    # Кто может пользоваться ботом. Пусто = все (не рекомендуется).
    allowed_users: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # User-аккаунт Telegram: нужен, чтобы получать initData мини-приложений бирж.
    # api_id / api_hash берутся на https://my.telegram.org
    api_id: int
    api_hash: str
    session_name: str = "data/account"

    # Какие биржи подключать, через запятую: portals, tonnel, mrkt.
    # Сейчас стабильно работает только Portals, поэтому по умолчанию — только он.
    markets: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["portals"])

    # Необязательный прокси для запросов к биржам: http://user:pass@host:port
    proxy: str | None = None

    # Как часто опрашивать ленту новых лотов каждой биржи (секунды)
    poll_interval: float = 1.5
    # Сколько секунд держать в кеше флор по коллекции+модели
    floor_ttl: float = 20.0

    # Пороги по умолчанию для уведомлений (каждый пользователь может поменять у себя)
    min_profit_ton: float = 1.0
    min_profit_pct: float = 5.0
    max_buy_ton: float = 0.0  # 0 = без лимита
    # Что искать: model (перепродажа по флору модели), backdrop (лот ниже флора фона), both
    default_mode: Literal["model", "backdrop", "both"] = "both"
    # Насколько лот должен быть дешевле флора того же фона, %
    min_discount_pct: float = 10.0

    # Комиссии бирж (доли). Проверяй актуальные значения в самих маркетах!
    portals_sell_fee: float = 0.05   # с продавца
    tonnel_buy_fee: float = 0.10     # сверху к цене для покупателя (API отдаёт цену без неё)
    tonnel_sell_fee: float = 0.0     # с продавца
    mrkt_sell_fee: float = 0.05      # запасной вариант, если API не вернул salePriceWithoutFee

    @field_validator("markets", mode="before")
    @classmethod
    def _split_markets(cls, v):
        if isinstance(v, str):
            v = [x.strip().lower() for x in v.split(",") if x.strip()]
        unknown = sorted(set(v) - KNOWN_MARKETS)
        if unknown:
            raise ValueError(f"неизвестные биржи: {', '.join(unknown)} (есть: portals, tonnel, mrkt)")
        if not v:
            raise ValueError("укажи хотя бы одну биржу")
        return list(dict.fromkeys(v))

    @field_validator("allowed_users", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

