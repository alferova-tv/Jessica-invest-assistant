"""Этап 3: кривая бескупонной доходности ОФЗ (КБД) и ключевая ставка ЦБ.

КБД нужна для G-спреда (признаки 2–3): доходность облигации минус
доходность ОФЗ на тот же срок. ISS отдаёт параметры кривой (модель
Нельсона — Сигеля — Свенссона, B1–B3, T1, G1–G9) и готовые доходности
на стандартных сроках на заданную дату. Исторический эндпоинт
`history/engines/stock/zcyc` возвращает только сегодняшний день
(проверено 2026-10-15), поэтому кривая берётся по одной дате на неделю —
каждую пятницу. На неторговую дату ISS отдаёт пустой ответ, поэтому тогда
берётся ближайший предыдущий день с кривой, но не дальше недели назад
(в марте 2022 торги стояли, и этих недель в таблице нет).

Ключевая ставка — признак 4 (доходность минус ставка) и правило П1.
Источник — таблица ЦБ `hd_base/KeyRate`, значение на каждый рабочий день.

Производственный календарь — рабочие дни для задачи B (сколько рабочих
дней прошло от неплатежа до выплаты). Источник — xmlcalendar.ru, по
файлу на год: в нём перечислены нерабочие дни, «*» — сокращённый рабочий.

Запуск (из корня Jessica):

    .venv/bin/python research/scripts/curves.py kbd       # ~660 запросов к ISS, около 5 минут
    .venv/bin/python research/scripts/curves.py keyrate   # один запрос к ЦБ
    .venv/bin/python research/scripts/curves.py calendar  # рабочие дни → business_days.csv
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import sys

import requests
from bs4 import BeautifulSoup

import iss

OUT = iss.HOME / "research" / "out"
FIRST_FRIDAY = dt.date(2014, 1, 3)
KBD_URL = "https://iss.moex.com/iss/engines/stock/zcyc.json?iss.meta=off&date={date}"
KEYRATE_URL = (
    "https://www.cbr.ru/hd_base/KeyRate/?UniDbQuery.Posted=True"
    "&UniDbQuery.From={start}&UniDbQuery.To={finish}"
)
CALENDAR_URL = "https://xmlcalendar.ru/data/ru/{year}/calendar.json"
# Нерабочие дни по указам 2020–2021 годов календарь отмечает выходными,
# хотя это не праздники: банки работали, а биржа торговала облигациями
# (проверено по сделкам в истории ISS 2026-10-20). Для льготного срока
# они рабочие. 24.06.2020 и 01.07.2020 биржа не торговала, они остаются
# нерабочими.
DECREE_WORKDAYS = (
    [dt.date(2020, 3, 30) + dt.timedelta(days=n) for n in range(32)]
    + [dt.date(2020, 5, 6), dt.date(2020, 5, 7), dt.date(2020, 5, 8)]
    + [dt.date(2021, 5, 4), dt.date(2021, 5, 5), dt.date(2021, 5, 6), dt.date(2021, 5, 7)]
    + [dt.date(2021, 11, 1), dt.date(2021, 11, 2), dt.date(2021, 11, 3), dt.date(2021, 11, 5)]
)
PARAMS = ["B1", "B2", "B3", "T1", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"]


def _fridays() -> list[dt.date]:
    out, day = [], FIRST_FRIDAY
    while day <= dt.date.today():
        out.append(day)
        day += dt.timedelta(days=7)
    return out


def cmd_kbd() -> None:
    """Параметры КБД и доходности на стандартных сроках по пятницам → kbd_weekly.csv."""
    rows, periods = [], set()
    for number, friday in enumerate(_fridays(), 1):
        for back in range(7):
            payload = iss.get(KBD_URL.format(date=(friday - dt.timedelta(days=back)).isoformat()))
            params = iss.rows(payload, "params")
            if params:
                break
        else:
            continue
        values = {key.upper(): value for key, value in params[0].items()}
        row = {"week": friday.isoformat(), "tradedate": values["TRADEDATE"]}
        row.update({key: values.get(key) for key in PARAMS})
        for point in iss.rows(payload, "yearyields"):
            name = f"y{point['period']:g}"
            periods.add((point["period"], name))
            row[name] = point["value"]
        rows.append(row)
        if number % 100 == 0:
            print(f"  КБД: {number} недель", flush=True)
    columns = ["week", "tradedate", *PARAMS, *[name for _, name in sorted(periods)]]
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "kbd_weekly.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"КБД: {len(rows)} недель, {rows[0]['tradedate']} — {rows[-1]['tradedate']}")


def cmd_keyrate() -> None:
    """Ключевая ставка ЦБ по рабочим дням с 2014 года → key_rate.csv."""
    url = KEYRATE_URL.format(start="01.01.2014", finish=dt.date.today().strftime("%d.%m.%Y"))
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    path = iss.RAW / f"cbr_keyrate_{dt.date.today().isoformat()}.html"
    path.write_text(response.text)
    table = BeautifulSoup(response.text, "html.parser").find("table", class_="data")
    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) == 2:
            day = dt.datetime.strptime(cells[0], "%d.%m.%Y").date()
            rows.append({"date": day.isoformat(), "key_rate": float(cells[1].replace(",", "."))})
    rows.sort(key=lambda r: r["date"])
    with (OUT / "key_rate.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "key_rate"])
        writer.writeheader()
        writer.writerows(rows)
    changes = sum(1 for a, b in zip(rows, rows[1:]) if a["key_rate"] != b["key_rate"])
    print(f"ключевая ставка: {len(rows)} дней, {rows[0]['date']} — {rows[-1]['date']}, "
          f"изменений {changes}, от {min(r['key_rate'] for r in rows)} до {max(r['key_rate'] for r in rows)}%")


def cmd_calendar() -> None:
    """Рабочие дни с 2014 года по производственному календарю → business_days.csv."""
    off = set()
    for year in range(2014, dt.date.today().year + 1):
        path = iss.RAW / f"xmlcalendar_{year}.json"
        if not path.exists() or year == dt.date.today().year:
            # Без браузерного User-Agent сайт рвёт соединение.
            response = requests.get(CALENDAR_URL.format(year=year), timeout=30,
                                    headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            path.write_text(response.text)
        for month in json.loads(path.read_text())["months"]:
            for day in month["days"].split(","):
                if "*" not in day:
                    off.add(dt.date(year, month["month"], int(day.rstrip("+"))))
    off -= {day for day in DECREE_WORKDAYS if day.weekday() < 5}
    day, rows = dt.date(2014, 1, 1), []
    while day.year <= dt.date.today().year:
        if day not in off:
            rows.append({"date": day.isoformat()})
        day += dt.timedelta(days=1)
    with (OUT / "business_days.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"рабочих дней: {len(rows)}, {rows[0]['date']} — {rows[-1]['date']}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        {"kbd": cmd_kbd, "keyrate": cmd_keyrate, "calendar": cmd_calendar}.get(command, lambda: print(__doc__))()
    except iss.SourceUnavailable as error:
        print(f"Остановлено: {error}")
        sys.exit(1)
