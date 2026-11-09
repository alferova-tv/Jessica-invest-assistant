"""Командная строка Jessica.

    jessica doctor    проверить источники и канал связи
    jessica import    разобрать брокерские выгрузки
    jessica refresh   пересобрать календарь выплат
    jessica calendar  показать ближайшие выплаты
    jessica check     отправить уведомления на сегодня
    jessica market    сводка по рынку: индекс МосБиржи и курс доллара
    jessica run       один проход по расписанию (её запускает launchd)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from zoneinfo import ZoneInfo

from . import calendar as cal
from . import events, holdings, notify
from .config import Config

MOSCOW = ZoneInfo("Europe/Moscow")


def _log(config: Config) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(config.logs_dir / "jessica.log", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def refresh_due(state: dict, now: dt.datetime, retry_hours: int) -> bool:
    """Пора ли собирать состав. Раз в сутки; если сегодняшняя попытка не удалась
    (API брокера не ответил), повтор — не раньше чем через retry_hours."""
    if state.get("refreshed") == now.date().isoformat():
        return False
    try:
        attempt = dt.datetime.fromisoformat(state["refresh_attempt"])
    except (KeyError, ValueError):
        return True
    if now.tzinfo is not None:
        # Старые отметки без смещения записывались по местному времени Mac.
        attempt = attempt.astimezone(now.tzinfo)
    return attempt.date() != now.date() or now - attempt >= dt.timedelta(hours=retry_hours)


def _composition(data: dict) -> set[tuple[str, str, float]]:
    return {
        (key, position["isin"], position["quantity"])
        for key, block in data.get("portfolios", {}).items()
        for position in block.get("positions", [])
    }


def rebuild_needed(state: dict, now: dt.datetime, before: dict, after: dict) -> bool:
    """Пересобирать ли календарь. Первая попытка дня — всегда: у smart-lab
    обновляются купоны. Повторная — только если состав изменился, иначе это
    сорок секунд и полсотни запросов впустую."""
    first_today = not str(state.get("refresh_attempt", "")).startswith(now.date().isoformat())
    return first_today or _composition(before) != _composition(after)


def stale_portfolios(data: dict) -> list[str]:
    return [block["title"] for block in data.get("portfolios", {}).values() if block.get("stale")]


def _warn(data: dict) -> None:
    """То, что нельзя пропустить: устаревший состав и бумаги, по которым
    новости молча не найдутся."""
    from .sources import news

    for block in data["portfolios"].values():
        if block.get("stale"):
            since = (block.get("synced") or "").replace("T", " ") or block.get("source_file") or "неизвестно когда"
            reason = block.get("stale_reason", "API не ответил")
            print(f"⚠ {block['title']}: {reason}, состав от {since}")
        for position in block["positions"]:
            if news.silent(position["name"], position.get("issuer", "")):
                print(f"⚠ без маркеров: {position['name']} ({position['isin']}) — новости по бумаге не найдутся")


def cmd_import(config: Config, args) -> int:
    data = holdings.collect(config)
    for block in data["portfolios"].values():
        origin = block["source_file"] or ("T-Invest API" if block["format"] == "tinvest-api" else "выгрузки нет")
        print(f"\n{block['title']} — {origin} [{block['format']}]")
        for position in block["positions"]:
            print(
                f"  {position['kind']:6} {position['isin']:14}"
                f" {position['quantity']:>8.0f} шт  {position['name'][:36]}"
            )
        print(f"  всего позиций: {len(block['positions'])}")
        for skipped in block["skipped"]:
            print(f"  ПРОПУЩЕНО: {skipped[:120]}")
    print()
    _warn(data)
    return 0


def cmd_refresh(config: Config, args) -> int:
    _warn(holdings.collect(config))
    schedule = cal.build(config)
    print(f"событий в календаре: {len(schedule['events'])}")
    if schedule["incomplete"]:
        print("не удалось обновить купоны по выпускам:", ", ".join(schedule["incomplete"]))
    if schedule.get("incomplete_details"):
        print("не удалось обновить параметры выпусков:", ", ".join(schedule["incomplete_details"]))
    return 0


def cmd_calendar(config: Config, args) -> int:
    rows = cal.upcoming(config, days=args.days)
    total = 0.0
    for row in rows:
        amount = "ставка не объявлена" if row["total"] is None else f"{row['total']:>10.2f}"
        total += row["total"] or 0
        print(f"{row['date']}  {row['portfolio']:13} {row['name'][:34]:34} {amount}")
    print(f"\nвыплат: {len(rows)}, сумма: {total:,.2f} руб".replace(",", " "))
    return 0


def cmd_check(config: Config, args) -> int:
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    text = events.send_due(config, today, dry_run=args.dry_run)
    if text is None:
        print("не удалось отправить уведомление; повтори команду позже")
        return 1
    if not text:
        print("на эту дату событий нет")
    else:
        print(text if args.dry_run else "уведомление отправлено")
        if args.dry_run:
            print("\n(пробный прогон, ничего не отправлено)")
    return 0


def cmd_news(config: Config, args) -> int:
    import re

    text = events.send_news(config, dry_run=args.dry_run)
    print(re.sub(r"<[^>]+>", "", text) if text else "значимых новостей нет")
    return 0


def cmd_market(config: Config, args) -> int:
    import re

    text = events.send_market(config, dry_run=args.dry_run)
    print(re.sub(r"<[^>]+>", "", text) if text else "источники молчат, сводки нет")
    return 0


def cmd_doctor(config: Config, args) -> int:
    import requests
    from .sources.smartlab import fetch_coupons
    from .sources import market, news, trading_calendar

    print("Проверка источников и связи:\n")
    checks: list[tuple[str, bool, str]] = []

    coupons = fetch_coupons("SU26238RMFS4")
    checks.append(("smart-lab (купоны)", bool(coupons), f"{len(coupons)} купонов по ОФЗ 26238"))

    # Лент несколько, и падают они порознь: The Bell живёт за VPN и отваливается
    # первым. Молчащую ленту видно только здесь — в `news` она просто ничего не даёт.
    for name, count in news.feed_status():
        checks.append((f"лента {name}", count > 0, f"{count} записей"))

    trading_day = trading_calendar.get_day(
        dt.datetime.now(MOSCOW).date(), config.state_file("trading_calendar.json"), timeout=config.moex_timeout,
    )
    known_day = trading_day is not None and trading_day["is_traded"] is not None
    checks.append((
        "календарь Мосбиржи", known_day,
        ("торговый день" if trading_day["is_traded"] else "неторговый день")
        if known_day else "торговый статус неизвестен",
    ))

    # Источники утренней сводки. ISS Мосбиржи годами был закрыт из-под VPN
    # и может закрыться снова; за ним стоят Alor и smart-lab. Что именно
    # Все три источника запрашиваются для IMOEX.
    index = market.fetch_index(events.MARKET_INDEX, timeout=config.moex_timeout)
    checks.append((
        f"Мосбиржа ({events.MARKET_INDEX})",
        index is not None,
        f"{index['secid']} {index['value']} на {index['date']}"
        if index else "ни один источник индекса не ответил",
    ))
    usd = market.fetch_cbr_usd()
    checks.append((
        "ЦБ (курс доллара)",
        usd is not None,
        f"{usd['value']:.4f} ₽ на {usd['date']}" if usd else "cbr.ru не ответил",
    ))

    # VPN изредка моргает, и одна неудачная попытка — не повод пугать словом «СБОЙ».
    for attempt in range(3):
        try:
            payload = requests.get(
                "https://api.telegram.org/bot{}/getMe".format(config.token), timeout=20
            ).json()
            checks.append(
                ("Telegram", payload.get("ok", False),
                 "@" + payload.get("result", {}).get("username", "?"))
            )
            break
        except requests.RequestException as exc:
            if attempt == 2:
                checks.append(("Telegram", False, f"связь недоступна: {str(exc)[:44]}"))
            else:
                time.sleep(2)

    for name, path in (
        ("holdings.json", config.state_file("holdings.json")),
        ("schedule.json", config.state_file("schedule.json")),
    ):
        checks.append((name, path.exists(), str(path.parent)))

    for portfolio in config.portfolios:
        if portfolio.source == "tinvest":
            from .sources import tinvest

            bonds = tinvest.fetch_positions(config.tinvest_token, account=config.tinvest_account)
            checks.append((
                "T-Invest API",
                bonds is not None,
                f"{len(bonds)} позиций" if bonds is not None else "API не ответил",
            ))
            continue
        report = holdings.latest_report(portfolio)
        checks.append((f"выгрузка «{portfolio.title}»", report is not None, report.name if report else "нет файла"))

    for name, ok, detail in checks:
        print(f"  {'OK  ' if ok else 'СБОЙ'}  {name:26} {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def cmd_run(config: Config, args) -> int:
    """Один проход по расписанию: launchd будит Jessica ежечасно.

    Раз в сутки обновляем данные, утром рассылаем события дня, следом сводку
    по рынку, первого числа — календарь на месяц. Проход идемпотентен: повтор
    в тот же час ничего не задваивает.
    """
    now = dt.datetime.now(MOSCOW)
    marker = config.state_file("last_run.json")
    state = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
    today = now.date().isoformat()
    calendar_built = False

    # Состав — раз в сутки. Если API брокера не ответил, прошлый состав остаётся,
    # а сбор повторяется через refresh_retry_hours: VPN то пускает к брокеру, то нет.
    if refresh_due(state, now, config.refresh_retry_hours):
        path = config.state_file("holdings.json")
        before = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        after = holdings.collect(config)
        if rebuild_needed(state, now, before, after):
            cal.build(config)
            state["calendar_attempt"] = now.isoformat(timespec="seconds")
            calendar_built = True
        state["refresh_attempt"] = now.isoformat(timespec="seconds")
        if not stale_portfolios(after):
            state["refreshed"] = today

    schedule_path = config.state_file("schedule.json")
    if not calendar_built and schedule_path.exists():
        schedule = cal.load(config)
        pending = schedule.get("incomplete") or schedule.get("incomplete_details")
        attempt = state.get("calendar_attempt") or schedule.get("updated")
        if pending and refresh_due({"refresh_attempt": attempt}, now, config.refresh_retry_hours):
            cal.build(config, retry_failed=True)
            state["calendar_attempt"] = now.isoformat(timespec="seconds")

    # Новости смотрим каждый час: дефолт или отчётность ждать до утра незачем.
    events.send_news(config, now.date())

    if now.hour >= config.morning_hour and state.get("checked") != today:
        if events.send_due(config, now.date()) is not None:
            state["checked"] = today

    if now.hour >= config.morning_hour and now.day == 1 and state.get("monthly") != today:
        text = events.monthly_calendar(config, now.date())
        if not text or notify.send(config, text):
            state["monthly"] = today

    # Сводка по рынку — отдельным часом и отдельной отметкой: она зависит
    # от живых торгов, а события дня — только от календаря.
    if now.hour >= config.market_hour and state.get("market") != today:
        # Источники промолчали либо день неторговый/не подтверждён:
        # следующий проход проверит календарь и доступность ещё раз.
        if events.send_market(config, now.date(), scheduled=True):
            state["market"] = today

    marker.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jessica", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("import", help="разобрать брокерские выгрузки").set_defaults(run=cmd_import)
    sub.add_parser("refresh", help="пересобрать календарь").set_defaults(run=cmd_refresh)
    sub.add_parser("doctor", help="проверить источники и связь").set_defaults(run=cmd_doctor)

    news_parser = sub.add_parser("news", help="новости по эмитентам и дефолты")
    news_parser.add_argument("--dry-run", action="store_true", help="показать, но не отправлять")
    news_parser.set_defaults(run=cmd_news)
    market_parser = sub.add_parser("market", help="индекс МосБиржи и курс доллара")
    market_parser.add_argument("--dry-run", action="store_true", help="показать, но не отправлять")
    market_parser.set_defaults(run=cmd_market)

    sub.add_parser("run", help="проход по расписанию").set_defaults(run=cmd_run)

    calendar_parser = sub.add_parser("calendar", help="ближайшие выплаты")
    calendar_parser.add_argument("--days", type=int, default=30)
    calendar_parser.set_defaults(run=cmd_calendar)

    check = sub.add_parser("check", help="уведомления на дату")
    check.add_argument("--date", help="ГГГГ-ММ-ДД, по умолчанию сегодня")
    check.add_argument("--dry-run", action="store_true", help="показать, но не отправлять")
    check.set_defaults(run=cmd_check)

    args = parser.parse_args(argv)
    config = Config.load()
    _log(config)
    return args.run(config, args)


if __name__ == "__main__":
    raise SystemExit(main())
