"""Расчёт портфеля из операций: позиции, прибыль, доходность, доходы, структура.

Правила учёта — те, что ждёшь от сервиса учёта инвестиций:

* Себестоимость — по средней цене покупки. Продажа списывает среднюю
  себестоимость проданных штук, разница — реализованная прибыль.
* НКД — не часть себестоимости. Уплаченный при покупке НКД вернётся первым
  купоном, поэтому купонный доход показывается и «как пришёл», и за вычетом
  уплаченного НКД («чистый купонный доход»).
* Амортизация — возврат вложенного, а не доход: уменьшает себестоимость.
  Погашение закрывает позицию, разница с себестоимостью — реализованная прибыль.
* Комиссии и налоги — отдельной строкой, в прибыль входят с минусом.
* Доходность годовых — XIRR по денежным потокам бумаг: покупки с минусом,
  выплаты и продажи с плюсом, текущая стоимость — последним потоком сегодня.
  Свободные деньги на счёте в неё не входят: брокер не отдаёт их историю целиком.

Всё в рублях: валютных бумаг в портфелях нет, и курс не пересчитывается.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import defaultdict

from . import db

PORTFOLIO_TITLES = {"tinkoff": "Т-Инвестиции", "alpha": "Альфа"}
EPS = 1e-9


def _position(portfolio: str, isin: str) -> dict:
    return {
        "portfolio": portfolio, "isin": isin, "quantity": 0.0, "cost": 0.0, "realized": 0.0,
        "coupons": 0.0, "dividends": 0.0, "amortization": 0.0, "redemption": 0.0,
        "accrued_paid": 0.0, "accrued_received": 0.0, "fees": 0.0, "taxes": 0.0, "bonus": 0.0,
        "invested": 0.0, "first_date": None, "flags": set(),
    }


def _apply(pos: dict, op: sqlite3.Row) -> None:
    kind, quantity, amount, accrued = op["kind"], op["quantity"] or 0.0, op["amount"], op["accrued"] or 0.0
    pos["fees"] += op["fee"] or 0.0
    if kind in (db.BUY, db.OPENING):
        clean = -amount - accrued
        pos["quantity"] += quantity
        pos["cost"] += clean
        pos["invested"] += clean
        pos["accrued_paid"] += accrued
        pos["first_date"] = pos["first_date"] or op["date"]
        if kind == db.OPENING:
            pos["flags"].add("jessica" if op["source"] == "jessica" else "opening")
    elif kind == db.SELL:
        if pos["quantity"] < quantity - EPS:
            pos["flags"].add("oversold")  # продано больше, чем куплено: истории не хватает
        sold = min(quantity, pos["quantity"])
        average = pos["cost"] / pos["quantity"] if pos["quantity"] > EPS else 0.0
        clean = amount - accrued
        pos["realized"] += clean - average * sold
        pos["cost"] -= average * sold
        pos["quantity"] -= sold
        pos["accrued_received"] += accrued
    elif kind == db.COUPON:
        pos["coupons"] += amount
    elif kind == db.DIVIDEND:
        pos["dividends"] += amount
    elif kind == db.AMORTIZATION:
        pos["amortization"] += amount
        pos["cost"] -= amount
    elif kind == db.REDEMPTION:
        pos["redemption"] += amount
        pos["realized"] += amount - pos["cost"]
        pos["cost"] = 0.0
        pos["quantity"] = 0.0
    elif kind == db.TAX:
        pos["taxes"] += amount
    elif kind == db.BONUS:
        pos["bonus"] += amount
    if abs(pos["quantity"]) < EPS:
        pos["quantity"], pos["cost"] = 0.0, (pos["cost"] if abs(pos["cost"]) > 0.005 else 0.0)


def xirr(flows: list[tuple[dt.date, float]]) -> float | None:
    """Годовая доходность по нерегулярным потокам. None — если не сходится."""
    flows = [(d, a) for d, a in flows if abs(a) > 0.005]
    if len(flows) < 2 or all(a >= 0 for _, a in flows) or all(a <= 0 for _, a in flows):
        return None
    start = min(d for d, _ in flows)
    years = [((d - start).days / 365.0, a) for d, a in flows]
    if max(t for t, _ in years) < 1 / 365:
        return None

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** t for t, a in years)

    low, high = -0.99, 10.0
    if npv(low) * npv(high) > 0:
        return None
    for _ in range(200):  # деление пополам: медленнее Ньютона, зато не убегает
        mid = (low + high) / 2
        if npv(low) * npv(mid) <= 0:
            high = mid
        else:
            low = mid
        if high - low < 1e-7:
            break
    return (low + high) / 2


def _month(date: str) -> str:
    return date[:7]


def _add_months(day: dt.date, months: int) -> dt.date:
    month = day.month - 1 + months
    return dt.date(day.year + month // 12, month % 12 + 1, 1)


def build(conn: sqlite3.Connection, portfolio: str | None = None, today: dt.date | None = None) -> dict:
    """Всё, что показывает интерфейс, для одного портфеля или для всех (None)."""
    today = today or dt.date.today()
    where, args = ("WHERE portfolio = ?", (portfolio,)) if portfolio else ("", ())
    # Входящий остаток — первым в своём дне: сделки того же дня идут уже от него.
    ops = conn.execute(
        f"SELECT * FROM operations {where} ORDER BY date, kind != 'opening', id", args
    ).fetchall()
    securities = {row["isin"]: dict(row) for row in conn.execute("SELECT * FROM securities")}
    quotes = {row["isin"]: dict(row) for row in conn.execute("SELECT * FROM quotes")}

    positions: dict[tuple[str, str], dict] = {}
    loose = defaultdict(float)  # комиссии, налоги, бонусы без привязки к бумаге
    flows: list[tuple[dt.date, float]] = []
    received = defaultdict(lambda: defaultdict(float))  # месяц → вид → сумма
    for op in ops:
        if op["kind"] in (db.DEPOSIT, db.WITHDRAWAL, db.OTHER):
            continue
        flows.append((dt.date.fromisoformat(op["date"]), op["amount"] - (op["fee"] or 0.0)))
        if op["kind"] in (db.COUPON, db.DIVIDEND, db.BONUS, db.AMORTIZATION, db.REDEMPTION):
            received[_month(op["date"])][op["kind"]] += op["amount"]
        if not op["isin"]:
            loose[op["kind"]] += op["amount"]
            loose["fees"] += op["fee"] or 0.0
            continue
        key = (op["portfolio"], op["isin"])
        positions.setdefault(key, _position(*key))
        _apply(positions[key], op)

    rows = []
    for (pf, isin), pos in positions.items():
        security = securities.get(isin, {})
        quote = quotes.get(isin)
        quantity = pos["quantity"]
        if quote:
            price, accrued_unit = quote["price"], quote["accrued"]
        else:
            price = pos["cost"] / quantity if quantity > EPS else 0.0
            accrued_unit = 0.0
            if quantity > EPS:
                pos["flags"].add("no_quote")
        value = quantity * price
        accrued_now = quantity * accrued_unit
        unrealized = value - pos["cost"] if quantity > EPS else 0.0
        income = pos["coupons"] + pos["dividends"] + pos["bonus"]
        profit = (unrealized + pos["realized"] + income + pos["accrued_received"] - pos["accrued_paid"]
                  + accrued_now - pos["fees"] + pos["taxes"])
        rows.append({
            "portfolio": pf, "isin": isin,
            "name": security.get("name") or isin, "kind": security.get("kind") or "other",
            "issuer": security.get("issuer") or "", "maturity": security.get("maturity"),
            "facevalue": security.get("facevalue"),
            "quantity": quantity, "cost": pos["cost"],
            "avg_price": pos["cost"] / quantity if quantity > EPS else None,
            "price": price if quantity > EPS else None,
            "price_pct": (price / security["facevalue"] * 100) if quantity > EPS and security.get("facevalue") and security.get("kind") == "bond" else None,
            "accrued_now": accrued_now, "value": value, "value_full": value + accrued_now,
            "unrealized": unrealized, "realized": pos["realized"],
            "coupons": pos["coupons"], "dividends": pos["dividends"], "bonus": pos["bonus"],
            "amortization": pos["amortization"], "redemption": pos["redemption"],
            "accrued_paid": pos["accrued_paid"], "accrued_received": pos["accrued_received"],
            "fees": pos["fees"], "taxes": pos["taxes"], "invested": pos["invested"],
            "profit": profit, "profit_pct": profit / pos["invested"] * 100 if pos["invested"] > EPS else None,
            "first_date": pos["first_date"],
            "quote_date": quote["date"] if quote else None,
            "flags": sorted(pos["flags"]),
        })
    open_rows = [r for r in rows if r["quantity"] > EPS]
    total_value = sum(r["value_full"] for r in open_rows)
    for row in rows:
        row["share"] = row["value_full"] / total_value * 100 if total_value > EPS and row["quantity"] > EPS else 0.0
    rows.sort(key=lambda r: (-r["value_full"], r["name"]))

    if total_value > EPS:
        flows.append((today, total_value))
    summary = _summary(rows, loose, total_value)
    summary["xirr"] = xirr(flows)

    return {
        "portfolio": portfolio,
        "today": today.isoformat(),
        "summary": summary,
        "positions": rows,
        "income": _income(conn, received, open_rows, today),
        "structure": _structure(open_rows, total_value),
        "upcoming": _upcoming(conn, open_rows, today),
        "reconcile": _reconcile(open_rows, portfolio),
    }


def _summary(rows: list[dict], loose: dict, total_value: float) -> dict:
    def total(field: str) -> float:
        return sum(r[field] for r in rows)

    open_rows = [r for r in rows if r["quantity"] > EPS]
    coupons, accrued_paid, accrued_received = total("coupons"), total("accrued_paid"), total("accrued_received")
    income = coupons + total("dividends") + total("bonus") + loose.get(db.BONUS, 0.0)
    fees = total("fees") + loose.get("fees", 0.0)
    taxes = total("taxes") + loose.get(db.TAX, 0.0)
    unrealized = sum(r["unrealized"] for r in open_rows)
    accrued_now = sum(r["accrued_now"] for r in open_rows)
    profit = (unrealized + total("realized") + income + accrued_received - accrued_paid + accrued_now
              - fees + taxes)
    invested = sum(r["cost"] for r in open_rows)
    return {
        "value": total_value,
        "cost": invested,
        "accrued_now": accrued_now,
        "unrealized": unrealized,
        "unrealized_pct": unrealized / invested * 100 if invested > EPS else None,
        "realized": total("realized"),
        "coupons": coupons,
        "coupons_net": coupons - accrued_paid + accrued_received,
        "dividends": total("dividends"),
        "bonus": total("bonus") + loose.get(db.BONUS, 0.0),
        "income": income,
        "amortization": total("amortization") + total("redemption"),
        "accrued_paid": accrued_paid,
        "accrued_received": accrued_received,
        "fees": fees,
        "taxes": taxes,
        "profit": profit,
        "invested_total": total("invested"),
        "positions": len(open_rows),
        "flags": sorted({flag for r in open_rows for flag in r["flags"]}),
    }


def _future(conn: sqlite3.Connection, open_rows: list[dict], today: dt.date, until: dt.date) -> list[dict]:
    """Будущие выплаты по текущему количеству: купоны, амортизации, погашения."""
    held: dict[str, list[dict]] = defaultdict(list)
    for row in open_rows:
        held[row["isin"]].append(row)
    if not held:
        return []
    marks = ",".join("?" * len(held))
    events = []
    for payment in conn.execute(
        f"SELECT * FROM payments WHERE isin IN ({marks}) AND date > ? AND date <= ? ORDER BY date",
        [*held, today.isoformat(), until.isoformat()],
    ):
        if not payment["per_unit"]:  # не объявлена или нулевая строка графика ISS
            continue
        for row in held[payment["isin"]]:
            events.append({
                "date": payment["date"], "kind": payment["kind"], "isin": payment["isin"],
                "name": row["name"], "portfolio": row["portfolio"], "quantity": row["quantity"],
                "per_unit": payment["per_unit"], "amount": payment["per_unit"] * row["quantity"],
                "estimated": bool(payment["estimated"]),
            })
    return events


def _income(conn, received, open_rows, today) -> dict:
    """Помесячно: полученное (прошлое) и ожидаемое на 12 месяцев вперёд."""
    until = _add_months(today, 13) - dt.timedelta(days=1)
    expected = defaultdict(lambda: defaultdict(float))
    estimated_months = set()
    for event in _future(conn, open_rows, today, until):
        expected[_month(event["date"])][event["kind"]] += event["amount"]
        if event["estimated"]:
            estimated_months.add(_month(event["date"]))
    months = sorted(set(received) | set(expected))
    if months:
        first = dt.date.fromisoformat(months[0] + "-01")
        last = dt.date.fromisoformat(months[-1] + "-01")
        months, cursor = [], first
        while cursor <= last:
            months.append(cursor.strftime("%Y-%m"))
            cursor = _add_months(cursor, 1)
    series = [{
        "month": m,
        "received": {k: round(v, 2) for k, v in received.get(m, {}).items()},
        "expected": {k: round(v, 2) for k, v in expected.get(m, {}).items()},
        "estimated": m in estimated_months,
    } for m in months]
    next12 = sum(v for m in expected.values() for k, v in m.items() if k in (db.COUPON, db.DIVIDEND))
    year = str(today.year)
    this_year = sum(v for m, kinds in received.items() if m.startswith(year)
                    for k, v in kinds.items() if k in db.INCOME)
    return {
        "months": series,
        "expected_12m": next12,
        "expected_12m_monthly": next12 / 12,
        "received_this_year": this_year,
        "capital_back_12m": sum(v for m in expected.values() for k, v in m.items()
                                if k in (db.AMORTIZATION, db.REDEMPTION)),
    }


def _upcoming(conn, open_rows, today) -> list[dict]:
    events = _future(conn, open_rows, today, today + dt.timedelta(days=90))
    return sorted(events, key=lambda e: (e["date"], e["name"]))


def _structure(open_rows: list[dict], total: float) -> dict:
    def group(key) -> list[dict]:
        bucket = defaultdict(float)
        for row in open_rows:
            bucket[key(row)] += row["value_full"]
        items = sorted(({"label": k, "value": v, "share": v / total * 100 if total else 0}
                        for k, v in bucket.items()), key=lambda x: -x["value"])
        return items

    kinds = {"bond": "Облигации", "share": "Акции", "fund": "Фонды", "other": "Прочее"}

    def maturity(row):
        if row["kind"] != "bond":
            return "не облигации"
        return (row["maturity"] or "?")[:4]

    def issuer(row):
        return _short_issuer(row["issuer"]) or row["name"]

    return {
        "kind": group(lambda r: kinds.get(r["kind"], "Прочее")),
        "portfolio": group(lambda r: PORTFOLIO_TITLES.get(r["portfolio"], r["portfolio"])),
        "issuer": group(issuer),
        "maturity": sorted(group(maturity), key=lambda x: x["label"]),
        "position": sorted(({"label": r["name"], "value": r["value_full"], "share": r["share"]} for r in open_rows),
                           key=lambda x: -x["value"]),
    }


def _short_issuer(name: str) -> str:
    """«Публичное акционерное общество "Газпром"» → «Газпром»."""
    import re

    name = (name or "").strip()
    match = re.search(r'"([^"]+(?:"[^"]+")?[^"]*)"\s*$', name) or re.search(r"[«\"](.+?)[»\"]", name)
    if match:
        return match.group(1).strip('"« »')
    return re.sub(r"^(ПАО|АО|ООО|ОАО|МКПАО)\s+", "", name)


def _reconcile(open_rows: list[dict], portfolio: str | None) -> list[dict]:
    """Расхождения с составом, который видит Jessica (брокерский отчёт / API)."""
    from .sync import jessica_positions

    ours = defaultdict(float)
    names = {}
    for row in open_rows:
        ours[(row["portfolio"], row["isin"])] += row["quantity"]
        names[row["isin"]] = row["name"]
    theirs = defaultdict(float)
    for position in jessica_positions():
        if portfolio and position.get("portfolio") != portfolio:
            continue
        theirs[(position["portfolio"], position["isin"])] += position["quantity"]
        names.setdefault(position["isin"], position.get("name") or position["isin"])
    diff = []
    for key in sorted(set(ours) | set(theirs)):
        if abs(ours.get(key, 0) - theirs.get(key, 0)) > EPS:
            diff.append({"portfolio": key[0], "isin": key[1], "name": names.get(key[1], key[1]),
                         "ours": ours.get(key, 0), "jessica": theirs.get(key, 0)})
    return diff


def operations(conn: sqlite3.Connection, portfolio: str | None = None, limit: int = 2000) -> list[dict]:
    where, args = ("WHERE o.portfolio = ?", [portfolio]) if portfolio else ("", [])
    rows = conn.execute(
        f"SELECT o.*, s.name FROM operations o LEFT JOIN securities s ON s.isin = o.isin {where} "
        f"ORDER BY o.date DESC, o.id DESC LIMIT ?", [*args, limit],
    )
    return [dict(row) for row in rows]


def last_sync(conn: sqlite3.Connection) -> dict | None:
    raw = db.get_meta(conn, "last_sync")
    return json.loads(raw) if raw else None
