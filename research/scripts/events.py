"""Этап 1: список дефолтов и даты событий.

Откуда берутся события. После дефолта биржа переводит облигации эмитента
в режим «Облигации Д» (правила листинга, moex.com/s28). В ISS у каждого
выпуска в блоке `boards` видна дата включения в режим — это и есть след
дефолта, машиночитаемый и покрывающий весь биржевой период.

Порядок работы:

1. `discover` — недельные срезы режимов «Д» за все годы. Срезы нужны только
   чтобы узнать *какие* выпуски там были; точная дата берётся потом
   из карточки выпуска, поэтому шаг среза на точность не влияет.
2. `universe` — полный справочник облигаций (торгуемых и снятых) ради
   связки выпуск → эмитент (ИНН) и ради контрольной группы.
3. `build` — по каждому найденному выпуску: точная дата перевода, эмитент,
   флаги дефолта; затем события сворачиваются на уровень эмитента
   и подбираются кандидаты в дату неплатежа из графика платежей.

Почему кандидаты, а не одна дата. Дефолтом считается просрочка более
10 рабочих дней, перевод в режим — не позднее 2 торговых дней после этого.
Значит, неплатёж случился примерно за три недели до перевода, и в это окно
у эмитента с несколькими выпусками попадает несколько плановых платежей.
Скрипт предлагает кандидатов и помечает спорные случаи; выбор делает
человек, сверяясь с раскрытием (см. DATA.md, п. 12).

Запуск:

    .venv/bin/python research/scripts/events.py discover
    .venv/bin/python research/scripts/events.py universe
    .venv/bin/python research/scripts/events.py build
"""

from __future__ import annotations

import csv
import datetime as dt
import sys

import iss
from jessica.sources import news

# Режимы «Д». Валютные (TQUD, TQED) включены, хотя рублёвые выпуски
# исследования их не касаются: пусть в списке событий будет весь рынок.
# TQOD — это облигации в долларах, к дефолту отношения не имеет.
BOARDS = ("TQRD", "EQDB", "TQUD", "TQED")

# История торгов облигациями в ISS начинается задолго до появления ВДО,
# но режим «Д» в нынешнем виде существует с середины 2010-х. Берём с запасом.
START = dt.date(2011, 1, 3)

SNAPSHOT_URL = (
    "https://iss.moex.com/iss/history/engines/stock/markets/bonds/boards/{board}"
    "/securities.json?iss.meta=off&date={date}&history.columns=SECID,SHORTNAME,BOARDID"
)
SECURITY_URL = "https://iss.moex.com/iss/securities/{secid}.json?iss.meta=off"
UNIVERSE_URL = (
    "https://iss.moex.com/iss/securities.json?iss.meta=off&group_by=group"
    "&group_by_filter=stock_bonds&is_trading={traded}"
)
BONDIZATION_URL = (
    "https://iss.moex.com/iss/statistics/engines/stock/markets/bonds/bondization/"
    "{secid}.json?iss.meta=off&limit=100"
)

OUT = iss.HOME / "research" / "out"

# Окно поиска неплатежа до даты перевода в «Д».
# Нижняя граница была 13 дней — по правилу «дефолт после 10 рабочих дней
# просрочки». Оказалось слишком строго: у выпуска RU000A0JRPK6 купон
# 2016-02-22, а перевод 2016-03-04, то есть 11 дней, и кандидат терялся.
# Биржа вправе признать дефолт сразу при отказе эмитента платить, минимальный
# измеренный разрыв — 3 дня. Поэтому нижняя граница 2 дня.
# Верхняя — с запасом на новогодние каникулы (расчёт по календарям дал
# до 31 дня) и на то, что биржа могла тянуть с переводом.
WINDOW_MIN = dt.timedelta(days=2)
WINDOW_MAX = dt.timedelta(days=60)


def _dates(step_days: int = 7) -> list[dt.date]:
    """Даты срезов: раз в неделю, по будням."""
    out = []
    day = START
    today = dt.date.today()
    while day <= today:
        while day.weekday() > 4:
            day += dt.timedelta(days=1)
        out.append(day)
        day += dt.timedelta(days=step_days)
    return out


def discover() -> dict[str, str]:
    """Какие выпуски когда-либо были в режимах «Д». Возвращает secid → имя."""
    found: dict[str, str] = {}
    dates = _dates()
    for number, date in enumerate(dates, 1):
        for board in BOARDS:
            url = SNAPSHOT_URL.format(board=board, date=date.isoformat())
            for row in iss.paged(url, "history"):
                found.setdefault(row["SECID"], row.get("SHORTNAME") or "")
        if number % 50 == 0 or number == len(dates):
            print(f"  срез {number}/{len(dates)} ({date}), выпусков найдено: {len(found)}")
    return found


def universe() -> list[dict]:
    """Справочник всех облигаций: и торгуемых, и снятых с торгов."""
    out = []
    for traded in (1, 0):
        rows = iss.paged(UNIVERSE_URL.format(traded=traded), "securities")
        print(f"  is_trading={traded}: {len(rows)} бумаг")
        out.extend(rows)
    return out


def _security(secid: str) -> tuple[dict, list[dict]]:
    """Карточка выпуска: описание как словарь и список режимов торгов."""
    payload = iss.get(SECURITY_URL.format(secid=secid))
    description = {row["name"]: row["value"] for row in iss.rows(payload, "description")}
    return description, iss.rows(payload, "boards")


def _default_date(boards: list[dict]) -> tuple[str | None, str | None]:
    """Самая ранняя дата включения в режим «Д» и сам режим."""
    dates = [
        (board["listed_from"], board["boardid"])
        for board in boards
        if board["boardid"] in BOARDS and board["listed_from"]
    ]
    return min(dates) if dates else (None, None)


def _payments(secid: str) -> list[tuple[str, str]]:
    """Плановые платежи выпуска: (дата, вид)."""
    payload = iss.get(BONDIZATION_URL.format(secid=secid))
    out = []
    for block, field, kind in (
        ("coupons", "coupondate", "купон"),
        ("amortizations", "amortdate", "амортизация"),
        ("offers", "offerdate", "оферта"),
    ):
        for row in iss.rows(payload, block):
            # В графике попадаются нулевые даты вида 0000-00-00 —
            # строку с такой датой пропускаем, платежа в ней нет.
            if not row.get(field):
                continue
            try:
                dt.date.fromisoformat(row[field])
            except ValueError:
                continue
            out.append((row[field], kind))
    return out


def build() -> None:
    """Собрать события: выпуски, эмитенты, кандидаты в дату неплатежа."""
    OUT.mkdir(parents=True, exist_ok=True)

    print("1. Срезы режимов «Д»")
    found = discover()

    print("2. Справочник облигаций")
    by_secid = {row["secid"]: row for row in universe()}
    print(f"  всего бумаг в справочнике: {len(by_secid)}")

    print("3. Карточки найденных выпусков")
    issues = []
    for number, secid in enumerate(sorted(found), 1):
        description, boards = _security(secid)
        if not description:
            continue
        date, board = _default_date(boards)
        reference = by_secid.get(secid, {})
        issues.append(
            {
                "secid": secid,
                "isin": description.get("ISIN") or "",
                "shortname": description.get("SHORTNAME") or found[secid],
                "emitent_id": reference.get("emitent_id") or description.get("EMITTER_ID") or "",
                "emitent_inn": reference.get("emitent_inn") or "",
                "emitent_title": reference.get("emitent_title") or "",
                "transfer_date": date or "",
                "transfer_board": board or "",
                "has_default": description.get("HASDEFAULT") or "",
                "has_technical_default": description.get("HASTECHNICALDEFAULT") or "",
                "matdate": description.get("MATDATE") or "",
                "issuedate": description.get("ISSUEDATE") or "",
                "issuesize": description.get("ISSUESIZE") or "",
                "listlevel": description.get("LISTLEVEL") or "",
                "type": description.get("TYPE") or "",
                "faceunit": description.get("FACEUNIT") or "",
            }
        )
        if number % 50 == 0 or number == len(found):
            print(f"  карточка {number}/{len(found)}")

    _write(OUT / "events_issues.csv", issues)
    print(f"  записано выпусков: {len(issues)} → {OUT / 'events_issues.csv'}")

    print("4. Эмитенты и кандидаты в дату неплатежа")
    issuers = _issuers(issues, by_secid)
    _write(OUT / "events_issuers.csv", issuers)
    print(f"  записано эмитентов: {len(issuers)} → {OUT / 'events_issuers.csv'}")

    _summary(issues, issuers)


def _issuers(issues: list[dict], by_secid: dict[str, dict]) -> list[dict]:
    """Свернуть выпуски в события эмитентов и подобрать кандидатов."""
    groups: dict[str, list[dict]] = {}
    for issue in issues:
        key = str(issue["emitent_id"] or issue["emitent_title"] or issue["secid"])
        groups.setdefault(key, []).append(issue)

    # Все выпуски эмитента, а не только переведённые: неплатёж мог случиться
    # по выпуску, который в тот же день погашался и в режим «Д» не попал
    # (так было у МОНОП 1P02).
    siblings: dict[str, list[str]] = {}
    for secid, row in by_secid.items():
        key = str(row.get("emitent_id") or "")
        if key:
            siblings.setdefault(key, []).append(secid)

    out = []
    for key, group in sorted(groups.items()):
        dates = [issue["transfer_date"] for issue in group if issue["transfer_date"]]
        transfer = min(dates) if dates else ""
        candidates: list[str] = []
        if transfer:
            border = dt.date.fromisoformat(transfer)
            # Сам дефолтный выпуск обязательно, плюс остальные выпуски
            # эмитента из справочника. Справочник бывает неполным: у эмитента
            # 654 (РТК-Лизинг) в нём три выпуска, а дефолтного BO-02 нет,
            # и раньше его платежи терялись вместе с кандидатами.
            pool = {issue["secid"] for issue in group} | set(siblings.get(key, []))
            for secid in sorted(pool):
                for date, kind in _payments(secid):
                    moment = dt.date.fromisoformat(date)
                    if border - WINDOW_MAX <= moment <= border - WINDOW_MIN:
                        candidates.append(f"{date} {kind} {secid}")
        # Купон и амортизация часто приходятся на один день одного выпуска:
        # это один платёж, а не два кандидата. Схлопываем по дате и выпуску.
        merged: dict[tuple[str, str], list[str]] = {}
        for candidate in candidates:
            date, kind, secid = candidate.split(" ", 2)
            merged.setdefault((date, secid), []).append(kind)
        candidates = [
            f"{date} {'+'.join(sorted(set(kinds)))} {secid}"
            for (date, secid), kinds in merged.items()
        ]
        candidates.sort()
        first = group[0]
        out.append(
            {
                "emitent_id": key,
                "emitent_inn": first["emitent_inn"],
                "emitent_title": first["emitent_title"] or first["shortname"],
                "transfer_date": transfer,
                "issues_in_default": len(group),
                "issues_total": len(siblings.get(key, [])),
                "day_zero_candidates": len(candidates),
                "candidates": " | ".join(candidates),
                "needs_review": "да" if len(candidates) != 1 else "",
                "secids": " ".join(issue["secid"] for issue in group),
                # Валюта номинала: исследование про рублёвые выпуски, а в режим
                # «Д» попадали и валютные бумаги иностранных эмитентов
                # (Raiffeisen, EVRAZ, Celtic Resources — 2023-09-04).
                "faceunit": first["faceunit"],
                # Выгрузка у Интерфакса платная, поиск по компании — бесплатный,
                # поэтому спорные случаи человек открывает по этой ссылке руками.
                "disclosure": news.disclosure_link(first["emitent_title"] or first["shortname"]),
            }
        )
    return out


def _write(path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _summary(issues: list[dict], issuers: list[dict]) -> None:
    """Что получилось: по годам, по флагам, сколько случаев на ручную проверку."""
    print("\nИТОГО")
    print(f"  выпусков в режиме «Д»: {len(issues)}")
    print(f"  эмитентов: {len(issuers)}")

    by_year: dict[str, int] = {}
    for issuer in issuers:
        year = issuer["transfer_date"][:4] or "без даты"
        by_year[year] = by_year.get(year, 0) + 1
    print("  эмитентов по годам перевода:")
    for year in sorted(by_year):
        print(f"    {year}: {by_year[year]}")

    flagged = sum(1 for issue in issues if issue["has_default"] == "1")
    print(f"  из них с флагом HASDEFAULT=1: {flagged} ({flagged / max(len(issues), 1):.0%})")

    review = [issuer for issuer in issuers if issuer["needs_review"]]
    print(f"  эмитентов на ручную проверку (кандидатов не ровно один): {len(review)}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "build"
    try:
        if command == "discover":
            found = discover()
            print(f"выпусков найдено: {len(found)}")
        elif command == "universe":
            print(f"бумаг в справочнике: {len(universe())}")
        else:
            build()
    except iss.SourceUnavailable as error:
        print(f"\nОстановка: {error}")
        sys.exit(1)
