"""Индекс МосБиржи и официальный курс доллара — числа для утренней сводки.

Источника два, и они намеренно независимы друг от друга.

ISS Мосбиржи (`iss.moex.com`) годами не открывался из-под VPN, и вся остальная
Jessica построена так, чтобы без него обходиться. Сейчас он отвечает, но
рассчитывать на это нельзя: значение IMOEX2 взять больше неоткуда — страниц
индексов нет ни на smart-lab, ни в новостном экспорте биржи. Зато курс доллара
приходит от ЦБ и от Мосбиржи не зависит. Поэтому молчание ISS урезает сводку
наполовину, а не отменяет её целиком.

Обе функции возвращают None, если источник не ответил. Что писать вместо числа —
решает events.py: сюда логика сообщений не спускается.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time

import requests

log = logging.getLogger(__name__)

INDEX_MARKETDATA = (
    "https://iss.moex.com/iss/engines/stock/markets/index/securities/{secid}.json"
    "?iss.meta=off&iss.only=marketdata"
)
INDEX_HISTORY = (
    "https://iss.moex.com/iss/history/engines/stock/markets/index/securities/{secid}.json"
    "?iss.meta=off&iss.only=history&from={since}"
)
# Ряд, а не курс на дату: ЦБ ставит курс по рабочим дням, и запись от пятницы
# действует все выходные. «Вчерашним» считается предыдущая запись ряда,
# а не предыдущий календарный день.
CBR_DYNAMIC = (
    "https://www.cbr.ru/scripts/XML_dynamic.asp"
    "?date_req1={since}&date_req2={until}&VAL_NM_RQ={code}"
)
USD = "R01235"  # код доллара США в справочнике ЦБ

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Насколько глубоко смотреть назад в поисках предыдущего значения: длинных
# праздников в России хватает, чтобы вчера оказалось неделю назад.
LOOKBACK = dt.timedelta(days=12)


def _get(url: str, timeout: float) -> requests.Response | None:
    """Ответ или None. Разовый таймаут — повод повторить: VPN изредка моргает."""
    for attempt in range(2):
        try:
            response = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if attempt:
                log.warning("Источник не ответил (%s): %s", url.split("/")[2], exc)
            else:
                time.sleep(2)
    return None


def _rows(payload: str, block: str) -> list[dict]:
    """ISS отдаёт таблицу как columns + data — разворачиваем её в словари."""
    try:
        table = json.loads(payload)[block]
    except (ValueError, KeyError):
        return []
    return [dict(zip(table["columns"], row)) for row in table["data"]]


def fetch_index(secid: str = "IMOEX2", *, timeout: float = 3, today: dt.date | None = None) -> dict | None:
    """Текущее значение индекса и его изменение к предыдущему закрытию.

    Закрытие берётся из истории, а не из поля LASTVALUE в текущих торгах:
    у IMOEX2 они расходятся, потому что индекс считается и в вечернюю сессию,
    а история отдаёт закрытие полного торгового дня — то самое «вчера»,
    с которым человек и сравнивает.

    `live` — идут ли торги сегодня: в выходной и в праздник ISS отдаёт
    вчерашнее значение, и выдавать его за сегодняшнее нельзя.
    """
    today = today or dt.date.today()

    current = _get(INDEX_MARKETDATA.format(secid=secid), timeout)
    history = _get(INDEX_HISTORY.format(secid=secid, since=(today - LOOKBACK).isoformat()), timeout)
    if current is None and history is None:
        return None

    closes = [
        row
        for row in (_rows(history.text, "history") if history else [])
        if row.get("CLOSE") is not None
    ]
    closes.sort(key=lambda row: row["TRADEDATE"])

    market = next(iter(_rows(current.text, "marketdata") if current else []), {})
    value = market.get("CURRENTVALUE")
    date = market.get("TRADEDATE")

    # Торги ещё не начались или уже закрыты — показываем последнее закрытие.
    if value is None:
        if not closes:
            return None
        value, date = closes[-1]["CLOSE"], closes[-1]["TRADEDATE"]

    earlier = next((row for row in reversed(closes) if row["TRADEDATE"] < date), None)
    return {
        "secid": secid,
        "value": value,
        "previous": earlier["CLOSE"] if earlier else None,
        "previous_date": earlier["TRADEDATE"] if earlier else None,
        "date": date,
        "time": market.get("UPDATETIME") or "",
        "live": date == today.isoformat(),
    }


RECORD = re.compile(
    r'<Record\s+Date="(\d{2}\.\d{2}\.\d{4})".*?'
    r"<Nominal>(\d+)</Nominal>\s*<Value>([\d,.]+)</Value>",
    re.S,
)


def fetch_cbr_usd(*, timeout: float = 15, today: dt.date | None = None) -> dict | None:
    """Официальный курс доллара на сегодня и предыдущий установленный курс."""
    today = today or dt.date.today()
    response = _get(
        CBR_DYNAMIC.format(
            since=(today - LOOKBACK).strftime("%d/%m/%Y"),
            until=today.strftime("%d/%m/%Y"),
            code=USD,
        ),
        timeout,
    )
    if response is None:
        return None

    # Ответ всегда в windows-1251; кириллица здесь не нужна, но requests
    # угадывает кодировку по заголовку и на числах может ошибиться.
    body = response.content.decode("cp1251", errors="replace")

    rates: list[tuple[dt.date, float]] = []
    for date, nominal, value in RECORD.findall(body):
        try:
            rates.append(
                (
                    dt.datetime.strptime(date, "%d.%m.%Y").date(),
                    float(value.replace(",", ".")) / int(nominal),
                )
            )
        except (ValueError, ZeroDivisionError):
            continue
    if not rates:
        log.warning("ЦБ ответил, но курса доллара в ответе нет")
        return None

    rates.sort()
    date, value = rates[-1]
    return {
        "value": value,
        "previous": rates[-2][1] if len(rates) > 1 else None,
        "date": date.isoformat(),
        "previous_date": rates[-2][0].isoformat() if len(rates) > 1 else None,
    }
