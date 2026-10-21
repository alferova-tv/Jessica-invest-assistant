"""Один проход синхронизации: операции брокеров → справочник → цены → график выплат.

Каждый шаг независим: если T-Invest или ISS не ответили, остальное всё равно
обновляется, а в базе остаются прошлые цены и график с их датами. Итог прохода
(что загружено, что не распознано, кто молчал) пишется в meta.last_sync —
его показывает интерфейс.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3

from . import db, iss, paths
from .importers import alfa, tinvest

log = logging.getLogger(__name__)

# Бумаги, по которым ISS ничего не знает, не спрашиваем чаще раза в неделю.
RESOLVE_RETRY_DAYS = 7


def _regnumber_resolver(conn: sqlite3.Connection, report: dict):
    def resolve(regnumber: str) -> str | None:
        row = conn.execute("SELECT isin FROM securities WHERE upper(regnumber) = upper(?)", (regnumber,)).fetchone()
        if row:
            return row["isin"]
        try:
            info = iss.isin_by_regnumber(regnumber)
        except iss.Unavailable as exc:
            report["silent"].add(f"ISS ({exc})")
            return None
        if not info:
            return None
        info["updated"] = dt.date.today().isoformat()
        db.upsert_security(conn, info)
        return info["isin"]

    return resolve


def _store(conn: sqlite3.Connection, operations: list[dict]) -> int:
    added = 0
    for op in operations:
        if op.get("isin") and op.get("_name"):
            db.upsert_security(conn, {"isin": op["isin"], "name": op["_name"], "kind": op.get("_kind")})
        added += db.upsert_operation(conn, op)
    return added


def import_alfa(conn: sqlite3.Connection, report: dict) -> None:
    result = alfa.load(resolve_regnumber=_regnumber_resolver(conn, report))
    if not result["operations"]:
        return
    # Входящий остаток зависит от самой ранней выписки — пересобираем его каждый раз.
    conn.execute("DELETE FROM operations WHERE source = ? AND kind = ?", (alfa.SOURCE, db.OPENING))
    # Купоны, для которых ISIN раньше не нашёлся, могли найтись теперь.
    conn.execute("DELETE FROM operations WHERE source = ? AND isin IS NULL AND kind NOT IN (?, ?, ?)",
                 (alfa.SOURCE, db.BONUS, db.DEPOSIT, db.WITHDRAWAL))
    report["added"]["alpha"] = _store(conn, result["operations"])
    report["unknown"] += result["unknown"]
    report["alfa_files"] = result["files"]
    report["alfa_period"] = [result["opening_date"], result["covered_until"]]
    for begin, end in result["gaps"]:
        report["notes"].append(f"Альфа: нет выписки за {_ru(begin)}–{_ru(end)}, сделки и выплаты этих дней не учтены")


def _ru(day: str) -> str:
    return dt.date.fromisoformat(day).strftime("%d.%m.%Y")


def import_tinvest(conn: sqlite3.Connection, report: dict) -> None:
    try:
        result = tinvest.load()
    except Exception as exc:  # нет токена, VPN, неожиданный ответ — Альфа и цены важнее
        report["silent"].add(f"T-Invest ({type(exc).__name__}: {exc})")
        return
    count = "SELECT count(*) FROM operations WHERE portfolio = ? AND source = ?"
    before = conn.execute(count, (tinvest.PORTFOLIO, tinvest.SOURCE)).fetchone()[0]
    if result["operations"]:
        # API отдаёт всю историю — заменяем целиком, чтобы правка разбора дошла до старых строк.
        conn.execute("DELETE FROM operations WHERE portfolio = ? AND source IN ('jessica', ?)",
                     (tinvest.PORTFOLIO, tinvest.SOURCE))
    _store(conn, result["operations"])
    report["added"]["tinkoff"] = conn.execute(count, (tinvest.PORTFOLIO, tinvest.SOURCE)).fetchone()[0] - before
    report["unknown"] += result["unknown"]


def resolve_securities(conn: sqlite3.Connection, report: dict) -> None:
    """Дописать в справочник SECID, рынок и эмитента для всех известных ISIN."""
    isins = {row["isin"] for row in conn.execute("SELECT DISTINCT isin FROM operations WHERE isin IS NOT NULL")}
    isins |= {p["isin"] for p in jessica_positions()}
    stale = (dt.date.today() - dt.timedelta(days=RESOLVE_RETRY_DAYS)).isoformat()
    for isin in sorted(isins):
        row = conn.execute("SELECT secid, name, updated FROM securities WHERE isin = ?", (isin,)).fetchone()
        if row and (row["secid"] or (row["updated"] or "") > stale):
            continue
        try:
            info = iss.resolve(isin) or {"isin": isin}
        except iss.Unavailable as exc:
            report["silent"].add(f"ISS ({exc})")
            return
        if row and row["name"]:
            info.pop("name", None)  # полное имя из выписки понятнее биржевого сокращения
        db.upsert_security(conn, {**info, "updated": dt.date.today().isoformat()})


def held_isins(conn: sqlite3.Connection) -> list[str]:
    from .model import build

    return sorted({p["isin"] for p in build(conn)["positions"] if p["quantity"] > 0})


def refresh_quotes(conn: sqlite3.Connection, report: dict) -> None:
    rows = [dict(r) for r in conn.execute("SELECT * FROM securities WHERE secid IS NOT NULL")]
    held = set(held_isins(conn)) | {p["isin"] for p in jessica_positions()}
    found = iss.quotes([r for r in rows if r["isin"] in held])
    today = dt.date.today().isoformat()
    for isin, quote in found.items():
        conn.execute(
            "INSERT INTO quotes (isin, price, accrued, date, source) VALUES (?, ?, ?, ?, 'iss') "
            "ON CONFLICT(isin) DO UPDATE SET price = excluded.price, accrued = excluded.accrued, "
            "date = excluded.date, source = excluded.source",
            (isin, quote["price"], quote["accrued"], quote["updated"] or today),
        )
        db.upsert_security(conn, {"isin": isin, "facevalue": quote["facevalue"],
                                  "currency": quote["currency"], "maturity": quote["maturity"]})
    report["quotes"] = f"{len(found)} из {len(held)}"
    if len(found) < len(held):
        report["no_quote"] = sorted(held - set(found))


def refresh_payments(conn: sqlite3.Connection, report: dict) -> None:
    """График выплат по облигациям, которые сейчас в портфеле."""
    held = set(held_isins(conn)) | {p["isin"] for p in jessica_positions() if p.get("kind") == "bond"}
    bonds = conn.execute(
        f"SELECT isin, secid FROM securities WHERE kind = 'bond' AND secid IS NOT NULL "
        f"AND isin IN ({','.join('?' * len(held))})", sorted(held),
    ).fetchall()
    done = 0
    for bond in bonds:
        try:
            events = iss.bondization(bond["secid"])
        except iss.Unavailable as exc:
            report["silent"].add(f"ISS ({exc})")
            break
        if not events:
            continue
        conn.execute("DELETE FROM payments WHERE isin = ?", (bond["isin"],))
        conn.executemany(
            "INSERT OR REPLACE INTO payments (isin, date, kind, per_unit, estimated, start) VALUES (?, ?, ?, ?, ?, ?)",
            [(bond["isin"], e["date"], e["kind"], e["per_unit"], e["estimated"], e.get("start")) for e in events],
        )
        done += 1
    report["schedules"] = f"{done} из {len(bonds)}"


def accrued_at(conn: sqlite3.Connection, isin: str, day: str) -> float | None:
    """НКД на штуку на дату: купон × доля прошедших дней купонного периода.

    Купон начисляется равномерно по дням от начала периода до выплаты. Начало
    периода ISS даёт сам (startdate), без него — дата предыдущего купона.
    """
    upcoming = conn.execute(
        "SELECT date, per_unit, start FROM payments WHERE isin = ? AND kind = 'coupon' AND date > ? "
        "ORDER BY date LIMIT 1", (isin, day)).fetchone()
    if not upcoming or upcoming["per_unit"] is None:
        return None
    begin = upcoming["start"]
    if not begin:
        previous = conn.execute(
            "SELECT date FROM payments WHERE isin = ? AND kind = 'coupon' AND date <= ? ORDER BY date DESC LIMIT 1",
            (isin, day)).fetchone()
        begin = previous["date"] if previous else None
    if not begin or begin > day:
        return None
    start, end, now = (dt.date.fromisoformat(d) for d in (begin, upcoming["date"], day))
    return upcoming["per_unit"] * (now - start).days / (end - start).days


def split_opening_accrued(conn: sqlite3.Connection, report: dict) -> None:
    """Стоимость входящего остатка Альфы включает НКД — выделяем его.

    Иначе НКД посчитался бы дважды: внутри себестоимости и в текущей стоимости.
    """
    missed = []
    for op in conn.execute(
        "SELECT o.id, o.isin, o.ext_id, o.quantity FROM operations o JOIN securities s ON s.isin = o.isin "
        "WHERE o.kind = ? AND o.source = ? AND s.kind = 'bond'", (db.OPENING, alfa.SOURCE),
    ).fetchall():
        valued_on = op["ext_id"].split(":")[1]  # opening:<дата оценки>:<ISIN>
        per_unit = accrued_at(conn, op["isin"], valued_on)
        if per_unit is None:
            missed.append(op["isin"])
            continue
        conn.execute("UPDATE operations SET accrued = ? WHERE id = ?", (round(per_unit * op["quantity"], 2), op["id"]))
    if missed:
        report["notes"].append("НКД во входящем остатке не выделен (нет графика купонов): " + ", ".join(missed))


def jessica_positions() -> list[dict]:
    """Состав из holdings.json Jessica — для сверки и как запасной для Т."""
    path = paths.jessica_state("holdings.json")
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [p for block in data.get("portfolios", {}).values() for p in block.get("positions", [])]


def fill_from_jessica(conn: sqlite3.Connection, report: dict) -> None:
    """Пока из API Т не загружено ни одной операции, состав Т берётся у Jessica.

    Цены покупки там нет, поэтому себестоимость = текущая стоимость, прибыль
    по таким позициям ноль. Как только придут настоящие операции, эти строки
    удаляются (см. import_tinvest).
    """
    if conn.execute("SELECT 1 FROM operations WHERE portfolio = ? AND source = ? LIMIT 1",
                    (tinvest.PORTFOLIO, tinvest.SOURCE)).fetchone():
        return
    conn.execute("DELETE FROM operations WHERE source = 'jessica'")
    today = dt.date.today().isoformat()
    added = 0
    for position in jessica_positions():
        if position.get("portfolio") != tinvest.PORTFOLIO:
            continue
        quote = conn.execute("SELECT price, accrued FROM quotes WHERE isin = ?", (position["isin"],)).fetchone()
        value = position["quantity"] * quote["price"] if quote else 0.0
        accrued = position["quantity"] * quote["accrued"] if quote else 0.0
        db.upsert_security(conn, {"isin": position["isin"], "name": position.get("name"),
                                  "kind": position.get("kind"), "issuer": position.get("issuer")})
        added += db.upsert_operation(conn, {
            "portfolio": tinvest.PORTFOLIO, "source": "jessica", "ext_id": f"holding:{position['isin']}",
            "date": today, "kind": db.OPENING, "isin": position["isin"], "quantity": position["quantity"],
            # Как будто куплено сегодня по текущей цене с текущим НКД: прибыль ровно ноль.
            "amount": -(value + accrued), "accrued": accrued, "note": "состав из Jessica: сделок из T-Invest ещё нет, себестоимость = текущая стоимость",
        })
    if added:
        report["notes"].append(f"Т: {added} позиций взяты из holdings.json Jessica без истории сделок")


def run(conn: sqlite3.Connection | None = None, *, tinvest_api: bool = True) -> dict:
    report = {"started": dt.datetime.now().isoformat(timespec="seconds"), "added": {}, "unknown": [],
              "silent": set(), "notes": []}
    own = conn is None
    conn = conn or db.connect()
    try:
        with conn:
            import_alfa(conn, report)
        if tinvest_api:
            with conn:
                import_tinvest(conn, report)
        with conn:
            resolve_securities(conn, report)
        with conn:
            refresh_quotes(conn, report)
        with conn:
            fill_from_jessica(conn, report)
        with conn:
            refresh_payments(conn, report)
        with conn:
            split_opening_accrued(conn, report)
        report["silent"] = sorted(report["silent"])
        report["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        with conn:
            db.set_meta(conn, "last_sync", json.dumps(report, ensure_ascii=False))
    finally:
        if own:
            conn.close()
    return report
