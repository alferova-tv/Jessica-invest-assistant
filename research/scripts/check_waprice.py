"""Проверка совместимости WAPRICE и VOLUME по локальному кэшу истории.

Сравнивает WAPRICE с VALUE / VOLUME / FACEVALUE * 100 по выпускам
исследования из bonds.csv. Это диагностическая сверка: расхождение
не устанавливает причину и само по себе не доказывает ошибку WAPRICE.
Дополнительно ищет строки с одной сделкой, где CLOSE совпадает с ценой,
восстановленной из оборота, но отличается от WAPRICE.

Сети и записи данных нет. Запуск из корня Jessica:
    .venv/bin/python research/scripts/check_waprice.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

HOME = Path(os.environ.get("JESSICA_HOME", "~/Documents/Tracking/Invest")).expanduser()
DATA = HOME / "research"
# Точный URL сбора history.py: читаем только соответствующие страницы,
# не смешивая их с прежними выборками и отдельным кэшем BUYBACKDATE.
COLUMNS = (
    "BOARDID,TRADEDATE,SECID,NUMTRADES,VALUE,VOLUME,WAPRICE,CLOSE,"
    "LEGALCLOSEPRICE,YIELDCLOSE,YIELDATWAP,DURATION,ACCINT,MATDATE,"
    "FACEVALUE,FACEUNIT,COUPONPERCENT,COUPONVALUE"
)
URL = (
    "https://iss.moex.com/iss/history/engines/stock/markets/bonds/"
    "securities/{secid}.json?iss.meta=off&from=2014-01-01&history.columns=" + COLUMNS
)
# Пороги описывают размер расхождения в п.п. номинала. Это не допуски
# качества цены и не фильтр будущей таблицы признаков.
THRESHOLDS = (0.0051, 0.051, 0.1001, 0.5001, 1.0001)


def cached_rows(secid: str):
    start = 0
    while True:
        url = URL.format(secid=secid) + f"&start={start}"
        tail = url.split("/iss/")[-1].split("?")[0].replace("/", "_")[:80]
        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        path = DATA / "raw" / f"{tail}.{digest}.json"
        # Пропущенная страница останавливает проверку, а не считается
        # отсутствием торгов. В сеть за ней не обращаемся.
        payload = json.loads(path.read_text())
        block = payload["history"]
        chunk = [dict(zip(block["columns"], values)) for values in block["data"]]
        yield from chunk
        if not chunk:
            return
        start += 100
        cursor = payload.get("history.cursor", {})
        if cursor.get("data"):
            info = dict(zip(cursor["columns"], cursor["data"][0]))
            if start >= info["TOTAL"]:
                return


def main() -> None:
    with (DATA / "out" / "bonds.csv").open() as fh:
        bonds = [r for r in csv.DictReader(fh) if r["in_study"] == "True"]
    total, years = Counter(), defaultdict(Counter)
    examples = []
    for bond in sorted(bonds, key=lambda r: r["secid"]):
        for row in cached_rows(bond["secid"]):
            if row["TRADEDATE"] < "2014-01-01":
                continue
            total["rows"] += 1
            if not (row["NUMTRADES"] or 0) > 0:
                if row["WAPRICE"] is not None:
                    total["wap_without_trades"] += 1
                continue
            total["traded"] += 1
            if not row["WAPRICE"]:
                total["missing_wap"] += 1
            if not (row["VOLUME"] or 0) > 0:
                total["missing_volume"] += 1
            if not all(row[k] is not None and row[k] > 0
                       for k in ("WAPRICE", "VOLUME", "FACEVALUE", "VALUE")):
                continue
            implied = row["VALUE"] / row["VOLUME"] / row["FACEVALUE"] * 100
            error = abs(implied - row["WAPRICE"])
            for counts in (total, years[row["TRADEDATE"][:4]]):
                counts["comparable"] += 1
                for threshold in THRESHOLDS:
                    if error > threshold:
                        counts[f"difference_gt_{threshold}"] += 1
                if row["NUMTRADES"] == 1:
                    counts["one_trade"] += 1
                    if row["CLOSE"] is not None and abs(row["CLOSE"] - row["WAPRICE"]) > 0.051:
                        counts["one_trade_close_wap_diff"] += 1
            # Короткий список простых контрпримеров: номинал 1000,
            # одна сделка, цена из оборота совпадает с CLOSE.
            if (len(examples) < 10 and row["NUMTRADES"] == 1
                    and row["FACEVALUE"] == 1000 and row["CLOSE"] is not None
                    and abs(row["CLOSE"] - implied) < 0.0051
                    and 0.1 < error < 10 and 50 < implied < 120):
                examples.append({
                    "name": bond["shortname"],
                    **{k: row[k] for k in ("SECID", "BOARDID", "TRADEDATE", "NUMTRADES",
                                          "VALUE", "VOLUME", "FACEVALUE", "WAPRICE", "CLOSE")},
                    "price_from_value": round(implied, 6),
                })
    print(json.dumps({"issues": len(bonds), "summary": dict(total),
                      "by_year": {y: dict(c) for y, c in sorted(years.items())},
                      "examples": examples}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
