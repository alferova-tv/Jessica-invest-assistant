"""Мосбиржа (ISS): справочник бумаг, котировки, график купонов и амортизаций.

ISS режется по IP выходного узла VPN, и непредсказуемо (см. CLAUDE.md Jessica).
Поэтому каждый запрос повторяется, а отказ не валит синхронизацию: старые цены
и график остаются в базе, интерфейс показывает их дату.

ISS ищет бумаги по SECID, а он совпадает с ISIN не всегда: у ОФЗ это
`SU26253RMFS3`. Поэтому сначала `resolve` находит SECID, рынок и режим торгов
через поиск, а уже потом идут котировки и график.
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

BASE = "https://iss.moex.com/iss"
TIMEOUT = 10
ATTEMPTS = 3
PAUSE = 3
BATCH = 10  # бумаг в одном запросе котировок

# Группы ISS → вид бумаги в портфеле и рынок, где искать котировку.
GROUPS = {
    "stock_bonds": ("bond", "bonds"),
    "stock_shares": ("share", "shares"),
    "stock_dr": ("share", "shares"),
    "stock_ppif": ("fund", "shares"),
    "stock_etf": ("fund", "shares"),
}


class Unavailable(Exception):
    pass


def _get(path: str, params: dict | None = None) -> dict:
    params = {"iss.meta": "off", **(params or {})}
    problem = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(PAUSE)
        try:
            response = requests.get(f"{BASE}/{path}.json", params=params, timeout=TIMEOUT)
            if response.status_code >= 500:
                problem = f"HTTP {response.status_code}"
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            problem = type(exc).__name__
    raise Unavailable(f"{path}: {problem}")


def _table(payload: dict, name: str) -> list[dict]:
    block = payload.get(name) or {}
    columns = block.get("columns", [])
    return [dict(zip(columns, row)) for row in block.get("data", [])]


def search(query: str) -> list[dict]:
    return _table(_get("securities", {"q": query}), "securities")


def resolve(isin: str) -> dict | None:
    """SECID, рынок и сведения о бумаге по ISIN."""
    rows = [row for row in search(isin) if row.get("isin") == isin]
    if not rows:
        return None
    row = max(rows, key=lambda r: r.get("is_traded") or 0)
    kind, market = GROUPS.get(row.get("group") or "", ("other", ""))
    return {
        "isin": isin,
        "secid": row["secid"],
        "name": row.get("shortname") or row.get("name"),
        "kind": kind,
        "issuer": row.get("emitent_title") or "",
        "regnumber": row.get("regnumber") or "",
        "market": market,
        "board": row.get("primary_boardid") or "",
    }


def isin_by_regnumber(regnumber: str) -> dict | None:
    """В выписке Альфы купон назван регномером выпуска, а не ISIN."""
    rows = [row for row in search(regnumber) if (row.get("regnumber") or "").upper() == regnumber.upper()]
    if not rows:
        return None
    return resolve(rows[0]["isin"]) if rows[0].get("isin") else None


def quotes(securities: list[dict]) -> dict[str, dict]:
    """Последние цены по ISIN: чистая цена и НКД за штуку в рублях.

    Цена облигации в ISS — процент от текущего номинала, пересчитываем в рубли.
    Берётся последняя сделка, без неё — текущая или вчерашняя цена.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for security in securities:
        if security.get("secid") and security.get("market") and security.get("board"):
            groups.setdefault((security["market"], security["board"]), []).append(security)

    result: dict[str, dict] = {}
    for (market, board), items in groups.items():
        for start in range(0, len(items), BATCH):
            chunk = items[start:start + BATCH]
            by_secid = {item["secid"]: item for item in chunk}
            try:
                payload = _get(
                    f"engines/stock/markets/{market}/boards/{board}/securities",
                    {"securities": ",".join(by_secid), "iss.only": "securities,marketdata"},
                )
            except Unavailable as exc:
                log.warning("ISS: котировки не получены (%s)", exc)
                continue
            static = {row["SECID"]: row for row in _table(payload, "securities")}
            for row in _table(payload, "marketdata"):
                secid = row["SECID"]
                info = static.get(secid, {})
                price = row.get("LAST") or row.get("LCURRENTPRICE") or row.get("MARKETPRICE") or info.get("PREVPRICE")
                if price is None or secid not in by_secid:
                    continue
                face = info.get("FACEVALUE")
                if market == "bonds":
                    if not face:
                        continue
                    price = price * face / 100
                result[by_secid[secid]["isin"]] = {
                    "price": float(price),
                    "accrued": float(info.get("ACCRUEDINT") or 0),
                    "facevalue": face,
                    "currency": info.get("FACEUNIT") or info.get("CURRENCYID") or "",
                    "maturity": info.get("MATDATE") or None,
                    "updated": (row.get("SYSTIME") or row.get("UPDATETIME") or "")[:10] or None,
                }
    return result


def bondization(secid: str) -> list[dict]:
    """Купоны, амортизации и погашение одного выпуска на штуку.

    Сумма будущего купона по флоатеру не объявлена (value = null) — её заменяем
    последней известной и помечаем как оценку. Погашение — последняя
    амортизация, ISS отдаёт его в той же таблице.
    """
    events: list[dict] = []
    start = 0
    coupons: list[dict] = []
    amortizations: list[dict] = []
    while True:
        payload = _get(f"statistics/engines/stock/markets/bonds/bondization/{secid}",
                       {"start": start, "limit": 100})
        page_coupons = _table(payload, "coupons")
        page_amort = _table(payload, "amortizations")
        coupons += page_coupons
        amortizations += page_amort
        if len(page_coupons) < 100 and len(page_amort) < 100:
            break
        start += 100

    last_value = None
    for row in sorted(coupons, key=lambda r: r["coupondate"]):
        value = row.get("value_rub") if row.get("value_rub") is not None else row.get("value")
        estimated = value is None
        if estimated:
            value = last_value
        else:
            last_value = value
        events.append({"date": row["coupondate"], "kind": "coupon", "per_unit": value,
                       "estimated": int(estimated), "start": row.get("startdate")})

    amortizations.sort(key=lambda r: r["amortdate"])
    for index, row in enumerate(amortizations):
        value = row.get("value_rub") if row.get("value_rub") is not None else row.get("value")
        kind = "redemption" if index == len(amortizations) - 1 else "amortization"
        events.append({"date": row["amortdate"], "kind": kind, "per_unit": value, "estimated": int(value is None)})
    return events
