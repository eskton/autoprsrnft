from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


MODES = {"model": "по модели", "backdrop": "по фону", "both": "по модели и по фону"}


@dataclass
class UserSettings:
    alerts: bool = True
    # Сравнение по модели: шлём, если перепродажа даёт столько профита
    min_profit_ton: float = 1.0
    min_profit_pct: float = 5.0
    max_buy_ton: float = 0.0  # 0 = без лимита
    # model / backdrop / both
    mode: str = "both"
    # Сравнение по фону: шлём, если лот дешевле флора того же фона на столько %
    min_discount_pct: float = 10.0

    def wants(self, basis: str) -> bool:
        return self.alerts and self.mode in (basis, "both")

    def accepts(self, op) -> bool:
        """op — app.arbitrage.Opportunity."""
        if not self.wants(op.basis):
            return False
        if self.max_buy_ton and op.buy.buy_cost > self.max_buy_ton:
            return False
        if op.basis == "backdrop":
            return op.discount_pct >= self.min_discount_pct
        return op.profit >= self.min_profit_ton and op.pct >= self.min_profit_pct


class Storage:
    """Настройки пользователей в JSON-файле (на одного-нескольких человек этого достаточно)."""

    def __init__(self, path: str, defaults: UserSettings):
        self.path = Path(path)
        self.defaults = defaults
        self.users: dict[int, UserSettings] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text("utf-8"))
            self.users = {int(k): UserSettings(**v) for k, v in raw.items()}

    def get(self, user_id: int) -> UserSettings:
        if user_id not in self.users:
            self.users[user_id] = UserSettings(**asdict(self.defaults))
            self.save()
        return self.users[user_id]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self.users.items()}, indent=2), "utf-8")
        os.replace(tmp, self.path)
