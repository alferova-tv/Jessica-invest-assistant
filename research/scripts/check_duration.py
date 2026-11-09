"""Контроль DURATION по двум доходностям; только чтение локального кэша.

Запуск из корня Jessica: .venv/bin/python research/scripts/check_duration.py
График из сегодняшнего кэша используется только для проверки формулы,
не для восстановления признаков на историческую дату.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import statistics
from collections import Counter

from check_waprice import DATA, cached_rows


def schedule(secid):
    url = ("https://iss.moex.com/iss/statistics/engines/stock/markets/bonds/"
           f"bondization/{secid}.json?iss.meta=off&limit=unlimited")
    tail = url.split("/iss/")[-1].split("?")[0].replace("/", "_")[:80]
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    payload = json.loads((DATA / "raw" / f"{tail}.{digest}.json").read_text())
    return {key: [dict(zip(payload[key]["columns"], row))
                  for row in payload[key]["data"]]
            for key in ("coupons", "amortizations", "offers")}


def day(value):
    return dt.date.fromisoformat(value[:10])


def calculate(row, flows, field, offset):
    settlement = day(row["TRADEDATE"]) + dt.timedelta(days=offset)
    # Нормировка на сумму PV: средний срок платежей с весами PV.
    # В формуле (27) знаменатель P+НКД. Они равны при точной доходности;
    # отдельно проверяем отклонение PV от цены, т.к. ISS округляет доходность.
    pv = [((date - settlement).days,
           value / (1 + row[field] / 100) ** ((date - settlement).days / 365))
          for date, value in flows if date > settlement]
    total = sum(value for _, value in pv)
    price_field = "WAPRICE" if field == "YIELDATWAP" else "CLOSE"
    dirty_price = row[price_field] * 10 + row["ACCINT"]  # номинал 1000
    return {"duration": sum(days * value for days, value in pv) / total,
            "price_residual": total - dirty_price}


def main():
    with (DATA / "out" / "bonds.csv").open() as fh:
        bonds = [r for r in csv.DictReader(fh) if r["in_study"] == "True"]
    fields = ("YIELDATWAP", "YIELDCLOSE")
    observations = []
    checked_issues = set()
    candidates = 0
    for bond in bonds:
        secid = bond["secid"]
        s = schedule(secid)
        coupons, principal = s["coupons"], s["amortizations"]
        # Простой контроль: нет оферт в кэше, один платёж номинала,
        # купоны постоянны с точностью до 2 копеек. Это не гарантия
        # отсутствия исторических изменений условий выпуска.
        if s["offers"] or len(principal) != 1 or len(coupons) < 4:
            continue
        if any(c["value"] is None or c["value"] <= 0 for c in coupons):
            continue
        if max(c["value"] for c in coupons) - min(c["value"] for c in coupons) > .02:
            continue
        if principal[0]["value"] != 1000 or any(c["facevalue"] != 1000 for c in coupons):
            continue
        flows = [(day(c["coupondate"]), c["value"]) for c in coupons]
        flows.append((day(principal[0]["amortdate"]), 1000))
        by_year = {}
        for row in cached_rows(secid):
            if row["BOARDID"] not in ("EQOB", "TQCB") or not row["NUMTRADES"]:
                continue
            if not all(row.get(k) and row[k] > 0 for k in
                       (*fields, "DURATION", "CLOSE", "WAPRICE")):
                continue
            if row["ACCINT"] is None or row["FACEVALUE"] != 1000:
                continue
            if not all(1 < row[f] < 50 for f in fields):
                continue
            gap = abs(row[fields[0]] - row[fields[1]])
            if gap < .3 or sum(day(c["coupondate"]) > day(row["TRADEDATE"])
                               for c in coupons) < 3:
                continue
            # Выбор строки до расчёта ошибки: максимальное различие
            # доходностей в каждом году у каждого выпуска.
            year = row["TRADEDATE"][:4]
            if year not in by_year or gap > abs(by_year[year][fields[0]] - by_year[year][fields[1]]):
                by_year[year] = row
        if by_year:
            checked_issues.add(secid)
        for row in by_year.values():
            candidates += 1
            calcs = {f: [calculate(row, flows, f, offset) for offset in range(4)]
                     for f in fields}
            # Порог 1 рубль = 0,1% номинала: график должен объяснять
            # обе исходные цены. Не доказывает полноту истории графика.
            if any(abs(calcs[f][0]["price_residual"]) > 1 for f in fields):
                continue
            # Разница 5 дней выбрана для отделения гипотез при округлении
            # DURATION до дня и проверяемом сдвиге даты на 0–3 дня.
            if abs(calcs[fields[0]][0]["duration"] - calcs[fields[1]][0]["duration"]) < 5:
                continue
            # Для ОБЕИХ гипотез одинаково допускаем сдвиги 0–3 дня.
            # Это проверка устойчивости, а не определение календаря расчётов.
            errors = {f: min(abs(c["duration"] - row["DURATION"])
                             for c in calcs[f]) for f in fields}
            observations.append({"secid": secid, "name": bond["shortname"],
                                 "date": row["TRADEDATE"], "duration_iss": row["DURATION"],
                                 "yields": {f: row[f] for f in fields},
                                 "calculations": calcs, "min_errors_days": errors})
    summary = {f: {"median_error_days": statistics.median(
                       r["min_errors_days"][f] for r in observations),
                   "within_0_6_days": sum(r["min_errors_days"][f] <= .6 for r in observations)}
               for f in fields} if observations else {}
    print(json.dumps({"candidate_issues": len(checked_issues), "candidate_rows": candidates,
                      "control_issues": len({r["secid"] for r in observations}),
                      "control_rows": len(observations),
                      "by_year": dict(sorted(Counter(r["date"][:4] for r in observations).items())),
                      "summary": summary, "observations": observations},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
