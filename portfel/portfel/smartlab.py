"""Кредитный рейтинг выпуска с карточки smart-lab (`/q/bonds/<ISIN>/`).

Там одна буква без агентства — сводная по рейтингам выпуска. Карточка плоская,
рейтинг ищется в тексте после «Кредитный рейтинг».
"""

from __future__ import annotations

import html
import re
import time

import requests

URL = "https://smart-lab.ru/q/bonds/{isin}/"
TIMEOUT = 20
ATTEMPTS = 2  # VPN изредка рвёт соединение
RATING = re.compile(r"Кредитный рейтинг\s+((?:AAA|AA|A|BBB|BB|B|CCC|CC|C|D)[+-]?)(?![A-Z])")


class Unavailable(Exception):
    pass


def rating(isin: str) -> str | None:
    """Рейтинг вроде «AA-» или None, если на карточке его нет."""
    problem = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(2)
        try:
            response = requests.get(URL.format(isin=isin), timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.text)))
            match = RATING.search(text)
            return match.group(1) if match else None
        except requests.RequestException as exc:
            problem = type(exc).__name__
    raise Unavailable(f"{isin}: {problem}")
