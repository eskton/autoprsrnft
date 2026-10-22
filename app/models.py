from __future__ import annotations

import re
from dataclasses import dataclass, field

_RARITY_RE = re.compile(r"\s*\([\d.,]+%\)\s*$")
_WORD_RE = re.compile(r"\w+(?:'\w+)?")


def strip_rarity(name: str) -> str:
    """'Wizard (1.5%)' -> 'Wizard' (Tonnel добавляет редкость к названию)."""
    return _RARITY_RE.sub("", name or "").strip()


def norm(name: str) -> str:
    """Ключ для сравнения названий между биржами: без редкости, регистра и пунктуации."""
    return re.sub(r"[^0-9a-z]", "", strip_rarity(name).lower())


def title(text: str) -> str:
    """Капитализирует слова, как это делают сами маркеты: 'plush pepe' -> 'Plush Pepe'."""
    for word in _WORD_RE.findall(text):
        text = text.replace(word, word[0].upper() + word[1:], 1)
    return text


def canon(text: str) -> str:
    """Название для фильтра биржи. Названия из лотов уже точные ('Jack-in-the-Box'),
    а введённые руками в нижнем регистре приводим к виду 'Plush Pepe'."""
    text = text.strip()
    return text if any(c.isupper() for c in text) else title(text)


def nft_slug(collection: str, number: int | None) -> str | None:
    """Слаг для ссылки t.me/nft/<slug>: 'Jack-in-the-Box', 7 -> 'JackInTheBox-7'."""
    if number is None:
        return None
    return re.sub(r"[^0-9A-Za-z]", "", title(collection)) + f"-{number}"


@dataclass(frozen=True)
class GiftKey:
    """Что сравниваем между биржами: коллекция + (необязательно) модель."""

    collection: str
    model: str | None = None

    @property
    def id(self) -> str:
        return f"{norm(self.collection)}|{norm(self.model or '')}"

    def __str__(self) -> str:
        return f"{self.collection} — {self.model}" if self.model else self.collection


@dataclass
class Listing:
    market: str
    id: str
    collection: str
    model: str | None
    backdrop: str | None
    symbol: str | None
    number: int | None
    # Сколько заплатит покупатель с учётом всех комиссий, TON
    buy_cost: float
    # Сколько получит продавец, если лот продастся по этой цене, TON
    seller_net: float
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self) -> GiftKey:
        return GiftKey(self.collection, self.model)

    @property
    def nft_url(self) -> str | None:
        slug = nft_slug(self.collection, self.number)
        return f"https://t.me/nft/{slug}" if slug else None
