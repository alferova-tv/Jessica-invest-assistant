"""Этап 3: все неплатежи эмитентов одной таблицей → nonpayments.csv.

Зачем. Событие задачи A — каждый техдефолт эмитента, а первый отмечается
флагом (HYPOTHESES, «Событие»). Неплатежи лежат в разных таблицах:

- `day_zero.csv` — неплатёж, после которого эмитента перевели в «Д»,
  и первый техдефолт по ручной проверке (`tech_first`);
- `disclosure_defaults.csv` — сообщения эмитентов «Д» в 450 днях до перевода;
- `techdefault_messages.csv` — сообщения эмитентов вне «Д» и ранние
  сообщения эмитентов «Д»;
- `incidents.csv` — цепочки техдефолтов эмитентов «Д» из списков smart-lab;
- `techdefault_events.csv` — объявленные дефолты вне «Д»;
- `manual_techdefault.csv` — неплатежи, известные только по ручной проверке.

Один неплатёж — эмитент и плановая дата платежа, сколько бы выпусков
и сообщений на неё ни приходилось. Неплатёж держателям (эмитент заплатил,
но деньги не дошли до держателей без реквизитов) событием не считается.

Исход неплатежа (HYPOTHESES, «В каких случаях запись по эмитенту
не добавляется»): выплата по сообщению эмитента (`cures.csv`), а если
сообщения о выплате нет — конец льготного срока, 10 рабочих дней, если
за это время не наступил дефолт.

Запуск (после day_zero.py, incidents.py, disclosure.py extra и cures):

    .venv/bin/python research/scripts/nonpayments.py
"""

from __future__ import annotations

import bisect
import csv
from collections import Counter

import day_zero as dz
import disclosure as ds

OUT = dz.OUT
GRACE_WORKDAYS = 10
# Дата smart-lab — начало техдефолта, обычно день после платежа (DATA.md,
# «smart-lab даёт начало техдефолта»). Дата из цепочки smart-lab или ручной
# проверки в 3 днях после даты из сообщения — тот же неплатёж.
SAME_NONPAYMENT_DAYS = 3


def _read(name: str) -> list[dict]:
    with (OUT / name).open() as fh:
        return list(csv.DictReader(fh))


def _cause_of(url: str, status: str) -> str:
    """Класс причины по тексту сообщения, как в `disclosure.py extra`."""
    path = ds.RAW / "events" / f"{url.rsplit('EventId=', 1)[-1]}.html"
    cause = ds._cause(ds._text(path)) if path.exists() else ""
    return ds._cause_class(cause, status == "дефолт")


def main() -> None:
    workdays = sorted(row["date"] for row in _read("business_days.csv"))
    zero = {row["emitent_id"]: row for row in _read("day_zero.csv") if row["day_zero"]}
    manual = ds._manual_extra()
    cures = {(row["emitent_id"], row["due_date"]): row for row in _read("cures.csv")}
    titles: dict[str, str] = {eid: row["emitent_title"] for eid, row in zero.items()}

    found: dict[tuple[str, str], dict] = {}

    def add(eid: str, date: str, source: str, status: str, cause_class: str) -> None:
        if not date:
            return
        item = found.setdefault((eid, date), {
            "emitent_id": eid, "date": date, "sources": [], "status": "", "cause_class": ""})
        if source not in item["sources"]:
            item["sources"].append(source)
        # «дефолт» в любом сообщении главнее «техдефолта» в другом.
        if status == "дефолт" or not item["status"]:
            item["status"] = status
        # Кредитная причина главнее заявленной технической, та — главнее неизвестной.
        rank = ("кредитная", "заявлена техническая", "неясно", "неизвестна")
        if not item["cause_class"] or rank.index(cause_class) < rank.index(item["cause_class"]):
            item["cause_class"] = cause_class

    # Эмитенты «Д»: неплатёж, после которого был перевод, и всё, что раньше него.
    for eid, row in zero.items():
        add(eid, row["day_zero"], f"дата неплатежа: {row['source']}", "дефолт", "кредитная")
        add(eid, row["tech_first"], "первый техдефолт (day_zero.csv)", "техдефолт", "неизвестна")
    for row in _read("disclosure_defaults.csv"):
        eid = row["emitent_id"]
        if eid in zero and row["due_date"] and row["due_date"] < zero[eid]["day_zero"]:
            cause_class = _cause_of(row["url"], row["status"])
            if cause_class != "у держателей":
                add(eid, row["due_date"], "сообщение эмитента", row["status"] or "техдефолт", cause_class)
    by_name = {dz._normalize(row["emitent_title"]): eid for eid, row in zero.items()}
    for row in _read("incidents.csv"):
        eid = by_name.get(dz._normalize(row["emitent_title"]))
        for part in filter(None, row["chain"].split(" | ")):
            date, label = part.split(" ", 1)
            if eid and date < zero[eid]["day_zero"]:
                add(eid, date, "цепочка smart-lab", label, "неизвестна")

    # Сообщения вне «Д» и ранние сообщения эмитентов «Д».
    for row in _read("techdefault_messages.csv"):
        if row["bond"] != "True" or not row["due_date"]:
            continue
        decision = (manual.get(row["inn"]) or {}).get("decision", "")
        if decision == "исключить":
            continue
        cause_class = _cause_of(row["url"], row["status"])
        status = row["status"] or "техдефолт"
        # Ручное решение главнее разбора причины, как в `disclosure.py extra`.
        if decision == "техническая":
            cause_class, status = "заявлена техническая", "техдефолт"
        elif decision:
            cause_class = decision
        if cause_class == "у держателей" and status != "дефолт":
            continue
        eid = row["emitent_id"]
        if eid in zero and row["due_date"] >= zero[eid]["day_zero"]:
            continue
        titles.setdefault(eid, row["emitent_title"])
        add(eid, row["due_date"], "сообщение эмитента", status, cause_class)

    episodes = _read("techdefault_events.csv")
    declared = {row["emitent_id"]: row for row in episodes if row["role"] == "дефолт"}
    for row in episodes:
        titles.setdefault(row["emitent_id"], row["emitent_title"])
        if row["date_from"] == "ручная проверка":
            add(row["emitent_id"], row["tech_first"], "ручная проверка", "техдефолт", row["cause_class"])

    # Дата из цепочки smart-lab или ручной проверки рядом с датой из сообщения —
    # тот же неплатёж, записанный днём позже.
    by_issuer: dict[str, list[dict]] = {}
    for item in found.values():
        by_issuer.setdefault(item["emitent_id"], []).append(item)
    rows = []
    for eid, items in by_issuer.items():
        items.sort(key=lambda i: i["date"])
        kept: list[dict] = []
        for item in items:
            prev = kept[-1] if kept else None
            weak = all(s in ("цепочка smart-lab", "первый техдефолт (day_zero.csv)") for s in item["sources"])
            if prev and weak and (dz.dt.date.fromisoformat(item["date"])
                                  - dz.dt.date.fromisoformat(prev["date"])).days <= SAME_NONPAYMENT_DAYS:
                prev["sources"] += [s for s in item["sources"] if s not in prev["sources"]]
                continue
            kept.append(item)

        default_day = zero[eid]["day_zero"] if eid in zero else (declared.get(eid) or {}).get("day_zero", "")
        default_date = (zero[eid]["default_date"] or zero[eid]["transfer_date"]) if eid in zero else (
            (declared.get(eid) or {}).get("default_date", ""))
        for n, item in enumerate(kept):
            date = item["date"]
            grace_end = workdays[min(bisect.bisect_right(workdays, date) + GRACE_WORKDAYS - 1, len(workdays) - 1)]
            cure = cures.get((eid, date)) or {}
            if default_day and date == default_day:
                outcome, cure_date, how = "дефолт", "", ""
            elif default_day and date < default_day <= grace_end:
                # Дефолт наступил в льготный срок этого неплатежа.
                outcome, cure_date, how = "дефолт", "", ""
            elif cure.get("cure_date"):
                outcome, cure_date, how = "исправлен", cure["cure_date"], f"выплата: {cure['how']}"
            else:
                outcome, cure_date, how = "исправлен", grace_end, "конец льготного срока"
            rows.append({
                "emitent_id": eid,
                "emitent_title": titles.get(eid, ""),
                "date": date,
                "status": item["status"],
                "cause_class": item["cause_class"],
                "sources": "; ".join(item["sources"]),
                "outcome": outcome,
                "cure_date": cure_date,
                "cure_how": how,
                "default_date": default_date if outcome == "дефолт" else "",
                "in_d": eid in zero,
                "first_for_issuer": n == 0,
            })

    rows.sort(key=lambda r: (r["date"], r["emitent_id"]))
    with (OUT / "nonpayments.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    since = [r for r in rows if r["date"] >= f"{ds.FIRST_YEAR}-01-01"]
    print(f"неплатежей: {len(rows)}, с {ds.FIRST_YEAR} года {len(since)} "
          f"у {len({r['emitent_id'] for r in since})} эмитентов")
    print("  исход:", dict(Counter(r["outcome"] for r in since)))
    print("  исправление:", dict(Counter(r["cure_how"].split(":")[0] for r in since if r["cure_how"])))
    print("  причина:", dict(Counter(r["cause_class"] for r in since)))
    print("  первых у эмитента:", sum(r["first_for_issuer"] for r in since),
          "из них у эмитентов «Д»:", sum(r["first_for_issuer"] and r["in_d"] for r in since))
    print(f"таблица: {OUT / 'nonpayments.csv'}")


if __name__ == "__main__":
    main()
