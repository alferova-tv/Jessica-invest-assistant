"""Этап 3: выпуски исследования → bonds.csv.

Зачем. Строки недельных таблиц есть у эмитента, пока у него в обращении
хотя бы один выпуск, который участвует в исследовании: корпоративный,
рублёвый и не структурный (HYPOTHESES, «Валютные выпуски», «Структурные
облигации»). Здесь эти правила применяются к каждому выпуску из кэша
`history.py`, и записываются сроки обращения.

- **Рублёвый** — `FACEUNIT` дневной истории `RUB`, `RUR` или `SUR`
  (DATA.md, п. 8: рубль в истории до 2018 года записан как `RUR`).
- **Структурный** — пометка «Структурная облигация» в описании, метка
  в названии, все выпуски Сбербанк КИБ, ВТБ серии С-1, символический купон
  или решение ручного разбора `ручные_проверки/nkd_review.csv` (DATA.md,
  «Тип купона: описание и график купонов»).
- **Сроки обращения** — первый и последний день в дневной истории. Биржа
  пишет строку на каждый торговый день и без сделок, поэтому последний
  день — это последний день на бирже, а не последняя сделка.

Запуск (после history.py history, bondization, describe):

    .venv/bin/python research/scripts/bonds.py
"""

from __future__ import annotations

import csv
import re
from collections import Counter

import events
import history
import iss

OUT = iss.HOME / "research" / "out"
NKD_REVIEW = iss.HOME / "research" / "ручные_проверки" / "nkd_review.csv"
RUBLE = {"RUB", "RUR", "SUR"}
# Метки структурного продукта в названии выпуска (DATA.md, «Структурные
# облигации исключаются»). «ИО-» только у ТБанка: у других эмитентов
# так называются обычные выпуски.
NAME_MARKS = re.compile(r"структур|ИОС|SP Plc|Инвест\. Продукты|(?<![А-ЯA-Z])СО-|БО-ИО", re.I)
TBANK_IO = re.compile(r"ИО-")
VTB_S1 = re.compile(r"^ВТБС|С-1-")
# Символический купон: все известные ставки не выше 0,1% годовых.
SYMBOLIC_RATE = 0.1


def _structural(row: dict, description: dict, coupons: list[dict], amorts: list[dict],
                manual: dict[str, str]) -> str:
    """Почему выпуск структурный; пустая строка, если не структурный."""
    names = f"{row['shortname']} {row['name']}"
    title = row["emitent_title"] or ""
    if "структур" in (description.get("BONDTYPE") or description.get("BOND_TYPE") or "").lower():
        return "описание"
    if "Сбербанк КИБ" in title:
        return "Сбербанк КИБ"
    if NAME_MARKS.search(names) or "структур" in title.lower():
        return "название"
    if "ТБанк" in title or "Тинькофф" in title:
        if TBANK_IO.search(names):
            return "название"
    if "ВТБ" in title and VTB_S1.search(row["shortname"] or ""):
        return "ВТБ С-1"
    rates = [c["valueprc"] for c in coupons]
    known = [r for r in rates if r not in (None, 0)]
    if coupons and rates[0] not in (None, 0) and known and max(known) <= SYMBOLIC_RATE and len(amorts) <= 1:
        return "символический купон"
    if manual.get(row["secid"]) == "да":
        return "ручной разбор НКД"
    return ""


def main() -> None:
    with NKD_REVIEW.open() as fh:
        manual = {row["secid"]: row["структурная"] for row in csv.DictReader(fh)}
    universe = {row["secid"]: row for row in events.universe() if row["type"] in history.CORPORATE}
    # Выпуски «Д», которых нет в справочнике (РТК-Лизинг БО-02).
    with (OUT / "events_issues.csv").open() as fh:
        for row in csv.DictReader(fh):
            universe.setdefault(row["secid"], {**row, "name": row["shortname"]})

    rows, stats = [], Counter()
    for number, (secid, row) in enumerate(sorted(universe.items()), 1):
        days = iss.paged(history.HISTORY_URL.format(secid=secid), "history")
        if not days:
            stats["нет истории с 2014 года"] += 1
            continue
        unit = next((d["FACEUNIT"] for d in days if d["FACEUNIT"]), "")
        dates = sorted(d["TRADEDATE"] for d in days)
        matdate = max((d["MATDATE"] for d in days if d["MATDATE"]), default="")
        traded = [d["TRADEDATE"] for d in days if (d["NUMTRADES"] or 0) > 0]
        structural = ""
        if unit in RUBLE:
            payload = iss.get(history.BONDIZATION_URL.format(secid=secid))
            coupons = sorted(iss.rows(payload, "coupons"), key=lambda c: c["coupondate"] or "")
            description = {d["name"]: d["value"] for d in iss.rows(
                iss.get(events.SECURITY_URL.format(secid=secid)), "description")}
            structural = _structural(row, description, coupons, iss.rows(payload, "amortizations"), manual)
        rows.append({
            "secid": secid,
            "emitent_id": row["emitent_id"],
            "emitent_title": row["emitent_title"],
            "shortname": row["shortname"],
            "faceunit": unit,
            "structural": structural,
            "in_study": unit in RUBLE and not structural,
            "first_day": dates[0],
            "last_day": dates[-1],
            "last_trade": traded[-1] if traded else "",
            "matdate": matdate,
        })
        if number % 1000 == 0:
            print(f"  {number}/{len(universe)}")

    with (OUT / "bonds.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    ruble = [r for r in rows if r["faceunit"] in RUBLE]
    traded = [r for r in rows if r["last_trade"]]
    print(f"выпусков в справочнике: {len(universe)}, с историей с 2014 года: {len(rows)}, "
          f"со сделками: {len(traded)}")
    print(f"рублёвых с историей: {len(ruble)}, рублёвых со сделками: "
          f"{sum(1 for r in ruble if r['last_trade'])}")
    print("структурных среди рублёвых:", sum(1 for r in ruble if r["structural"]),
          dict(Counter(r["structural"] for r in ruble if r["structural"])))
    study = [r for r in rows if r["in_study"]]
    print(f"в исследовании: {len(study)} выпусков у {len({r['emitent_id'] for r in study})} эмитентов")
    print(f"таблица: {OUT / 'bonds.csv'}")


if __name__ == "__main__":
    main()
