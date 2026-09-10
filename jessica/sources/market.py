"""Индекс МосБиржи и официальный курс доллара — числа для утренней сводки.

Индекс и курс намеренно независимы: курс приходит от ЦБ и от Мосбиржи не
зависит, поэтому молчание источников индекса урезает сводку наполовину,
а не отменяет её целиком.

Сам индекс берётся из трёх источников по очереди, пока один не ответит:

1. ISS Мосбиржи (`iss.moex.com`) — родной источник, реальное время. Из-под
   европейского VPN режется по IP выходного узла, и узел этот у VPN гуляет:
   в один час запрос проходит, в другой рвётся на TLS.
2. Alor OpenAPI (`api.alor.ru`) — тот же IMOEX2 дневными свечами, без ключа.
   Данные отстают минут на пятнадцать — для сводки в десять утра это не важно.
3. smart-lab (главная страница) — последний рубеж. Там только IMOEX, не
   IMOEX2, и без точного времени; на него падаем, лишь когда молчат оба первых.

Обе публичные функции возвращают None, если источник не ответил. Что писать
вместо числа — решает events.py: сюда логика сообщений не спускается.
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
# Alor отдаёт свечи; дневных за две недели хватает и на текущее значение
# (сегодняшняя свеча ещё формируется), и на предыдущее закрытие.
ALOR_HISTORY = (
    "https://api.alor.ru/md/v2/history"
    "?symbol={secid}&exchange=MOEX&tf=D&from={since}&to={until}"
)
SMARTLAB_HOME = "https://smart-lab.ru/"
# Строка индекса в таблице в шапке главной smart-lab: значение и процент,
# но не абсолютное вчерашнее закрытие и не дата.
SMARTLAB_ROW = re.compile(
    r'/gr/MOEX\.IMOEX"[^>]*>IMOEX</a>\s*</td>\s*'
    r"<td[^>]*>([\d\s,]+)</td>\s*"
    r"<td[^>]*>([-+]?[\d,]+)\s*%"
)
FALLBACK_TIMEOUT = 10

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

    ISS, затем Alor, затем smart-lab — первый ответивший и выигрывает.
    Возвращаемый словарь одинаков у всех трёх; поле `secid` говорит, чей
    именно индекс в нём лежит (у smart-lab это IMOEX, а не IMOEX2).

    `live` — идут ли торги сегодня: в выходной и в праздник источник отдаёт
    вчерашнее значение, и выдавать его за сегодняшнее нельзя.
    """
    today = today or dt.date.today()
    return (
        _index_iss(secid, timeout, today)
        or _index_alor(secid, today)
        or _index_smartlab(today)
    )


def _index_iss(secid: str, timeout: float, today: dt.date) -> dict | None:
    """Родной источник. Закрытие берётся из истории, а не из поля LASTVALUE
    в текущих торгах: у IMOEX2 они расходятся, потому что индекс считается
    и в вечернюю сессию, а история отдаёт закрытие полного торгового дня —
    то самое «вчера», с которым человек и сравнивает.
    """
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


def _index_alor(secid: str, today: dt.date) -> dict | None:
    """Тот же IMOEX2 из Alor OpenAPI. Дневные свечи: последняя (сегодняшняя,
    ещё формируется) — текущее значение, предыдущая с другой датой — «вчера».
    Данные отстают минут на пятнадцать; точного времени свеча не несёт.
    """
    since = int(dt.datetime.combine(today - LOOKBACK, dt.time(), dt.timezone.utc).timestamp())
    until = int(dt.datetime.now(dt.timezone.utc).timestamp())
    response = _get(ALOR_HISTORY.format(secid=secid, since=since, until=until), FALLBACK_TIMEOUT)
    if response is None:
        return None
    try:
        candles = json.loads(response.text)["history"]
    except (ValueError, KeyError, TypeError):
        return None

    days = sorted(
        (dt.datetime.fromtimestamp(c["time"], dt.timezone.utc).date(), c["close"])
        for c in candles
        if c.get("close") is not None
    )
    if not days:
        return None

    date, value = days[-1]
    earlier = next((day for day in reversed(days) if day[0] < date), None)
    return {
        "secid": secid,
        "value": value,
        "previous": earlier[1] if earlier else None,
        "previous_date": earlier[0].isoformat() if earlier else None,
        "date": date.isoformat(),
        "time": "",
        "live": date == today,
    }


def _index_smartlab(today: dt.date) -> dict | None:
    """Последний рубеж: строка индекса в шапке главной smart-lab. Там IMOEX,
    не IMOEX2, и без вечерней сессии. Абсолютного вчерашнего закрытия страница
    не даёт — восстанавливаем его из процента (тот округлён до 0,1 %),
    а дату закрытия принимаем за предыдущий будний день.
    """
    response = _get(SMARTLAB_HOME, FALLBACK_TIMEOUT)
    if response is None:
        return None
    match = SMARTLAB_ROW.search(response.text)
    if match is None:
        log.warning("smart-lab: строки IMOEX в шапке главной не нашлось")
        return None

    value = float(re.sub(r"\s", "", match.group(1)).replace(",", "."))
    percent = float(match.group(2).replace(",", "."))
    previous = value / (1 + percent / 100) if percent else value

    prev_day = today - dt.timedelta(days=1)
    while prev_day.weekday() >= 5:
        prev_day -= dt.timedelta(days=1)
    return {
        "secid": "IMOEX",  # не IMOEX2: на smart-lab его нет
        "value": value,
        "previous": round(previous, 2),
        "previous_date": prev_day.isoformat(),
        "date": today.isoformat(),
        "time": "",
        "live": today.weekday() < 5,
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
