"""Проверка гипотезы: где проявляется сигнал — на всех выпусках или только на «своём».

Вопрос автора (2026-09-24). Дефолт — событие эмитента, но признаки
считаются по выпускам. Если перед неплатежом падает только тот выпуск,
по которому подходит платёж, то главный признак — связка «выпуск плюс
ближайший платёж», и сворачивать признаки в неделю эмитента нельзя.
Если падают все выпуски одинаково, сворачивать можно, и это упрощает всё.

Как проверяется. Берём события, где дефолтный выпуск известен точно
(`issue_confidence` = единственный платёж в дату или раскрытие) и у эмитента
есть хотя бы два торговавшихся выпуска. В окне за 13 недель до дня ноль,
не включая последнюю неделю перед ним (там уже обвал), считаем по каждому
выпуску три величины:

- падение от максимума окна;
- худшее недельное падение;
- всплеск оборота: последние 4 недели против медианы предыдущих.

Дальше сравниваем дефолтный выпуск с остальными выпусками того же
эмитента — попарно, внутри эмитента. Считаем, у скольких эмитентов
дефолтный выпуск просел сильнее. Это знаковый тест: если сигнал одинаков,
доля будет около половины.

Запуск:

    .venv/bin/python research/scripts/signal_scope.py
"""

from __future__ import annotations

import csv
import datetime as dt
import statistics as st

import iss
import day_zero as dz

WINDOW_WEEKS = 13
# Последняя неделя перед днём ноль выбрасывается: рынок уже мог узнать
# о беде из новостей, а нам нужны признаки, а не сам обвал.
SKIP_LAST_WEEKS = 1
SURE = ("единственный платёж в эту дату", "раскрытие")


def _weekly(history: list[dict]) -> dict[dt.date, dict]:
    """Дневные сделки → недели: средневзвешенная цена и оборот."""
    weeks: dict[dt.date, dict] = {}
    for row in history:
        monday = row["date"] - dt.timedelta(days=row["date"].weekday())
        week = weeks.setdefault(monday, {"value": 0.0, "amount": 0.0, "trades": 0})
        week["value"] += row["price"] * (row["value"] or 1)
        week["amount"] += row["value"] or 1
        week["trades"] += row["trades"]
    for week in weeks.values():
        week["price"] = week["value"] / week["amount"] if week["amount"] else None
    return weeks


def _measures(history: list[dict], zero: dt.date) -> dict | None:
    """Три величины по выпуску в окне до дня ноль."""
    weeks = _weekly(history)
    last = zero - dt.timedelta(days=zero.weekday() + 7 * SKIP_LAST_WEEKS)
    first = last - dt.timedelta(weeks=WINDOW_WEEKS)
    series = [(monday, week["price"], week["amount"])
              for monday, week in sorted(weeks.items())
              if first <= monday <= last and week["price"]]
    if len(series) < 5:
        return None

    prices = [price for _, price, _ in series]
    peak = max(prices)
    drops = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
    amounts = [amount for _, _, amount in series]
    recent = amounts[-4:]
    earlier = amounts[:-4] or amounts
    return {
        "from_peak": prices[-1] / peak - 1,
        "worst_week": min(drops) if drops else 0.0,
        "volume_spike": (st.median(recent) / st.median(earlier)) if st.median(earlier) else 1.0,
        "weeks": len(series),
    }


def main() -> None:
    with (dz.OUT / "day_zero.csv").open() as fh:
        events = [
            row for row in csv.DictReader(fh)
            if row["day_zero"] and row["issue_confidence"] in SURE and row["transfer_date"] >= "2014"
        ]
    print(f"событий с точно известным выпуском: {len(events)}")

    compared = []
    for number, event in enumerate(events, 1):
        zero = dt.date.fromisoformat(event["day_zero"])
        defaulted = event["defaulted_secid"]
        issues = dz._issuer_issues({"secids": event["defaulted_secid"], "emitent_id": event["emitent_id"]})
        if len(issues) < 2:
            continue

        measures = {}
        for secid in issues:
            history = dz._history(secid, zero - dt.timedelta(days=140), zero)
            value = _measures(history, zero)
            if value:
                measures[secid] = value
        others = {secid: value for secid, value in measures.items() if secid != defaulted}
        if defaulted not in measures or not others:
            continue

        compared.append(
            {
                "emitent": event["emitent_title"][:40],
                "day_zero": event["day_zero"],
                "issues": len(measures),
                "own_from_peak": measures[defaulted]["from_peak"],
                "others_from_peak": st.median(v["from_peak"] for v in others.values()),
                "own_worst": measures[defaulted]["worst_week"],
                "others_worst": st.median(v["worst_week"] for v in others.values()),
                "own_volume": measures[defaulted]["volume_spike"],
                "others_volume": st.median(v["volume_spike"] for v in others.values()),
            }
        )
        if number % 20 == 0:
            print(f"  обработано {number}/{len(events)}")

    if not compared:
        print("нет эмитентов с двумя и более торговавшимися выпусками")
        return

    print(f"\nэмитентов с двумя и более торговавшимися выпусками: {len(compared)}")
    for label, own_key, others_key, worse in (
        ("падение от максимума за 13 недель", "own_from_peak", "others_from_peak", "меньше"),
        ("худшее недельное падение", "own_worst", "others_worst", "меньше"),
        ("всплеск оборота", "own_volume", "others_volume", "больше"),
    ):
        own = [row[own_key] for row in compared]
        others = [row[others_key] for row in compared]
        if worse == "меньше":
            stronger = sum(1 for row in compared if row[own_key] < row[others_key])
        else:
            stronger = sum(1 for row in compared if row[own_key] > row[others_key])
        print(f"\n{label}:")
        print(f"  дефолтный выпуск: медиана {st.median(own):+.1%}" if worse == "меньше"
              else f"  дефолтный выпуск: медиана ×{st.median(own):.2f}")
        print(f"  прочие выпуски:   медиана {st.median(others):+.1%}" if worse == "меньше"
              else f"  прочие выпуски:   медиана ×{st.median(others):.2f}")
        print(f"  сигнал сильнее у дефолтного: {stronger} из {len(compared)}"
              f" ({stronger / len(compared):.0%})")

    with (dz.OUT / "signal_scope.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(compared[0]))
        writer.writeheader()
        writer.writerows(compared)
    print(f"\nподробно: {dz.OUT / 'signal_scope.csv'}")


if __name__ == "__main__":
    try:
        main()
    except iss.SourceUnavailable as error:
        print(f"\nОстановка: {error}")
