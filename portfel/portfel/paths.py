"""Где лежат данные.

Код здесь, данные — рядом с данными Jessica, в `~/Documents/Tracking/Invest/`.
Портфель читает её выгрузки и состояние, но ничего туда не пишет: своя база
лежит в подпапке `portfel/`. Пути переопределяются переменными окружения —
так тесты не трогают настоящие файлы.
"""

from __future__ import annotations

import os
from pathlib import Path

JESSICA_CODE = Path.home() / "Projects" / "jessica"


def invest_home() -> Path:
    """Папка с данными Jessica (выгрузки брокеров, state/)."""
    return Path(os.environ.get("JESSICA_HOME", Path.home() / "Documents" / "Tracking" / "Invest"))


def data_dir() -> Path:
    path = Path(os.environ.get("PORTFEL_HOME", invest_home() / "portfel"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "portfel.db"


def alfa_reports() -> Path:
    return invest_home() / "alpha" / "reports"


def jessica_state(name: str) -> Path:
    return invest_home() / "state" / name
