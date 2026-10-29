from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_RARITY_RE = re.compile(r"\s*\([\d.,]+%\)\s*$")
_WORD_RE = re.compile(r"\w+(?:'\w+)?")
_ISO_RE = re.compile(r"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def parse_ts(value) -> float | None:
    """Время от биржи ('2025-07-01T12:00:00.123456Z', '...+03:00', unix) -> unix-время или None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e12 else float(value)
    m = _ISO_RE.match(str(value).strip())
    if not m:
        return None
    base, frac, tz = m.groups()
    dt = datetime.fromisoformat(base.replace(" ", "T")).replace(tzinfo=timezone.utc)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        dt -= sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[-2:]))
    return dt.timestamp() + (float(frac) if frac else 0.0)


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
    """Что сравниваем между биржами: коллекция + (необязательно) модель и/или фон."""

    collection: str
    model: str | None = None
    backdrop: str | None = None

    @property
    def id(self) -> str:
        return f"{norm(self.collection)}|{norm(self.model or '')}|{norm(self.backdrop or '')}"

    def __str__(self) -> str:
        parts = [self.collection]
        if self.model:
            parts.append(self.model)
        if self.backdrop:
            parts.append(f"фон {self.backdrop}")
        return " — ".join(parts)


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
    # Когда лот выставили / сменили цену (по часам биржи) и когда его увидел бот, unix-время
    event_at: float | None = field(default=None, compare=False)
    seen_at: float | None = field(default=None, compare=False)

    @property
    def key(self) -> GiftKey:
        """Для сравнения по модели."""
        return GiftKey(self.collection, self.model)

    @property
    def backdrop_key(self) -> GiftKey | None:
        """Для сравнения по фону (любая модель той же коллекции)."""
        return GiftKey(self.collection, backdrop=self.backdrop) if self.backdrop else None

    @property
    def nft_url(self) -> str | None:
        slug = nft_slug(self.collection, self.number)
        return f"https://t.me/nft/{slug}" if slug else None
