"""Отправка уведомлений в Telegram.

Канал односторонний: Jessica только пишет владельцу на chat_id из конфига
и не читает входящие сообщения. Посторонний, написавший боту, не получит ничего.
"""

from __future__ import annotations

import html
import logging
import time

import requests

from .config import Config

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4096  # предел длины сообщения в Telegram


def _chunks(text: str) -> list[str]:
    """Режем длинный текст по границам строк, чтобы не рвать посреди слова."""
    if len(text) <= LIMIT:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > LIMIT:
            if current:
                parts.append(current)
            # Одна строка длиннее лимита — режем как есть, деваться некуда.
            while len(line) > LIMIT:
                parts.append(line[:LIMIT])
                line = line[LIMIT:]
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


ATTEMPTS = 3


def _post(config: Config, part: str, preview: bool) -> bool:
    """Одна часть сообщения, с повторами: VPN изредка моргает, и разовый
    таймаут не должен стоить пропущенного купона."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            payload = requests.post(
                API.format(token=config.token, method="sendMessage"),
                json={
                    "chat_id": config.chat_id,
                    "text": part,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": not preview,
                },
                timeout=20,
            ).json()
            if payload.get("ok"):
                return True
            # Отказ самого Telegram (битая разметка, неверный chat_id) повтором не лечится.
            log.error("Telegram отклонил сообщение: %s", payload.get("description"))
            return False
        except requests.RequestException as exc:
            log.warning("Попытка %d из %d не удалась: %s", attempt, ATTEMPTS, type(exc).__name__)
            if attempt < ATTEMPTS:
                time.sleep(2 ** attempt)
    log.error("Сообщение не отправлено после %d попыток", ATTEMPTS)
    return False


def send(config: Config, text: str, *, preview: bool = False) -> bool:
    """Отправляет сообщение. Возвращает False, если хотя бы одна часть не ушла."""
    return all(_post(config, part, preview) for part in _chunks(text))


def escape(value: object) -> str:
    """Экранирование для parse_mode=HTML: названия эмитентов содержат кавычки и амперсанды."""
    return html.escape(str(value), quote=False)
