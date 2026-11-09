"""Этап 3: метка y на каждую неделю эмитента → labels.csv.

Строка — эмитент и неделя, пока у эмитента в обращении есть хотя бы один
выпуск исследования (`bonds.csv`). Неделя — с понедельника по воскресенье,
обозначается пятницей (HYPOTHESES, «Наблюдения»).

- `label` — 1, если у эмитента будет неплатёж (`nonpayments.csv`)
  в одну из 16 следующих недель, t+1 … t+16. Неплатёж недели t в метку
  строки t не входит, так как признаки этой недели его уже видят.
- `weeks_to_event` — сколько недель до ближайшего следующего неплатежа.
- `use`, `skip_reason` — используется ли строка и почему нет (DATA.md,
  «Таблица меток»; HYPOTHESES, «В каких случаях запись по эмитенту
  не добавляется»).

Запуск (после bonds.py и nonpayments.py):

    .venv/bin/python research/scripts/labels.py
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
from collections import Counter, defaultdict

import iss

OUT = iss.HOME / "research" / "out"
MANUAL = pathlib.Path(__file__).resolve().parents[1] / "manual"
HORIZON = dt.timedelta(weeks=16)
FIRST_WEEK = dt.date(2014, 1, 3)
# Цены в кэше истории есть по 14.10.2026, последняя полная неделя — 09.10.2026.
LAST_WEEK = dt.date(2026, 10, 9)
# Сообщения о неплатежах собраны по 23.09.2026: последняя полная неделя
# до этой даты — 18.09.2026, исход за 16 недель известен до недели 29.05.2026.
MESSAGES_END = dt.date(2026, 9, 23)
# Выпуск ушёл с биржи без погашения, если последний день в истории раньше
# даты погашения больше чем на 30 дней. Выпуски с историей после 18.09.2026
# ещё обращаются: их обрезал день скачивания.
REDEEMED_DAYS = 30
STILL_TRADED = dt.date(2026, 9, 18)


def friday(day: dt.date) -> dt.date:
    """Пятница недели, в которую попадает день."""
    return day + dt.timedelta(days=4 - day.weekday())


def _date(text: str) -> dt.date | None:
    return dt.date.fromisoformat(text) if text else None


def _read(path) -> list[dict]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    last_known = friday(MESSAGES_END - dt.timedelta(weeks=1)) - HORIZON

    # Недели в обращении и уходы с биржи без погашения.
    weeks: dict[str, set[dt.date]] = defaultdict(set)
    left: dict[str, list[dt.date]] = defaultdict(list)
    for bond in _read(OUT / "bonds.csv"):
        if bond["in_study"] != "True":
            continue
        eid = bond["emitent_id"]
        first, last = friday(_date(bond["first_day"])), friday(_date(bond["last_day"]))
        week = max(first, FIRST_WEEK)
        while week <= min(last, LAST_WEEK):
            weeks[eid].add(week)
            week += dt.timedelta(weeks=1)
        matdate = _date(bond["matdate"])
        last_day = _date(bond["last_day"])
        if last_day < STILL_TRADED and (not matdate or last_day < matdate - dt.timedelta(days=REDEEMED_DAYS)):
            left[eid].append(last)
    # Уход считается уходом эмитента, только если на следующей неделе
    # у него не осталось выпусков в обращении.
    departures = {eid: sorted({w for w in ws if w + dt.timedelta(weeks=1) not in weeks[eid]})
                  for eid, ws in left.items()}

    nonpayments: dict[str, list[dict]] = defaultdict(list)
    for row in _read(OUT / "nonpayments.csv"):
        nonpayments[row["emitent_id"]].append({
            "week": friday(_date(row["date"])),
            "default": row["outcome"] == "дефолт",
            "cure_week": friday(_date(row["cure_date"])) if row["cure_date"] else None,
            "date": row["date"],
            "first": row["first_for_issuer"] == "True",
        })
    restructurings: dict[str, list[dt.date]] = defaultdict(list)
    for row in _read(MANUAL / "manual_restructuring.csv"):
        restructurings[row["emitent_id"]].append(friday(_date(row["decision_date"])))

    rows = []
    for eid in sorted(weeks, key=int):
        events = sorted(nonpayments.get(eid, []), key=lambda e: e["week"])
        default_week = min((e["week"] for e in events if e["default"]), default=None)
        for week in sorted(weeks[eid]):
            ahead = [e["week"] for e in events if e["week"] > week]
            label = int(any(w <= week + HORIZON for w in ahead))
            reason = ""
            if default_week and week >= default_week:
                reason = "после дефолта"
            elif any(e["week"] <= week < (e["cure_week"] or e["week"]) for e in events):
                reason = "между неплатежом и исправлением"
            elif week > last_known:
                reason = "конец данных"
            elif any(week < w <= week + HORIZON for w in restructurings.get(eid, [])):
                reason = "реструктуризация в 16 неделях"
            elif any(week <= w < week + HORIZON for w in departures.get(eid, [])):
                reason = "уход с биржи в 16 неделях"
            rows.append({
                "emitent_id": eid,
                "week": week.isoformat(),
                "label": label,
                "weeks_to_event": (ahead[0] - week).days // 7 if ahead else "",
                "use": int(not reason),
                "skip_reason": reason,
            })

    with (OUT / "labels.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    used = [r for r in rows if r["use"]]
    print(f"строк: {len(rows)} у {len(weeks)} эмитентов, недели {FIRST_WEEK} … {LAST_WEEK}, "
          f"исход известен до недели {last_known}")
    print(f"используются: {len(used)}, из них label = 1: {sum(r['label'] for r in used)} "
          f"({sum(r['label'] for r in used) / len(used):.2%})")
    print("не используются:", dict(Counter(r["skip_reason"] for r in rows if not r["use"])))

    # Сколько неплатежей видно в выборке: есть хотя бы одна используемая
    # строка с label = 1 в 16 неделях перед ним.
    positive = defaultdict(set)
    for r in used:
        if r["label"]:
            positive[r["emitent_id"]].add(dt.date.fromisoformat(r["week"]))
    seen, missed = 0, Counter()
    lost = []
    for eid, events in nonpayments.items():
        for e in events:
            if not (FIRST_WEEK < e["week"] <= last_known + HORIZON):
                continue
            if any(e["week"] - HORIZON <= w < e["week"] for w in positive.get(eid, ())):
                seen += 1
            else:
                why = ("нет строк эмитента" if eid not in weeks else
                       "все 16 недель перед ним не используются")
                missed[why] += 1
                lost.append((eid, e["date"], why))
    print(f"неплатежей с 2014 года в пределах известного исхода: {seen + sum(missed.values())}, "
          f"видны в выборке: {seen}, не видны: {dict(missed)}")
    print(f"таблица: {OUT / 'labels.csv'}")


if __name__ == "__main__":
    main()
