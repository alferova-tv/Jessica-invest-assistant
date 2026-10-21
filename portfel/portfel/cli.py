"""Командная строка.

    portfel sync [--no-tinvest]   загрузить операции, цены и график выплат
    portfel serve [--port 4343]   открыть интерфейс на http://localhost:4343
    portfel summary [--portfolio alpha]   короткая сводка в терминал
"""

from __future__ import annotations

import argparse
import logging

from . import db, model, server, sync


def _rub(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _print_sync(report: dict) -> None:
    print("Добавлено операций:", ", ".join(f"{k}: {v}" for k, v in report["added"].items()) or "нет")
    print("Котировки:", report.get("quotes", "—"), " графики выплат:", report.get("schedules", "—"))
    for note in report["notes"]:
        print("•", note)
    for source in report["silent"]:
        print("⚠ не ответил:", source)
    if report.get("no_quote"):
        print("⚠ нет цены:", ", ".join(report["no_quote"]))
    if report["unknown"]:
        print("Нераспознанные строки:")
        for line in report["unknown"]:
            print("  ", line)


def _print_summary(conn, portfolio: str | None) -> None:
    report = model.build(conn, portfolio)
    s = report["summary"]
    print(f"Стоимость {_rub(s['value'])}  (НКД {_rub(s['accrued_now'])})")
    print(f"Вложено {_rub(s['cost'])}  курсовая {_rub(s['unrealized'])}")
    print(f"Купоны {_rub(s['coupons'])}  дивиденды {_rub(s['dividends'])}  комиссии {_rub(-s['fees'])}  налоги {_rub(s['taxes'])}")
    xirr = "—" if s["xirr"] is None else f"{s['xirr'] * 100:.1f} %"
    print(f"Прибыль {_rub(s['profit'])}  XIRR {xirr}")
    print(f"Ожидается за 12 мес. {_rub(report['income']['expected_12m'])}")
    if report["reconcile"]:
        print("Расхождения с составом Jessica:")
        for row in report["reconcile"]:
            print(f"  {row['portfolio']} {row['name']}: у нас {row['ours']:g}, у Jessica {row['jessica']:g}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="portfel")
    sub = parser.add_subparsers(dest="command", required=True)
    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--no-tinvest", action="store_true", help="не ходить в T-Invest API")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--port", type=int, default=4343)
    p_sum = sub.add_parser("summary")
    p_sum.add_argument("--portfolio")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "sync":
        _print_sync(sync.run(tinvest_api=not args.no_tinvest))
    elif args.command == "serve":
        server.serve(args.port)
    elif args.command == "summary":
        with db.session() as conn:
            _print_summary(conn, args.portfolio)
