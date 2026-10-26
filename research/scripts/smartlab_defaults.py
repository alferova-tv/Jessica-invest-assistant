"""Годовые списки дефолтов smart-lab: даты неплатежа для сверки.

Зачем. В данных биржи есть только дата перевода выпусков в режим «Д» —
это примерно на три недели позже самого неплатежа (см. DATA.md, п. 2).
Дата неплатежа нужна как день ноль. Первоисточник — сообщение эмитента
о неисполнении обязательств на сервере раскрытия, но выгрузка там платная,
а поиск идёт по одной компании. Поэтому массово даты берём отсюда,
а спорные случаи человек проверяет руками по ссылке из events_issuers.csv.

Списки — это текст, написанный людьми, с разным оформлением по годам.
Разбор намеренно грубый: вытаскиваем из строки название компании и все
даты, а решение принимает `events.py`, сопоставляя их с окном до перевода.
Всё, что не разобралось, попадает в отдельный файл, а не теряется молча.

Запуск:

    .venv/bin/python research/scripts/smartlab_defaults.py
"""

from __future__ import annotations

import csv
import datetime as dt
import re

import iss  # ради путей к данным и папки out

import requests
from bs4 import BeautifulSoup

PAGES = (
    "https://smart-lab.ru/finansoviy-slovar/russian-bonds-defaults",
    *[f"https://smart-lab.ru/finansoviy-slovar/defaults-{year}" for year in range(2018, 2027)],
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Даты в списках пишут по-разному: 13.01.2025, 13 января 2025 и просто «с 04.02»
# без года — год тогда берётся из адреса страницы.
DATE_DIGITS = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b")
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
DATE_WORDS = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b")

# Компания в начале строки: «ООО «Феррони» (техдефолт …», «ООО „Центр-резерв“ (…».
# Кавычки на страницах разные: « », „ “, " ".
COMPANY = re.compile(
    r"^\s*(?:[\d\.\)•👉\-–—]\s*)*"
    r"((?:ООО|АО|ПАО|НАО|ОАО|ЗАО|ИП)?\s*[«„\"]?[^(\n]{3,80}?)[»“\"]?\s*[\(,]"
)

KIND = re.compile(r"\b(техдефолт\w*|технический дефолт\w*|дефолт\w*)", re.I)


def _dates(text: str, default_year: int) -> list[str]:
    """Все даты строки. Без года — год берётся из адреса страницы."""
    out = []
    for day, month, year in DATE_DIGITS.findall(text):
        try:
            out.append(dt.date(int(year or default_year), int(month), int(day)).isoformat())
        except ValueError:
            continue
    for day, month, year in DATE_WORDS.findall(text):
        try:
            out.append(dt.date(int(year), MONTHS[month], int(day)).isoformat())
        except ValueError:
            continue
    return sorted(set(out))


def _lines(url: str) -> list[str]:
    response = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    if response.status_code != 200:
        print(f"  {url}: код {response.status_code}, пропускаем")
        return []
    soup = BeautifulSoup(response.text, "lxml")
    body = soup.find("div", class_="content") or soup
    lines = [line.strip() for line in body.get_text("\n").split("\n")]
    return [line for line in lines if len(line) > 25 and KIND.search(line)]


def collect() -> tuple[list[dict], list[str]]:
    parsed: list[dict] = []
    skipped: list[str] = []
    for url in PAGES:
        year_in_url = re.search(r"(20\d{2})", url)
        default_year = int(year_in_url.group(1)) if year_in_url else dt.date.today().year
        lines = _lines(url)
        print(f"  {url.rsplit('/', 1)[-1]}: строк с упоминанием дефолта — {len(lines)}")
        for line in lines:
            dates = _dates(line, default_year)
            company = COMPANY.match(line)
            if not dates or not company:
                skipped.append(f"{url} :: {line}")
                continue
            kind = KIND.search(line).group(1).lower()
            parsed.append(
                {
                    "company": company.group(1).strip(" «»\""),
                    "first_date": dates[0],
                    "all_dates": " ".join(dates),
                    "kind": "техдефолт" if kind.startswith("тех") or "технич" in kind else "дефолт",
                    "source": url,
                    "text": line,
                }
            )
    return parsed, skipped


if __name__ == "__main__":
    out = iss.HOME / "research" / "out"
    out.mkdir(parents=True, exist_ok=True)

    parsed, skipped = collect()
    if parsed:
        with (out / "smartlab_defaults.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(parsed[0]))
            writer.writeheader()
            writer.writerows(parsed)
    with (out / "smartlab_skipped.txt").open("w") as fh:
        fh.write("\n".join(skipped))

    print(f"\nразобрано записей: {len(parsed)}")
    print(f"не разобрано (в smartlab_skipped.txt): {len(skipped)}")
    print(f"файлы: {out}")
