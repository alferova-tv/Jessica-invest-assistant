"""Операции брокерского счёта Т-Инвестиций из T-Invest API.

Токен, сертификат Минцифры, повторы при обрывах VPN — всё это уже решено
в Jessica (`jessica/sources/tinvest.py`), здесь используется её клиент.
Токен читает только код при запуске, из config.yaml Jessica.

История берётся целиком, `GetOperationsByCursor` с начала счёта: операций
у частного счёта сотни, это несколько запросов. Сделки и комиссии приходят
отдельными операциями (комиссия — `BROKER_FEE` с `parentOperationId`),
поэтому комиссия сделки берётся только оттуда, а не из поля `commission`,
иначе посчиталась бы дважды.

Модуль писался по документации API без живого прогона — первый запуск
`portfel sync` покажет нераспознанные типы операций в отчёте.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys

from .. import db, paths

log = logging.getLogger(__name__)

PORTFOLIO = "tinkoff"
SOURCE = "tinvest"
PAGE = 1000

T = "OPERATION_TYPE_"
KINDS = {
    T + "BUY": db.BUY, T + "BUY_CARD": db.BUY, T + "BUY_MARGIN": db.BUY, T + "DELIVERY_BUY": db.BUY,
    T + "SELL": db.SELL, T + "SELL_CARD": db.SELL, T + "SELL_MARGIN": db.SELL, T + "DELIVERY_SELL": db.SELL,
    T + "COUPON": db.COUPON,
    T + "DIVIDEND": db.DIVIDEND, T + "DIVIDEND_TRANSFER": db.DIVIDEND,
    T + "BOND_REPAYMENT": db.AMORTIZATION,
    T + "BOND_REPAYMENT_FULL": db.REDEMPTION,
    T + "BROKER_FEE": db.FEE, T + "SERVICE_FEE": db.FEE, T + "SUCCESS_FEE": db.FEE,
    T + "MARGIN_FEE": db.FEE, T + "TRACK_MFEE": db.FEE, T + "TRACK_PFEE": db.FEE, T + "CASH_FEE": db.FEE,
    T + "OUT_FEE": db.FEE, T + "OUT_STAMP_DUTY": db.FEE, T + "ADVICE_FEE": db.FEE, T + "OTHER_FEE": db.FEE,
    T + "TAX": db.TAX, T + "BOND_TAX": db.TAX, T + "DIVIDEND_TAX": db.TAX, T + "BENEFIT_TAX": db.TAX,
    T + "TAX_PROGRESSIVE": db.TAX, T + "BOND_TAX_PROGRESSIVE": db.TAX, T + "DIVIDEND_TAX_PROGRESSIVE": db.TAX,
    T + "TAX_CORRECTION": db.TAX, T + "TAX_CORRECTION_COUPON": db.TAX, T + "TAX_CORRECTION_PROGRESSIVE": db.TAX,
    T + "TAX_REPO": db.TAX, T + "TAX_REPO_PROGRESSIVE": db.TAX, T + "TAX_REPO_HOLD": db.TAX,
    T + "TAX_REPO_REFUND": db.TAX, T + "TAX_REPO_HOLD_PROGRESSIVE": db.TAX, T + "TAX_REPO_REFUND_PROGRESSIVE": db.TAX,
    T + "TAX_CORRECTION_DIVIDEND": db.TAX,
    T + "INPUT": db.DEPOSIT, T + "INPUT_SECURITIES": db.DEPOSIT, T + "INPUT_SWIFT": db.DEPOSIT,
    T + "INPUT_ACQUIRING": db.DEPOSIT, T + "INP_MULTI": db.DEPOSIT,
    T + "OUTPUT": db.WITHDRAWAL, T + "OUTPUT_SECURITIES": db.WITHDRAWAL, T + "OUTPUT_SWIFT": db.WITHDRAWAL,
    T + "OUT_MULTI": db.WITHDRAWAL, T + "OUTPUT_ACQUIRING": db.WITHDRAWAL,
    T + "BENEFIT": db.BONUS, T + "OVERNIGHT": db.BONUS,
}


def _client():
    if str(paths.JESSICA_CODE) not in sys.path:
        sys.path.insert(0, str(paths.JESSICA_CODE))
    from jessica.config import Config
    from jessica.sources import tinvest

    config = Config.load()
    headers = {"Authorization": f"Bearer {config.tinvest_token}"}
    return tinvest, headers, config.tinvest_account


def _money(value: dict | None) -> float:
    if not value:
        return 0.0
    return int(value.get("units", 0) or 0) + int(value.get("nano", 0) or 0) / 1e9


def _instrument(tinvest, headers, uid: str, cache: dict) -> dict | None:
    """ISIN и название по uid. Кэш Jessica покрывает текущие бумаги, проданные спрашиваем."""
    if uid in cache:
        return cache[uid]
    try:
        found = tinvest._post("InstrumentsService/GetInstrumentBy",
                              {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}, headers)["instrument"]
    except (tinvest.Unavailable, KeyError) as exc:
        log.warning("T-Invest: бумага %s не найдена (%s)", uid, exc)
        cache[uid] = None
        return None
    kind = {"bond": "bond", "share": "share", "etf": "fund"}.get(found.get("instrumentType"), "other")
    cache[uid] = {"isin": found.get("isin"), "name": found.get("name"), "kind": kind}
    return cache[uid]


def convert(item: dict, instrument: dict | None) -> tuple[dict | None, str | None]:
    """Операция API → строка таблицы operations. Второе — причина, если пропущена."""
    kind = KINDS.get(item.get("type", ""))
    if kind is None:
        return None, f"{item.get('date', '')[:10]} {item.get('type')}: {item.get('name') or item.get('description') or ''}"
    payment = _money(item.get("payment"))
    quantity = float(item.get("quantityDone") or item.get("quantity") or 0)
    accrued = abs(_money(item.get("accruedInt")))
    amount = payment
    # payment уже включает НКД: покупка ТрансКонтейнера П02-05 19.10.2026 в приложении
    # −997,79 ₽ при НКД 0,89 ₽, и остаток денег сходится с брокером только так.
    op = {
        "portfolio": PORTFOLIO, "source": SOURCE, "ext_id": item["id"],
        "date": item["date"][:10], "kind": kind,
        "isin": (instrument or {}).get("isin"),
        "quantity": quantity if kind in (db.BUY, db.SELL) else 0,
        "amount": amount, "accrued": accrued if kind in (db.BUY, db.SELL) else 0, "fee": 0,
        "note": item.get("name") or item.get("description") or "",
    }
    if kind == db.FEE:
        op["amount"], op["fee"] = 0.0, abs(payment)
    return op, None


def load(since: str | None = None) -> dict:
    """Операции счёта с даты `since` (по умолчанию — с открытия)."""
    tinvest, headers, account_id = _client()
    account_id = tinvest._account(headers, account_id)
    cache_path = paths.jessica_state("tinvest_instruments.json")
    cache: dict = {}
    if cache_path.exists():
        import json

        cache = {uid: {"isin": v["isin"], "name": v["name"], "kind": v["kind"]}
                 for uid, v in json.loads(cache_path.read_text(encoding="utf-8")).items()}

    payload = {
        "accountId": account_id,
        "from": (since or "2015-01-01") + "T00:00:00Z",
        "to": (dt.date.today() + dt.timedelta(days=1)).isoformat() + "T00:00:00Z",
        "limit": PAGE,
        "state": "OPERATION_STATE_EXECUTED",
        "withoutCommissions": False,
        "withoutTrades": True,
        "withoutOvernights": False,
    }
    operations, skipped, cursor = [], [], ""
    while True:
        response = tinvest._post("OperationsService/GetOperationsByCursor", {**payload, "cursor": cursor}, headers)
        for item in response.get("items", []):
            uid = item.get("instrumentUid") or ""
            instrument = _instrument(tinvest, headers, uid, cache) if uid else None
            op, reason = convert(item, instrument)
            if op:
                operations.append(op)
                if instrument and instrument.get("isin"):
                    op["_name"], op["_kind"] = instrument.get("name"), instrument.get("kind")
            elif reason:
                skipped.append(reason)
        cursor = response.get("nextCursor") or ""
        if not response.get("hasNext") or not cursor:
            break
    return {"operations": operations, "unknown": sorted(set(skipped))}
