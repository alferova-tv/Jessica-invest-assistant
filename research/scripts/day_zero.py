"""Этап 1, шаг 4: выбрать дату неплатежа среди кандидатов.

`events.py` даёт по каждому эмитенту список плановых платежей, попавших
в окно до перевода в режим «Д». У 67 эмитентов кандидат один, у остальных
их несколько или нет вовсе. Разбирать 110 случаев руками долго, и большую
часть можно снять данными торгов.

Идея простая. Эмитент обязан раскрыть неплатёж в тот же день, и рынок
реагирует сразу: цена падает и не отыгрывается. Значит, пропущенный платёж
отличается от прошедшего тем, что **после него цена обвалилась**. Проверяем
каждого кандидата и оставляем самого раннего, после которого случился обвал.

Реакция меряется по всем выпускам эмитента, а не только по тому, чей платёж
проверяется: дефолт по одному выпуску роняет цену всех. Берётся выпуск
с наибольшим числом сделок в окне — у неликвидных бумаг цена скачет
от одной сделки.

Третий источник — даты из списков smart-lab (`smartlab_defaults.py`).
Если имя эмитента там нашлось и дата попадает в окно, она считается
подтверждением.

Запуск (после events.py):

    .venv/bin/python research/scripts/day_zero.py
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
import re

import iss
import events

HISTORY_URL = (
    "https://iss.moex.com/iss/history/engines/stock/markets/bonds/securities/{secid}.json"
    "?iss.meta=off&from={since}&till={until}"
    "&history.columns=TRADEDATE,SECID,CLOSE,WAPRICE,NUMTRADES,VALUE"
)

OUT = iss.HOME / "research" / "out"

# Цена до платежа — медиана за 10 торговых дней перед ним; после — минимум
# за 10 торговых дней после. Обвалом считаем падение на 7% и глубже.
# Порог выбран с запасом: дневные колебания ликвидной бумаги редко дают
# больше 3–4%, а настоящий неплатёж роняет цену на десятки процентов
# (у МОНОП 1P01 за три дня со 88 до 36).
BEFORE_DAYS = 10
AFTER_DAYS = 10
DROP = -0.07

# Если все кандидаты укладываются в две недели, это один эпизод неплатежа:
# по выпуску сначала не проходит купон, через несколько дней — оферта.
# Днём ноль берём самый ранний: ошибиться в раннюю сторону безопаснее,
# поздняя дата затащила бы в признаки уже случившийся обвал.
SAME_EPISODE = dt.timedelta(days=14)

# Период исследования. События раньше 2014 года не берём: для них нет кривой
# доходности ОФЗ (она начинается в 2014-м) и почти нет графика платежей.
# Решение записано в DATA.md, раздел «Порядок этапов».
PERIOD_START = "2014-01-01"


def _history(secid: str, since: dt.date, until: dt.date) -> list[dict]:
    url = HISTORY_URL.format(secid=secid, since=since.isoformat(), until=until.isoformat())
    rows = iss.paged(url, "history")
    out = []
    for row in rows:
        price = row.get("WAPRICE") or row.get("CLOSE")
        if price and row.get("NUMTRADES"):
            out.append(
                {
                    "date": dt.date.fromisoformat(row["TRADEDATE"]),
                    "price": float(price),
                    "trades": row["NUMTRADES"],
                    "value": row.get("VALUE") or 0,
                }
            )
    out.sort(key=lambda item: item["date"])
    return out


def _reaction(history: list[dict], moment: dt.date) -> float | None:
    """Насколько упала цена после даты платежа. None — сделок мало."""
    before = [row["price"] for row in history if row["date"] < moment][-BEFORE_DAYS:]
    after = [row["price"] for row in history if row["date"] >= moment][:AFTER_DAYS]
    if len(before) < 3 or len(after) < 2:
        return None
    base = sorted(before)[len(before) // 2]
    return min(after) / base - 1 if base else None


def _normalize(name: str) -> str:
    """Имя компании для сопоставления: без формы, кавычек и регистра."""
    name = re.sub(r"\b(ООО|АО|ПАО|НАО|ОАО|ЗАО|ИП|Общество с ограниченной ответственностью"
                  r"|Акционерное общество|Публичное акционерное общество)\b", " ", name, flags=re.I)
    return re.sub(r"[^а-яёa-z0-9]+", "", name.lower())


def _manual() -> dict[str, dict]:
    """Даты, проверенные человеком по раскрытию. Они главнее любой автоматики.

    Файл лежит в репозитории (`research/manual/manual_day_zero.csv`), а не в папке
    данных: это результат ручной работы, его нельзя потерять вместе с кэшем.
    """
    path = pathlib.Path(__file__).resolve().parents[1] / "manual" / "manual_day_zero.csv"
    if not path.exists():
        return {}
    with path.open() as fh:
        return {_normalize(row["emitent_title"]): row for row in csv.DictReader(fh)}


def _disclosure() -> dict[str, dict]:
    """Итог разбора сообщений e-disclosure (`disclosure.py parse`) по эмитентам."""
    path = OUT / "disclosure_day_zero.csv"
    if not path.exists():
        return {}
    with path.open() as fh:
        return {row["emitent_id"]: row for row in csv.DictReader(fh)}


def _smartlab() -> list[dict]:
    path = OUT / "smartlab_defaults.csv"
    if not path.exists():
        return []
    with path.open() as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["key"] = _normalize(row["company"])
    return rows


def _issuer_issues(issuer: dict) -> list[str]:
    """Все выпуски эмитента: переведённые в «Д» плюс остальные из справочника."""
    pool = set(issuer["secids"].split())
    key = str(issuer.get("emitent_id") or "")
    if key:
        pool |= {row["secid"] for row in _universe() if str(row.get("emitent_id")) == key}
    return sorted(pool)


_UNIVERSE: list[dict] = []


def _universe() -> list[dict]:
    global _UNIVERSE
    if not _UNIVERSE:
        for traded in (1, 0):
            _UNIVERSE.extend(iss.paged(events.UNIVERSE_URL.format(traded=traded), "securities"))
    return _UNIVERSE


def _defaulted_issue(row: dict, issuer: dict, by_hand: dict | None) -> None:
    """Какой именно выпуск не заплатил.

    Это нужно не для даты, а для признаков: гипотеза исследования —
    проявляется ли сигнал на всех выпусках эмитента или только на том,
    по которому подходит платёж. Без известного выпуска её не проверить.

    Порядок: раскрытие → единственный платёж эмитента в этот день →
    неоднозначность (несколько выпусков платили в один день).
    """
    if by_hand and by_hand.get("secid"):
        row["defaulted_secid"] = by_hand["secid"]
        row["issue_confidence"] = "раскрытие"
        return
    if not row["day_zero"]:
        return

    # Ищем среди всех выпусков эмитента, а не только переведённых в «Д».
    # У МОНОПОЛИИ неплатёж случился на погашении выпуска 1P02, который
    # в тот же день ушёл с торгов и в режим «Д» не попал.
    matches = []
    for secid in _issuer_issues(issuer):
        for date, kind in events._payments(secid):
            if date == row["day_zero"]:
                matches.append((secid, kind))
    # Купон и амортизация одного выпуска в один день — это один платёж,
    # а не два разных выпуска. Неоднозначность считаем по выпускам.
    secids = {secid for secid, _ in matches}
    if len(secids) == 1:
        row["defaulted_secid"] = matches[0][0]
        row["issue_confidence"] = "единственный платёж в эту дату"
    elif matches:
        row["defaulted_secid"] = " ".join(sorted({secid for secid, _ in matches}))
        row["issue_confidence"] = "несколько выпусков платили в этот день"
    else:
        row["issue_confidence"] = "платежа в графике нет"


def _tech_defaults(rows: list[dict], manual: dict[str, dict], disclosure: dict[str, dict]) -> None:
    """Дата первого техдефолта и граница обрезки признаков.

    Решение владелицы 2026-09-25: целевых событий два. Дефолт — формальное
    событие, а первый техдефолт — практический: после него цена уже упала,
    и выйти из бумаги без потерь нельзя. Поэтому предупреждать надо до него,
    и признаки обрезаются по ранней из двух дат.

    Источник дат техдефолта — цепочки из `incidents.csv` (списки smart-lab).
    Где цепочки нет, техдефолт неизвестен: это не значит, что его не было.
    Дата из ручной проверки по раскрытию главнее всего, за ней — первый
    неплатёж из сообщений e-disclosure, и только потом цепочка smart-lab.
    """
    chains = {}
    path = OUT / "incidents.csv"
    if path.exists():
        with path.open() as fh:
            for row in csv.DictReader(fh):
                if row["first_incident"]:
                    chains[_normalize(row["emitent_title"])] = row["first_incident"]

    for row in rows:
        if not row["day_zero"]:
            continue
        key = _normalize(row["emitent_title"])
        tech = (
            (manual.get(key) or {}).get("tech_first")
            or (disclosure.get(row["emitent_id"]) or {}).get("disclosure_first")
            or chains.get(key, "")
        )
        # Техдефолт позже неплатежа по дефолту — это уже часть самого дефолта,
        # такую дату как «первую» не берём.
        row["tech_first"] = tech if tech and tech < row["day_zero"] else ""
        row["cutoff"] = row["tech_first"] or row["day_zero"]


def _disclosed(issuer: dict, disclosure: dict[str, dict]) -> bool:
    """Есть ли у эмитента день ноль из сообщений, в том же окне, что у smart-lab.

    Окно 70 дней до перевода отсекает ИА «Кама»: неплатёж 15.12.2017,
    перевод в «Д» только 03.07.2018. Такой случай разбирает человек.
    """
    found = disclosure.get(issuer["emitent_id"])
    day = (found or {}).get("disclosure_day_zero") or (found or {}).get("disclosure_unpaid")
    if not day or not issuer["transfer_date"]:
        return False
    gap = dt.date.fromisoformat(issuer["transfer_date"]) - dt.date.fromisoformat(day)
    return dt.timedelta(0) <= gap <= dt.timedelta(days=70)


def main() -> None:
    with (OUT / "events_issuers.csv").open() as fh:
        issuers = list(csv.DictReader(fh))
    smartlab = _smartlab()
    disclosure = _disclosure()
    manual = _manual()

    result = []
    for number, issuer in enumerate(issuers, 1):
        transfer = issuer["transfer_date"]
        candidates = [item for item in issuer["candidates"].split(" | ") if item]
        row = {
            "emitent_id": issuer["emitent_id"],
            "emitent_title": issuer["emitent_title"],
            "transfer_date": transfer,
            "candidates_total": len(candidates),
            "day_zero": "",
            "source": "",
            "drop": "",
            "smartlab_date": "",
            "details": "",
            "tech_first": "",
            "default_date": "",
            "cutoff": "",
            "defaulted_secid": "",
            "issue_confidence": "",
            "faceunit": issuer.get("faceunit", ""),
            "disclosure": issuer.get("disclosure", ""),
        }

        # 1. Что говорит smart-lab: даты неплатежа там названы прямо.
        key = _normalize(issuer["emitent_title"])
        if key and transfer:
            border = dt.date.fromisoformat(transfer)
            for record in smartlab:
                if not record["key"] or (record["key"] not in key and key not in record["key"]):
                    continue
                dates = [dt.date.fromisoformat(value) for value in record["all_dates"].split()]
                inside = [value for value in dates if border - dt.timedelta(days=70) <= value <= border]
                if inside:
                    row["smartlab_date"] = min(inside).isoformat()
                    break

        # 2. Реакция цены на каждого кандидата.
        history: dict[str, list[dict]] = {}
        secids = issuer["secids"].split()
        checked = []
        if transfer:
            window_start = dt.date.fromisoformat(transfer) - dt.timedelta(days=120)
            window_end = dt.date.fromisoformat(transfer) + dt.timedelta(days=20)
            for secid in secids:
                history[secid] = _history(secid, window_start, window_end)

        for candidate in candidates:
            date_text, kind, secid = candidate.split(" ", 2)
            moment = dt.date.fromisoformat(date_text)
            # Смотрим на самый ликвидный выпуск эмитента в окне.
            best = max(history.values(), key=lambda rows: sum(item["trades"] for item in rows), default=[])
            reaction = _reaction(best, moment)
            checked.append((moment, kind, secid, reaction))

        confirmed = [item for item in checked if item[3] is not None and item[3] <= DROP]
        row["details"] = " | ".join(
            f"{moment} {kind} {secid} {'нет данных' if reaction is None else f'{reaction:+.0%}'}"
            for moment, kind, secid, reaction in checked
        )

        # Старые события помечаем сразу: иначе в файле они выглядят как
        # работа, которую кто-то должен доделать.
        if transfer and transfer < PERIOD_START:
            row["source"] = "вне периода: до 2014 года"
            result.append(row)
            continue

        # Валютные выпуски иностранных эмитентов (Raiffeisen, EVRAZ,
        # Celtic Resources — все переведены 2023-09-04) в исследование
        # не входят: оно про рублёвый рынок.
        if issuer.get("faceunit") and issuer["faceunit"] != "SUR":
            row["source"] = "вне выборки: валютный выпуск"
            result.append(row)
            continue

        checked_by_hand = manual.get(_normalize(issuer["emitent_title"]))
        # Пустая дата в файле ручных проверок означает «исключён из выборки»:
        # так помечаются случаи, разобранные человеком и признанные
        # неподходящими (валютные SPV без отметки о валюте номинала).
        if checked_by_hand and not checked_by_hand["day_zero"]:
            row["source"] = "вне выборки: проверено вручную"
            row["details"] = checked_by_hand["note"]
            result.append(row)
            continue
        if checked_by_hand:
            row["day_zero"] = checked_by_hand["day_zero"]
            row["source"] = "раскрытие, проверено вручную"
            row["details"] = checked_by_hand["note"]
        elif _disclosed(issuer, disclosure):
            found = disclosure[issuer["emitent_id"]]
            row["day_zero"] = found["disclosure_day_zero"] or found["disclosure_unpaid"]
            row["source"] = "раскрытие e-disclosure" + ("" if found["disclosure_day_zero"] else ", неплатёж без слова «дефолт»")
            row["details"] = f"{found['kind']} {found['defaulted_isin']} {found['url']}".strip()
        elif row["smartlab_date"]:
            row["day_zero"] = row["smartlab_date"]
            row["source"] = "smart-lab"
        elif len(confirmed) == 1 or (confirmed and len(candidates) > 1):
            moment, kind, secid, reaction = min(confirmed)
            row["day_zero"] = moment.isoformat()
            row["drop"] = f"{reaction:+.0%}"
            row["source"] = "график + обвал цены"
        elif checked and max(m for m, *_ in checked) - min(m for m, *_ in checked) <= SAME_EPISODE:
            moment, kind, secid, reaction = min(checked)
            row["day_zero"] = moment.isoformat()
            row["drop"] = "" if reaction is None else f"{reaction:+.0%}"
            row["source"] = "один эпизод, ранняя дата"
        elif len(candidates) == 1:
            moment, kind, secid, reaction = checked[0]
            row["day_zero"] = moment.isoformat()
            row["drop"] = "" if reaction is None else f"{reaction:+.0%}"
            row["source"] = "единственный кандидат"
        else:
            row["source"] = "нужна ручная проверка"

        # Дата, когда техдефолт стал дефолтом, — из сообщения эмитента, если
        # оно про тот же неплатёж, что и день ноль (льготный срок до 25 дней).
        found = disclosure.get(issuer["emitent_id"]) or {}
        if row["day_zero"] and found.get("disclosure_default_date"):
            lag = (dt.date.fromisoformat(found["disclosure_default_date"]) - dt.date.fromisoformat(row["day_zero"])).days
            if 0 <= lag <= 25:
                row["default_date"] = found["disclosure_default_date"]
        _defaulted_issue(row, issuer, checked_by_hand)
        result.append(row)
        if number % 25 == 0 or number == len(issuers):
            print(f"  эмитент {number}/{len(issuers)}")

    _tech_defaults(result, manual, disclosure)

    with (OUT / "day_zero.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(result[0]))
        writer.writeheader()
        writer.writerows(result)

    print(f"\nзаписано: {OUT / 'day_zero.csv'}")
    by_source: dict[str, int] = {}
    for row in result:
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1
    print("откуда взялся день ноль:")
    for source, count in sorted(by_source.items(), key=lambda item: -item[1]):
        print(f"  {source}: {count}")

    decided = [row for row in result if row["day_zero"]]
    print(f"\nдата определена: {len(decided)} из {len(result)}")
    gaps = [
        (dt.date.fromisoformat(row["transfer_date"]) - dt.date.fromisoformat(row["day_zero"])).days
        for row in decided
        if row["transfer_date"]
    ]
    if gaps:
        gaps.sort()
        print(
            "разрыв «неплатёж → перевод», календарных дней: "
            f"медиана {gaps[len(gaps) // 2]}, "
            f"четверти {gaps[len(gaps) // 4]}–{gaps[3 * len(gaps) // 4]}, "
            f"мин {gaps[0]}, макс {gaps[-1]}"
        )


if __name__ == "__main__":
    try:
        main()
    except iss.SourceUnavailable as error:
        print(f"\nОстановка: {error}")
