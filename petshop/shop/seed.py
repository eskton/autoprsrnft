"""Примеры питомцев и товаров при первом запуске, чтобы магазин не был пустым.
Цены условные — поменяй их в админке мини-аппа."""
from __future__ import annotations

from shop.db import Database

PETS = [
    # name, emoji, description, цена ₽, принесёт ₽, дней
    ("Котик", "🐱", "Ласковый и надёжный", 500, 600, 30),
    ("Пёс", "🐶", "Верный друг, приносит каждый день", 1000, 1200, 30),
    ("Лис", "🦊", "Хитрый — приносит больше", 3000, 3600, 30),
    ("Дракон", "🐉", "Легендарный питомец", 10000, 12000, 30),
]

PRODUCTS = [
    # game, title, цена ₽, что спросить
    ("Roblox", "400 Robux", 450, "Ник в Roblox"),
    ("Roblox", "800 Robux", 850, "Ник в Roblox"),
    ("PUBG Mobile", "60 UC", 99, "ID игрока"),
    ("PUBG Mobile", "325 UC", 450, "ID игрока"),
    ("Brawl Stars", "30 гемов", 250, "Тег игрока (#...)"),
    ("Genshin Impact", "Благословение полой луны", 450, "UID"),
]


async def seed_examples(db: Database) -> None:
    async with db.tx() as t:
        if not await t.val("SELECT COUNT(*) FROM pet_types"):
            for i, (name, emoji, desc, price, ret, days) in enumerate(PETS):
                await t.run(
                    "INSERT INTO pet_types(name, emoji, description, price, total_return, duration_days, sort) "
                    "VALUES (?,?,?,?,?,?,?)",
                    name, emoji, desc, price * 100, ret * 100, days, i,
                )
        if not await t.val("SELECT COUNT(*) FROM products"):
            for i, (game, title, price, label) in enumerate(PRODUCTS):
                await t.run(
                    "INSERT INTO products(game, title, price, input_label, sort) VALUES (?,?,?,?,?)",
                    game, title, price * 100, label, i,
                )
