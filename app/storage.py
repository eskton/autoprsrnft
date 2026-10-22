from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class UserSettings:
    alerts: bool = True
    min_profit_ton: float = 1.0
    min_profit_pct: float = 5.0
    max_buy_ton: float = 0.0  # 0 = без лимита

    def accepts(self, profit: float, pct: float, buy_cost: float) -> bool:
        if not self.alerts or profit < self.min_profit_ton or pct < self.min_profit_pct:
            return False
        return not self.max_buy_ton or buy_cost <= self.max_buy_ton


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
