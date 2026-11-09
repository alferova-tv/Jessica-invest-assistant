"""Торговый календарь Мосбиржи: копия в проекте и обновление раз в полгода.

Источники и дата проверки указаны в data/trading_calendar.json. Неизвестные
даты не выводятся из номера дня недели. Дата сессии ДСВД хранится отдельно.
"""

from __future__ import annotations

import datetime as dt
import calendar
import json
import logging
import tempfile
from pathlib import Path

from .market import _get

URL = (
    "https://iss.moex.com/iss/calendars.json?iss.meta=off&iss.only=off_days"
    "&show_all_days=1&from={since}&till={until}"
)
log = logging.getLogger(__name__)
BUNDLED = Path(__file__).resolve().parents[1] / "data" / "trading_calendar.json"


def six_months_later(day: dt.date) -> dt.date:
    month = day.month - 1 + 6
    year, month = day.year + month // 12, month % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def parse(payload: dict) -> dict[str, dict]:
    table = payload["off_days"]
    columns = table["columns"]
    if not {"tradedate", "stock_workday", "stock_trade_session_date"}.issubset(columns):
        raise ValueError("в календаре нет необходимых полей фондового рынка")
    days = {}
    for values in table["data"]:
        if len(values) != len(columns):
            raise ValueError("длина строки календаря не совпадает с заголовком")
        row = dict(zip(columns, values))
        day = dt.date.fromisoformat(row["tradedate"]).isoformat()
        flag = row.get("stock_workday")
        if flag not in (0, 1, None):
            raise ValueError("неизвестный признак торгового дня")
        session = row.get("stock_trade_session_date")
        if session:
            session = dt.date.fromisoformat(session).isoformat()
        if day in days or (flag == 0 and session) or (session and session < day):
            raise ValueError("противоречивые даты календаря")
        days[day] = {"date": day, "is_traded": None if flag is None else bool(flag),
                     "session_date": session, "reason": row.get("stock_reason")}
    if not days:
        raise ValueError("календарь не содержит дат")
    return days


def _load(path: Path) -> dict:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored.get("version") != 1 or stored.get("source") != "moex":
            return {}
        parse(stored["payload"])
        stamp = dt.datetime.fromisoformat(stored["verified_at"])
        if stamp.tzinfo is None:
            return {}
        return stored
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _save(path: Path, stored: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as fh:
        temporary = Path(fh.name)
        json.dump(stored, fh, ensure_ascii=False, indent=2)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _fetch(since: dt.date, *, timeout: float) -> dict[str, dict]:
    """Шесть месяцев, запросами не длиннее 31 дня и без пересечения года."""
    until = six_months_later(since) - dt.timedelta(days=1)
    days = {}
    while since <= until:
        end = min(since + dt.timedelta(days=30), dt.date(since.year, 12, 31), until)
        response = _get(URL.format(since=since.isoformat(), until=end.isoformat()), timeout)
        if response is None:
            raise ValueError("источник не ответил после повторных запросов")
        chunk = parse(json.loads(response.text))
        date = since
        while date <= end:
            if date.isoformat() not in chunk:
                raise ValueError("источник вернул неполный период календаря")
            date += dt.timedelta(days=1)
        days.update(chunk)
        since = end + dt.timedelta(days=1)
    return days


def get_day(day: dt.date, cache_file: Path, *, timeout: float = 3) -> dict | None:
    """Копия из проекта, затем проверенные обновления Мосбиржи.

    Плановая проверка раз в шесть календарных месяцев. Если дата неизвестна
    или обновление не удалось, повторяем запрос не чаще одного раза в сутки.
    Недоступность API не стирает локальный календарь и не подменяет пропуски.
    """
    now = dt.datetime.now(dt.timezone.utc)
    bundled, cached = _load(BUNDLED), _load(cache_file)
    stored = max((s for s in (bundled, cached) if s),
                 key=lambda s: dt.datetime.fromisoformat(s["verified_at"]), default={})
    days = parse(stored["payload"]) if stored else {}
    known = days.get(day.isoformat())
    verified = dt.datetime.fromisoformat(stored["verified_at"]).date() if stored else None
    due = verified is None or now.date() >= six_months_later(verified)
    missing = not known or known["is_traded"] is None
    attempt = cached.get("last_attempt_at")
    try:
        last_attempt = dt.datetime.fromisoformat(attempt) if attempt else None
        recent_attempt = last_attempt is not None and dt.timedelta() <= now - last_attempt < dt.timedelta(days=1)
    except (ValueError, TypeError):
        recent_attempt = False
    if (due or missing) and not recent_attempt:
        try:
            fresh = _fetch(now.date(), timeout=timeout)
            for date, row in fresh.items():
                if row["is_traded"] is not None or date not in days:
                    days[date] = row
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Календарь Мосбиржи не обновлён (%s); сохранённые даты остаются доступны", type(exc).__name__)
        else:
            stored = {**stored, "verified_at": now.isoformat(),
                      "updated_from": "https://iss.moex.com/iss/calendars.json"}
        stored = {**stored, "version": 1, "source": "moex", "last_attempt_at": now.isoformat(),
                  "payload": {"off_days": {
                      "columns": ["tradedate", "stock_workday", "stock_trade_session_date", "stock_reason"],
                      "data": [[date, None if row["is_traded"] is None else int(row["is_traded"]), row["session_date"], row["reason"]]
                               for date, row in sorted(days.items())],
                  }}}
        if stored.get("verified_at"):
            _save(cache_file, stored)
        known = days.get(day.isoformat())
    if not known or known["is_traded"] is None:
        log.warning("Календарь Мосбиржи: торговый статус %s неизвестен", day)
    return known
