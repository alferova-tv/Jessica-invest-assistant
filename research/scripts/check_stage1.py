"""Проверка на выходе этапа 1: сходится ли список событий с внешними списками.

Что сверяется (таблица этапов в DATA.md):
- fin-plan: все ли дефолтные ISIN из их списка есть среди наших выпусков;
- smart-lab: все ли дефолты из годовых списков нашлись у наших эмитентов
  и совпадает ли дата;
- ОР Групп: известный дефолт, который поиск ISS по имени не находит;
- число событий по годам и разрыв «неплатёж → перевод» по источникам даты.

Работает по готовым таблицам, в сеть не ходит. Страница fin-plan лежит
в raw/finplan_def_<дата>.html: .venv/bin/python research/scripts/check_stage1.py
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import glob
import json
import pathlib
import re
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from day_zero import _normalize, _same_company  # noqa: E402 — то же сопоставление имён smart-lab, что в day_zero.py

DATA = pathlib.Path.home() / "Documents/Tracking/Invest/research"
RAW, OUT = DATA / "raw", DATA / "out"
PERIOD_START = "2014-01-01"


def _read(name: str) -> list[dict]:
    with (OUT / name).open() as fh:
        return list(csv.DictReader(fh))


def _universe() -> dict[str, dict]:
    """Справочник облигаций из кэша ISS: ISIN → строка."""
    found = {}
    for path in glob.glob(str(RAW / "securities.json*")):
        block = json.loads(pathlib.Path(path).read_text())["securities"]
        for values in block["data"]:
            row = dict(zip(block["columns"], values))
            found[row["isin"] or row["secid"]] = row
    return found


def main() -> None:
    issues = _read("events_issues.csv")
    days = _read("day_zero.csv")
    by_issuer = {row["emitent_id"]: row for row in days}
    our_isins = {row["isin"] or row["secid"] for row in issues}
    our_issuers = {row["emitent_id"] for row in issues}

    # --- fin-plan -------------------------------------------------------------
    page = sorted(RAW.glob("finplan_def_*.html"))[-1]
    finplan = sorted(set(re.findall(r"RU000[A-Z0-9]{7}", page.read_text())))
    universe = _universe()
    missing = [isin for isin in finplan if isin not in our_isins]
    print(f"== fin-plan ({page.name}): {len(finplan)} ISIN, есть в нашем списке {len(finplan) - len(missing)}")
    for isin in missing:
        row = universe.get(isin, {})
        known = row.get("emitent_id") and str(row["emitent_id"]) in our_issuers
        print(f"   нет выпуска {isin} {row.get('shortname', '?'):14} {row.get('emitent_title', 'нет в справочнике')[:60]}"
              f"{'  (эмитент у нас есть)' if known else ''}")

    # --- smart-lab ------------------------------------------------------------
    smartlab = [r for r in _read("smartlab_defaults.csv") if r["first_date"] >= PERIOD_START]
    print(f"\n== smart-lab: {len(smartlab)} записей с 2014 года")
    unmatched, agree, differ = [], 0, []
    for record in smartlab:
        key = _normalize(record["company"])
        hits = [row for row in days if _same_company(key, _normalize(row["emitent_title"]))]
        if not hits:
            unmatched.append(record)
            continue
        dates = [dt.date.fromisoformat(value) for value in record["all_dates"].split()]
        # Запись может относиться к другому эпизоду того же эмитента:
        # берём событие, ближайшее по дате.
        row = min(hits, key=lambda r: min(abs((dt.date.fromisoformat(r["transfer_date"]) - d).days) for d in dates))
        if not row["day_zero"]:
            continue
        ours = [dt.date.fromisoformat(v) for v in (row["day_zero"], row["tech_first"]) if v]
        # smart-lab пишет день начала техдефолта — обычно следующий за платежом.
        if any(abs((a - b).days) <= 3 for a in ours for b in dates):
            agree += 1
        else:
            differ.append((record["company"], record["all_dates"], row))
    print(f"   нашлись у наших эмитентов: {len(smartlab) - len(unmatched)}, не нашлись: {len(unmatched)}")
    print(f"   дата дня ноль или первого техдефолта в пределах 3 дней от записи smart-lab: {agree}, нет: {len(differ)}")
    for company, dates, row in differ:
        print(f"     {company[:34]:34} smart-lab {dates:35} у нас {row['day_zero']} / тех {row['tech_first'] or '-':10}"
              f" перевод {row['transfer_date']} ({row['source'][:22]})")
    for record in unmatched:
        print(f"   не нашлась: {record['company'][:45]:45} {record['first_date']} {record['kind']}")

    # --- ОР Групп -------------------------------------------------------------
    orgroup = [row for row in issues if row["emitent_inn"] == "5407249872"]
    print(f"\n== ОР Групп (ООО «ОР», ИНН 5407249872): выпусков {len(orgroup)}", end="")
    if orgroup:
        row = by_issuer[orgroup[0]["emitent_id"]]
        print(f", день ноль {row['day_zero']} ({row['source']}), перевод {row['transfer_date']}")
    else:
        print(" — НЕТ В СПИСКЕ")

    # --- по годам и разрыв ---------------------------------------------------
    dated = [row for row in days if row["day_zero"] and row["day_zero"] >= PERIOD_START]
    print(f"\n== события с датой с 2014 года: {len(dated)}")
    per_year = collections.Counter(row["day_zero"][:4] for row in dated)
    issues_year = collections.Counter()
    for row in issues:
        issuer = by_issuer.get(row["emitent_id"])
        if issuer and issuer["day_zero"] and issuer["day_zero"] >= PERIOD_START:
            issues_year[issuer["day_zero"][:4]] += 1
    print("   год: эмитентов / выпусков")
    for year in sorted(per_year):
        print(f"   {year}: {per_year[year]:3} / {issues_year[year]:3}")

    print("\n== разрыв «неплатёж → перевод», дней, по источнику даты")
    gaps = collections.defaultdict(list)
    for row in dated:
        gap = (dt.date.fromisoformat(row["transfer_date"]) - dt.date.fromisoformat(row["day_zero"])).days
        gaps[row["source"]].append(gap)
        gaps["все"].append(gap)
    for source, values in sorted(gaps.items(), key=lambda item: -len(item[1])):
        values.sort()
        print(f"   {source[:52]:52} n={len(values):3} медиана {statistics.median(values):4.0f}"
              f"  мин {values[0]:3}  макс {values[-1]:3}")
    tech = [row for row in dated if row["tech_first"]]
    print(f"\n== первый техдефолт раньше дня ноль: у {len(tech)} событий из {len(dated)}")


if __name__ == "__main__":
    main()
