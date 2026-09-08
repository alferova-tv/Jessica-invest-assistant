"""Загрузка конфигурации и пути к данным.

Данные и секреты живут отдельно от кода: код можно снести и переустановить,
папка с данными переживёт это без потерь.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import yaml

DEFAULT_HOME = Path.home() / "Documents" / "Tracking" / "Invest"


def home() -> Path:
    """Папка с данными. Переопределяется переменной JESSICA_HOME — нужно для тестов."""
    return Path(os.environ.get("JESSICA_HOME", DEFAULT_HOME))


@dataclass(frozen=True)
class Portfolio:
    key: str
    title: str
    reports_dir: Path


class Config:
    def __init__(self, raw: dict, root: Path):
        self._raw = raw
        self.root = root

    @classmethod
    def load(cls, root: Path | None = None) -> "Config":
        root = root or home()
        path = root / "config.yaml"
        if not path.exists():
            raise SystemExit(f"Не найден конфиг: {path}")
        with path.open(encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh) or {}, root)

    @property
    def token(self) -> str:
        return self._raw["telegram"]["token"]

    @property
    def chat_id(self) -> int:
        return int(self._raw["telegram"]["chat_id"])

    @cached_property
    def portfolios(self) -> list[Portfolio]:
        return [
            Portfolio(key=key, title=spec["title"], reports_dir=self.root / spec["reports_dir"])
            for key, spec in self._raw.get("portfolios", {}).items()
        ]

    def reminders(self, event_type: str) -> list[int]:
        """За сколько дней до события напоминать. Неизвестный тип — только в день события."""
        return self._raw.get("reminders", {}).get(event_type, [0])

    @property
    def moex_timeout(self) -> float:
        return float(self._raw.get("sources", {}).get("moex_timeout_sec", 3))

    @property
    def morning_hour(self) -> int:
        return int(self._raw.get("schedule", {}).get("morning_hour", 9))

    # Пути к состоянию — собраны здесь, чтобы модули не выдумывали их каждый по-своему.
    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def digests_dir(self) -> Path:
        return self.root / "digests"

    def state_file(self, name: str) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir / name
