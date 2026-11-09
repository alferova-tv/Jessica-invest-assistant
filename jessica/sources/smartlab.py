"""Купонный график с smart-lab.

Основной источник дат и размеров купонов: страница /q/bonds/<ISIN>/coupons/
отдаёт обычную HTML-таблицу, без JS и антибота.

Оферты, погашения, номинал и признак амортизации берутся из карточки выпуска.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://smart-lab.ru/q/bonds/{isin}/coupons/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
DATE = re.compile(r"^\d{2}-\d{2}-\d{4}$")


def _to_date(value: str) -> dt.date | None:
    return dt.datetime.strptime(value, "%d-%m-%Y").date() if DATE.match(value) else None


def _to_amount(value: str) -> float | None:
    """'35.4' или '1 234,56' → float. Прочерк и пустое — купон ещё не объявлен (флоатер)."""
    cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _load(identifier: str, timeout: float, *, template: str = BASE) -> str | None:
    for attempt in range(3):
        try:
            response = requests.get(
                template.format(isin=identifier), headers={"User-Agent": UA}, timeout=timeout
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            log.debug("smart-lab не отдал %s: %s", identifier, exc)
            status = exc.response.status_code if exc.response is not None else None
            if attempt == 2 or (status is not None and status != 429 and status < 500):
                return None
            time.sleep(2 ** attempt)
    return None


def fetch_coupons(isin: str, *alternates: str, timeout: float = 20) -> list[dict]:
    """Все купоны выпуска, включая прошедшие: календарь фильтруется выше по датам.

    Идентификаторы пробуются по очереди: ОФЗ лежат на smart-lab под биржевым
    кодом (SU26253RMFS3), а не под ISIN, и берутся вторым заходом.

    Купон без суммы (флоатер, ставка ещё не объявлена) возвращается с amount=None
    и announced=False — по нему Jessica пришлёт уведомление, когда ставка появится.
    """
    for identifier in dict.fromkeys((isin, *alternates)):
        if not identifier:
            continue
        page = _load(identifier, timeout)
        if page:
            break
    else:
        log.warning("Не удалось получить купоны: %s", " / ".join(filter(None, (isin, *alternates))))
        return []

    soup = BeautifulSoup(page, "lxml")
    events: list[dict] = []
    for row in soup.find_all("tr"):
        cells = [cell.get_text(strip=True) for cell in row.find_all("td")]
        if len(cells) < 3:
            continue
        date = _to_date(cells[1])
        if date is None:
            continue
        amount = _to_amount(cells[2])
        events.append(
            {
                "isin": isin,
                "type": "coupon",
                "date": date.isoformat(),
                "amount": amount,
                "announced": amount is not None,
                "source": "smartlab",
            }
        )

    if not events:
        log.warning("По %s не нашлось ни одного купона на smart-lab", isin)
    return events


# --------------------------------------------------------- параметры выпуска

DETAIL_URL = "https://smart-lab.ru/q/bonds/{isin}/"

# Карточка выпуска выводит параметры плоским текстом, а не таблицей,
# поэтому разбираем их по подписям.
PATTERNS = {
    "maturity": re.compile(r"Дата погашения\s+(\d{2}-\d{2}-\d{4})"),
    "offer_date": re.compile(r"Дата оферты[^\d—-]*(\d{2}-\d{2}-\d{4})"),
    "offer_type": re.compile(r"Тип оферты\s*\(\?\)\s*([^Д]{3,40}?)\s+Дюрация"),
    "nominal": re.compile(r"Номинал\s+([\d.,]+)"),
}


def _plain(html: str) -> str:
    text = re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))


def fetch_details(isin: str, *alternates: str, timeout: float = 20) -> dict:
    """Погашение, оферта и признак амортизации — то, чего нет в купонном графике.

    Пропущенная оферта — самая дорогая ошибка держателя облигаций, поэтому
    эти поля важнее точности купона на копейку.
    """
    for identifier in dict.fromkeys((isin, *alternates)):
        if not identifier:
            continue
        page = _load(identifier, timeout, template=DETAIL_URL)
        if not page:
            continue

        text = _plain(page)
        details: dict = {"isin": isin, "source": "smartlab"}
        for field, pattern in PATTERNS.items():
            match = pattern.search(text)
            if match:
                details[field] = match.group(1).strip()
        if not {"maturity", "offer_date", "nominal"}.intersection(details):
            log.warning("Карточка %s не содержит распознанных параметров", identifier)
            continue

        try:
            for field in ("maturity", "offer_date"):
                if field in details:
                    details[field] = dt.datetime.strptime(details[field], "%d-%m-%Y").date().isoformat()
        except ValueError:
            log.warning("В карточке %s некорректная дата", identifier)
            continue

        if details.get("offer_type", "").lower().startswith("оферты отсутств"):
            details.pop("offer_type", None)

        details["amortized"] = "с амортизацией долга" in text.lower()
        return details
    log.warning("Не удалось получить параметры выпуска: %s", isin)
    return {}
