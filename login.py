"""Одноразовый вход в user-аккаунт Telegram: создаёт файл сессии data/account.session.

Запуск: python login.py  (спросит номер телефона, код и пароль 2FA, если он есть)
"""
import asyncio
from pathlib import Path

from telethon import TelegramClient

from app.config import Settings


async def main() -> None:
    cfg = Settings()  # type: ignore[call-arg]
    Path(cfg.session_name).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Готово! Вошли как {me.first_name} (id {me.id}). Сессия: {cfg.session_name}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
