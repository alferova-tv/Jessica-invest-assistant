"""Новые отчёты из таблицы smart-lab, без пересказа финансовых показателей."""

from __future__ import annotations

import datetime as dt
import logging
import re
import urllib.parse

from bs4 import BeautifulSoup

SOURCE = "smart-lab-reports"
URL = "https://smart-lab.ru/q/shares_fundamental3/"
log = logging.getLogger(__name__)


def document_key(url: str) -> str:
    """Один файл e-disclosure независимо от www и регистра Fileid/FileLoad."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.removeprefix("www.") == "e-disclosure.ru":
        query = {k.lower(): v for k, v in urllib.parse.parse_qsl(parts.query)}
        if parts.path.lower() == "/portal/fileload.ashx" and query.get("fileid", "").isdigit():
            return f"e-disclosure:{query['fileid']}"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def parse(raw: str) -> list[dict]:
    soup = BeautifulSoup(raw, "html.parser")
    items = {}
    found_table = False
    for table in soup.select("table"):
        headers = [cell.get_text(" ", strip=True) for cell in table.select("tr:first-child th")]
        if not {"Название", "отчет", "дата публикации"}.issubset(headers):
            continue
        found_table = True
        for row in table.select("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) != len(headers):
                continue
            company = cells[headers.index("Название")].get_text(" ", strip=True)
            label = cells[headers.index("отчет")].get_text(" ", strip=True)
            period = re.fullmatch(r"(\d{4}(?:Q[1-4])?)-(МСФО|РСБУ)", label)
            link = row.select_one('a[title="Открыть файл отчета"][href]')
            if not company or not period or link is None:
                continue
            url = urllib.parse.urljoin(URL, link["href"].strip())
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                continue
            try:
                date = dt.datetime.strptime(cells[headers.index("дата публикации")].get_text(strip=True), "%d.%m.%Y").date()
            except ValueError:
                continue
            forum = row.select_one('a[href^="/forum/"]')
            key = document_key(url)
            items[key] = {
                "title": f"{company} · {period[1]} · {period[2]}",
                "company": company, "period": period[1], "standard": period[2],
                "ticker": forum["href"].strip("/").split("/")[-1] if forum else "",
                "url": url, "date": date.isoformat(), "source": SOURCE,
                "document_key": key,
            }
    if not found_table:
        raise ValueError("таблица отчётов не найдена: изменился формат или получена страница защиты")
    return list(items.values())


def fetch(*, timeout: float = 20) -> list[dict]:
    # Общие повторы и HTTP-настройки новостей, без отдельной браузерной сессии.
    from .news import _download

    raw = _download(SOURCE, URL, timeout)
    if not raw:
        return []
    try:
        return parse(raw)
    except ValueError as exc:
        log.warning("Отчётность smart-lab недоступна: %s", exc)
        return []
