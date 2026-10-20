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

Запуск (из корня Jessica):

    .venv/bin/python research/scripts/curves.py kbd       # ~660 запросов к ISS, около 5 минут
    .venv/bin/python research/scripts/curves.py keyrate   # один запрос к ЦБ
"""

from __future__ import annotations

import csv
import datetime as dt
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


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        {"kbd": cmd_kbd, "keyrate": cmd_keyrate}.get(command, lambda: print(__doc__))()
    except iss.SourceUnavailable as error:
        print(f"Остановлено: {error}")
        sys.exit(1)
