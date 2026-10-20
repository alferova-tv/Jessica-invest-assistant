"""Этап 3: история торгов, график платежей и описание всех корпоративных облигаций.

Что качается. Для каждого корпоративного выпуска из справочника ISS
(`exchange_bond`, `corporate_bond`, кэш `events.py universe`) три вещи:

1. `history` — дневная история торгов с 2014 года по всем режимам: цена,
   доходность, дюрация, оборот, дата погашения. Из неё потом строятся
   недельные признаки 1–8 и 10 и погашение для признака 9.
2. `bondization` — график выпуска: купоны, амортизации, оферты. Оферты
   нужны для признака 9, так как `OFFERDATE` в дневной истории пуст
   (DATA.md, «Поля дневной истории ISS и даты оферт»).
3. `describe` — описание выпуска, из него тип купона (флоатер или нет):
   `BONDTYPE` в истории пуст.
4. `buyback` — `BUYBACKDATE` из дневной истории с 2018 года: ближайшая
   оферта в том виде, как её знала биржа в день торгов. Для признака 9:
   в графике выпуска нет около четверти оферт (DATA.md, «Даты оферт:
   график против BUYBACKDATE»). Качается только по выпускам с торгами
   после 2018 года, отдельным запросом, чтобы не перекачивать историю.

Почему по выпускам, а не по дням. Срез одного дня не даёт средневзвешенную
цену и оборот за неделю, а срезы всех дней — столько же запросов, сколько
по выпускам, при этом кэш по выпускам проще дозагружать.

Всё идёт через `iss.py`: каждый ответ ложится на диск, повторный запуск
ничего не качает заново, после десяти отказов подряд сбор останавливается.

Запуск (из корня Jessica, долгие команды — в фоне под caffeinate):

    .venv/bin/python research/scripts/history.py count                # сколько выпусков
    .venv/bin/python research/scripts/history.py history 20           # пробный прогон на 20 выпусках
    caffeinate -i .venv/bin/python -u research/scripts/history.py history
    caffeinate -i .venv/bin/python -u research/scripts/history.py bondization
    caffeinate -i .venv/bin/python -u research/scripts/history.py describe
    caffeinate -i .venv/bin/python -u research/scripts/history.py buyback
"""

from __future__ import annotations

import sys
import time

import iss
import events

CORPORATE = {"exchange_bond", "corporate_bond"}
FIRST_DAY = "2014-01-01"

# Только нужные столбцы: ответ короче в несколько раз, а полный набор
# (50 столбцов) почти весь пуст в ранние годы.
COLUMNS = ",".join([
    "BOARDID", "TRADEDATE", "SECID", "NUMTRADES", "VALUE", "VOLUME",
    "WAPRICE", "CLOSE", "LEGALCLOSEPRICE", "YIELDCLOSE", "YIELDATWAP",
    "DURATION", "ACCINT", "MATDATE", "FACEVALUE", "FACEUNIT",
    "COUPONPERCENT", "COUPONVALUE",
])
HISTORY_URL = (
    "https://iss.moex.com/iss/history/engines/stock/markets/bonds/securities/{secid}.json"
    f"?iss.meta=off&from={FIRST_DAY}&history.columns={COLUMNS}"
)
# До 2018 года поле не заполнено (DATA.md, «Поля дневной истории ISS и даты оферт»).
BUYBACK_FROM = "2018-01-01"
BUYBACK_URL = (
    "https://iss.moex.com/iss/history/engines/stock/markets/bonds/securities/{secid}.json"
    f"?iss.meta=off&from={BUYBACK_FROM}&history.columns=TRADEDATE,BUYBACKDATE"
)
# limit=unlimited, а не 100, как в events.py: при limit=100 купоны обрезаются
# на сотом (RU000A0ZYDH0: 140 купонов, приходило 100), а оферты не листаются.
BONDIZATION_URL = (
    "https://iss.moex.com/iss/statistics/engines/stock/markets/bonds/bondization/"
    "{secid}.json?iss.meta=off&limit=unlimited"
)


def corporate() -> list[str]:
    """Коды всех корпоративных выпусков из справочника, по порядку."""
    return sorted({row["secid"] for row in events.universe() if row["type"] in CORPORATE})


def _run(name: str, fetch, limit: str = "") -> None:
    secids = corporate()
    if limit:
        secids = secids[: int(limit)]
    started = time.monotonic()
    for number, secid in enumerate(secids, 1):
        fetch(secid)
        if number % 200 == 0 or number == len(secids):
            spent = time.monotonic() - started
            left = spent / number * (len(secids) - number)
            print(f"  {name}: {number}/{len(secids)}, прошло {spent / 60:.0f} мин, "
                  f"осталось ~{left / 60:.0f} мин", flush=True)


def _history(secid: str) -> None:
    iss.paged(HISTORY_URL.format(secid=secid), "history")


def _buyback(secid: str) -> None:
    # История уже в кэше: по ней видно, торговался ли выпуск после 2018 года.
    rows = iss.paged(HISTORY_URL.format(secid=secid), "history")
    if rows and max(row["TRADEDATE"] for row in rows) >= BUYBACK_FROM:
        iss.paged(BUYBACK_URL.format(secid=secid), "history")


def _bondization(secid: str) -> None:
    iss.get(BONDIZATION_URL.format(secid=secid))


def _describe(secid: str) -> None:
    iss.get(events.SECURITY_URL.format(secid=secid))


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else ""
    limit = argv[2] if len(argv) > 2 else ""
    if command == "count":
        print(f"корпоративных выпусков в справочнике: {len(corporate())}")
    elif command == "history":
        _run("история", _history, limit)
    elif command == "bondization":
        _run("график", _bondization, limit)
    elif command == "describe":
        _run("описание", _describe, limit)
    elif command == "buyback":
        _run("оферты", _buyback, limit)
    else:
        print(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except iss.SourceUnavailable as error:
        print(f"Остановлено: {error}")
        sys.exit(1)
