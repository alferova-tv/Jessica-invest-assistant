"""Доступ к ISS Мосбиржи для исследования: запрос, кэш, постраничная выборка.

Отдельный слой нужен по двум причинам. Первая: ISS режется по IP выходного
узла VPN непредсказуемо (см. CLAUDE.md), а сбор идёт часами, поэтому каждый
ответ кладётся на диск и повторный запуск ничего не качает заново. Вторая:
данные исследования живут вне репозитория, рядом с остальными данными
Jessica, — репозиторий можно снести, не потеряв выгрузку.

Повтор при обрыве берётся из `jessica.sources.market._get`: там уже учтено,
что разовый таймаут у VPN — повод повторить, а не считать отказом.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import sys
import time

# Скрипты исследования лежат в research/scripts, а код Jessica — двумя
# уровнями выше. Без этого `from jessica…` не находится.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from jessica.sources import market

log = logging.getLogger(__name__)

HOME = pathlib.Path(os.environ.get("JESSICA_HOME", "~/Documents/Tracking/Invest")).expanduser()
RAW = HOME / "research" / "raw"

# Пауза между запросами: ISS публичный и без ключа, ведём себя как человек,
# листающий сайт. На полном сборе это часы, поэтому пауза небольшая.
PAUSE = 0.2
TIMEOUT = 25
# Сколько отказов подряд считаем блокировкой, а не разовым обрывом связи.
FAILURES_BEFORE_STOP = 10


class SourceUnavailable(RuntimeError):
    """ISS перестал отвечать: продолжать бессмысленно, нужно разобраться."""


_failures = 0


def _path(url: str) -> pathlib.Path:
    """Имя файла кэша: хвост пути плюс хэш от всего url, чтобы не путать запросы."""
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    tail = url.split("/iss/")[-1].split("?")[0].replace("/", "_")[:80]
    return RAW / f"{tail}.{digest}.json"


def get(url: str, *, refresh: bool = False) -> dict:
    """Ответ ISS как словарь. Сначала диск, потом сеть."""
    global _failures
    path = _path(url)
    if path.exists() and not refresh:
        with path.open() as fh:
            return json.load(fh)

    response = market._get(url, TIMEOUT)
    if response is None:
        _failures += 1
        if _failures >= FAILURES_BEFORE_STOP:
            raise SourceUnavailable(
                f"ISS не отвечает {_failures} раз подряд. Последний запрос: {url}. "
                "Скачанное сохранено, запустите сбор заново — он продолжит с этого места."
            )
        return {}
    _failures = 0

    payload = response.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    tmp.replace(path)
    time.sleep(PAUSE)
    return payload


def rows(payload: dict, block: str) -> list[dict]:
    """Таблица ISS (columns + data) как список словарей."""
    table = payload.get(block)
    if not table:
        return []
    return [dict(zip(table["columns"], row)) for row in table["data"]]


def paged(url: str, block: str, *, page: int = 100) -> list[dict]:
    """Все строки блока: ISS отдаёт по сотне и сообщает TOTAL в курсоре."""
    out: list[dict] = []
    start = 0
    while True:
        separator = "&" if "?" in url else "?"
        payload = get(f"{url}{separator}start={start}")
        chunk = rows(payload, block)
        out.extend(chunk)
        if not chunk:
            return out
        # Курсор с полем TOTAL есть не у всех эндпоинтов: у справочника
        # бумаг его нет вовсе, и раньше выгрузка обрывалась на первой сотне.
        # Поэтому курсор — только подсказка, а признак конца — пустая страница.
        cursor = rows(payload, f"{block}.cursor")
        start += page
        if cursor and start >= cursor[0]["TOTAL"]:
            return out
