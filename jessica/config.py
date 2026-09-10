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
    # Папка выгрузок. У портфеля, который берётся из API брокера, её нет.
    reports_dir: Path | None
    # Откуда брать состав: "reports" — брокерские выгрузки, "tinvest" — T-Invest API.
    source: str = "reports"


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
            Portfolio(
                key=key,
                title=spec["title"],
                reports_dir=self.root / spec["reports_dir"] if spec.get("reports_dir") else None,
                source=spec.get("source", "reports"),
            )
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

    @property
    def market_hour(self) -> int:
        """Час сводки по рынку. Отдельный от morning_hour: события дня приходят
        раньше открытия торгов, а индексу до открытия взяться неоткуда."""
        return int(self._raw.get("schedule", {}).get("market_hour", 10))

    @property
    def market_weekends(self) -> bool:
        """Слать ли сводку в субботу и воскресенье. По умолчанию нет: торгов
        не было, и пришло бы то же пятничное число третий раз подряд."""
        return bool(self._raw.get("schedule", {}).get("market_weekends", False))

    @property
    def tinvest_token(self) -> str:
        """Read-only токен T-Invest API. Пустая строка — токен не задан."""
        return str((self._raw.get("tinvest") or {}).get("token") or "")

    @property
    def tinvest_account(self) -> str | None:
        """Номер брокерского счёта. Обычно не нужен: счёт один, его находит API."""
        account = (self._raw.get("tinvest") or {}).get("account")
        return str(account) if account else None

    @property
    def refresh_retry_hours(self) -> int:
        """Как часто `run` повторяет сбор состава, если API брокера не ответил."""
        return int(self._raw.get("schedule", {}).get("refresh_retry_hours", 3))

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
