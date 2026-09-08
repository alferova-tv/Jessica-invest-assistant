"""Браузер для источников, закрытых от обычных запросов.

Часть нужных сайтов не отдаёт данные скрипту: e-disclosure требует пройти
JS-проверку антибота, Cbonds и dohod рисуют таблицы уже в браузере. Поэтому
здесь настоящий Chromium.

Профиль хранится на диске и переживает перезапуски: куку, выданную антиботом,
незачем добывать заново каждый час. Браузер поднимается один раз за проход
и обслуживает все источники — старт стоит несколько секунд, и платить их
трижды не за что.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Убираем самый заметный признак автоматизации: без этого navigator.webdriver
# равен true, и часть антиботов отсекает страницу ещё до отрисовки.
STEALTH = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


class Browser:
    """Тонкая обёртка: отдаёт HTML страницы, скрывая возню с Playwright."""

    def __init__(self, context):
        self._context = context

    def html(self, url: str, *, timeout: float = 45_000, settle: float = 1_500) -> str | None:
        """HTML страницы после того, как она перестанет перестраиваться.

        Антибот отдаёт сначала страницу-заглушку и уводит на настоящую редиректом,
        уже решив свою задачу. Читать содержимое в этот момент бесполезно — оно
        меняется под руками, поэтому дожидаемся, пока навигация уляжется, и берём
        содержимое с несколькими попытками.
        """
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            for state in ("load", "networkidle"):
                try:
                    page.wait_for_load_state(state, timeout=timeout)
                except Exception:  # noqa: BLE001 — страница может уйти на редирект
                    pass
            page.wait_for_timeout(settle)

            last: Exception | None = None
            for _ in range(3):
                try:
                    return page.content()
                except Exception as exc:  # noqa: BLE001 — «page is navigating»
                    last = exc
                    page.wait_for_timeout(settle)
            log.warning("Страница %s так и не устоялась: %s", url, str(last)[:100])
            return None
        except Exception as exc:  # noqa: BLE001 — Playwright бросает свои типы ошибок
            log.warning("Не удалось загрузить %s: %s", url, str(exc)[:120])
            return None
        finally:
            page.close()


@contextmanager
def session(profile: Path, *, headless: bool = True):
    """Браузер с постоянным профилем. Закрывается сам, даже если источник упал."""
    from playwright.sync_api import sync_playwright

    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=headless,
            user_agent=UA,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        context.add_init_script(STEALTH)
        try:
            yield Browser(context)
        finally:
            context.close()
