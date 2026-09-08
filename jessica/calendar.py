"""Сборка календаря выплат по бумагам портфелей.

Читает holdings.json, опрашивает источники и складывает всё в schedule.json —
единый список событий, с которым дальше работают правила уведомлений.
Ни одно правило не знает, откуда взялись данные: замена источника не трогает
логику рассылки.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time

from .config import Config
from .sources import smartlab

log = logging.getLogger(__name__)

COUPON, AMORTIZATION, OFFER, MATURITY = "coupon", "amortization", "offer", "maturity"


def _as_float(value) -> float | None:
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _bonds(holdings: dict) -> dict[str, dict]:
    """Уникальные облигации обоих портфелей: одна бумага может лежать в обоих."""
    bonds: dict[str, dict] = {}
    for block in holdings["portfolios"].values():
        for position in block["positions"]:
            if position["kind"] != "bond":
                continue
            existing = bonds.setdefault(
                position["isin"],
                {"isin": position["isin"], "name": position["name"], "code": position.get("code", "")},
            )
            # Биржевой код может быть указан только в одном из отчётов.
            if not existing["code"] and position.get("code"):
                existing["code"] = position["code"]
    return bonds


def build(config: Config, *, pause: float = 0.4) -> dict:
    """Опрашивает источники и пишет schedule.json.

    pause — задержка между запросами: незачем долбить чужой сайт очередью.
    """
    holdings_path = config.state_file("holdings.json")
    if not holdings_path.exists():
        raise SystemExit("Сначала нужно разобрать выгрузки: jessica import")
    holdings = json.loads(holdings_path.read_text(encoding="utf-8"))

    events: list[dict] = []
    incomplete: list[str] = []
    bonds = _bonds(holdings)

    details: dict[str, dict] = {}
    for index, bond in enumerate(bonds.values()):
        coupons = smartlab.fetch_coupons(bond["isin"], bond["code"])
        if not coupons:
            incomplete.append(bond["isin"])
            log.warning("Нет купонов по %s (%s)", bond["name"], bond["isin"])
        events.extend(coupons)

        # Погашение и оферта в купонный график не входят, а пропустить их дороже
        # всего: оферту нельзя проспать, погашение возвращает весь номинал.
        card = smartlab.fetch_details(bond["isin"], bond["code"])
        if card:
            details[bond["isin"]] = card
            nominal = _as_float(card.get("nominal"))
            for field, kind in (("maturity", MATURITY), ("offer_date", OFFER)):
                if card.get(field):
                    events.append(
                        {
                            "isin": bond["isin"],
                            "type": kind,
                            "date": card[field],
                            "amount": nominal,
                            "announced": nominal is not None,
                            "source": "smartlab",
                        }
                    )
        if index + 1 < len(bonds):
            time.sleep(pause)

    schedule = {
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
        # Даты амортизационных выплат остаются неизвестными: карточка выпуска
        # сообщает только сам факт амортизации. Он сохранён в details, и по таким
        # выпускам уведомление помечается как неполное.
        "incomplete": incomplete,
        "details": details,
        "events": sorted(events, key=lambda event: (event["date"], event["isin"])),
    }
    config.state_file("schedule.json").write_text(
        json.dumps(schedule, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return schedule


def load(config: Config) -> dict:
    path = config.state_file("schedule.json")
    if not path.exists():
        raise SystemExit("Календарь пуст: сначала выполните jessica refresh")
    return json.loads(path.read_text(encoding="utf-8"))


def upcoming(config: Config, *, days: int = 30, today: dt.date | None = None) -> list[dict]:
    """События ближайших дней, обогащённые позициями: сколько и в каком портфеле."""
    today = today or dt.date.today()
    horizon = today + dt.timedelta(days=days)
    holdings = json.loads(config.state_file("holdings.json").read_text(encoding="utf-8"))

    quantities: dict[str, list[tuple[str, float, str]]] = {}
    for block in holdings["portfolios"].values():
        for position in block["positions"]:
            quantities.setdefault(position["isin"], []).append(
                (block["title"], position["quantity"], position["name"])
            )

    result = []
    for event in load(config)["events"]:
        date = dt.date.fromisoformat(event["date"])
        if not (today <= date <= horizon):
            continue
        for title, quantity, name in quantities.get(event["isin"], []):
            result.append(
                {
                    **event,
                    "portfolio": title,
                    "quantity": quantity,
                    "name": name,
                    "total": round(event["amount"] * quantity, 2) if event["amount"] else None,
                }
            )
    return sorted(result, key=lambda item: (item["date"], item["name"]))
