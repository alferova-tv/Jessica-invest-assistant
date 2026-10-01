"""Все неисполнения обязательств эмитента до дефолта.

Зачем. У части эмитентов дефолту предшествует длинная цепочка техдефолтов:
Русская Контейнерная Компания не платила вовремя с июня 2024, а в режим «Д»
её перевели в марте 2025 — девять месяцев спустя. Если считать признаки
за полгода до дефолта, туда попадёт период, когда эмитент уже публично
не платил. Модель научится замечать беду, а не предсказывать её.

Поэтому по каждому событию нужны две даты:

- **первый техдефолт** — первое неисполнение, даже если деньги потом
  пришли. До неё обрезаются признаки;
- **дефолт** — перевод в режим «Д», он остаётся целевым событием.

Заодно считается, сколько неисполнений было до дефолта: у Роял Капитала
за месяц их пять по разным выпускам, и это кандидат в признаки.

События после дефолта не собираем: спекулировать бумагами в дефолте
не входит в задачу.

Источник — годовые списки smart-lab (`smartlab_defaults.py`), где цепочка
описана словами: «техдефолт по купону с 16.02, 18.02 обязательства
исполнены, 26.08 техдефолт по части номинала, 08.09 дефолт».

Запуск (после smartlab_defaults.py и day_zero.py):

    .venv/bin/python research/scripts/incidents.py
"""

from __future__ import annotations

import csv
import datetime as dt
import re

import day_zero as dz

# Слова-метки в текстах smart-lab. Порядок важен: «техдефолт» проверяется
# раньше «дефолта», иначе первое совпадёт со вторым.
MARKERS = (
    ("исполнено", r"обязательств[оа]?\s+исполнен|погашен[аоы]|выплачен"),
    ("техдефолт", r"техдефолт|технический дефолт"),
    ("дефолт", r"дефолт"),
)

DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b")


def _chain(text: str, default_year: int) -> list[tuple[str, str]]:
    """Цепочка (дата, что случилось) в порядке появления в тексте."""
    out: list[tuple[str, str]] = []
    label = "техдефолт"
    for piece in re.split(r"[,;]", text):
        lowered = piece.lower()
        for name, pattern in MARKERS:
            if re.search(pattern, lowered):
                label = name
                break
        for day, month, year in DATE.findall(piece):
            try:
                moment = dt.date(int(year or default_year), int(month), int(day))
            except ValueError:
                continue
            out.append((moment.isoformat(), label))
    return out


def main() -> None:
    with (dz.OUT / "smartlab_defaults.csv").open() as fh:
        records = list(csv.DictReader(fh))
    with (dz.OUT / "day_zero.csv").open() as fh:
        events = [row for row in csv.DictReader(fh) if row["day_zero"] and row["transfer_date"] >= "2014"]

    chains: dict[str, list[tuple[str, str]]] = {}
    for record in records:
        year = int(re.search(r"(20\d{2})", record["source"]).group(1))
        key = dz._normalize(record["company"])
        chains.setdefault(key, []).extend(_chain(record["text"], year))

    out = []
    covered = 0
    for event in events:
        key = dz._normalize(event["emitent_title"])
        chain = next(
            (items for other, items in chains.items() if other and (other in key or key in other)),
            [],
        )
        # Только то, что случилось до дефолта: после него бумага уже в «Д».
        before = sorted({
            (date, label) for date, label in chain
            if date <= event["transfer_date"] and label != "исполнено"
        })
        cured = sorted({date for date, label in chain if label == "исполнено" and date <= event["transfer_date"]})
        first = before[0][0] if before else ""
        if first:
            covered += 1
        out.append(
            {
                "emitent_title": event["emitent_title"],
                "transfer_date": event["transfer_date"],
                "day_zero": event["day_zero"],
                "first_incident": first,
                "incidents_before": len(before),
                "cured_before": len(cured),
                "days_first_to_default": (
                    (dt.date.fromisoformat(event["transfer_date"]) - dt.date.fromisoformat(first)).days
                    if first else ""
                ),
                "chain": " | ".join(f"{date} {label}" for date, label in before),
            }
        )

    with (dz.OUT / "incidents.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out[0]))
        writer.writeheader()
        writer.writerows(out)

    print(f"событий 2014+: {len(events)}")
    print(f"цепочка известна: {covered} ({covered / len(events):.0%})")

    serial = [row for row in out if row["incidents_before"] and int(row["incidents_before"]) > 1]
    print(f"эмитентов с несколькими неисполнениями до дефолта: {len(serial)}")
    if serial:
        counts = sorted(int(row["incidents_before"]) for row in serial)
        gaps = sorted(int(row["days_first_to_default"]) for row in serial if row["days_first_to_default"])
        print(f"  неисполнений до дефолта: медиана {counts[len(counts) // 2]}, максимум {counts[-1]}")
        print(f"  от первого неисполнения до дефолта, дней: медиана {gaps[len(gaps) // 2]}, максимум {gaps[-1]}")
        print(f"  из них больше полугода: {sum(1 for g in gaps if g > 182)}")
    print(f"\nтаблица: {dz.OUT / 'incidents.csv'}")


if __name__ == "__main__":
    main()
