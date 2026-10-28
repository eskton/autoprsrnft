"""Все суммы хранятся в копейках (int), чтобы не ловить ошибки округления float."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def parse_rub(value: str | int | float) -> int:
    """'1 200,50' / '1200.5' / 1200 -> 120050 копеек."""
    text = str(value).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        rub = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Не число: {value!r}") from None
    if not rub.is_finite():
        raise ValueError(f"Не число: {value!r}")
    return int((rub * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rub_str(kop: int) -> str:
    """120050 -> '1200.50' (формат для API платёжки)."""
    sign = "-" if kop < 0 else ""
    kop = abs(kop)
    return f"{sign}{kop // 100}.{kop % 100:02d}"


def fmt_rub(kop: int) -> str:
    """120050 -> '1 200,50 ₽', 120000 -> '1 200 ₽' (для сообщений)."""
    sign = "-" if kop < 0 else ""
    kop = abs(kop)
    whole = f"{kop // 100:,}".replace(",", " ")
    frac = kop % 100
    return f"{sign}{whole}{',%02d' % frac if frac else ''} ₽"
