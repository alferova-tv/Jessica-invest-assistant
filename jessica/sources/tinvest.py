"""Облигации брокерского счёта Т-Инвестиций из T-Invest API.

Токен — только на чтение: Jessica смотрит состав портфеля и больше ничего.
Отсюда уходят простые словари; что с ними делать, решает holdings.py.

Две особенности сети определяют весь модуль.

Сертификат. `invest-public-api.tbank.ru` подписан цепочкой Минцифры (Russian
Trusted Root CA), которой нет в стандартных хранилищах. Корень лежит в
`jessica/certs/` и передаётся в `verify=` только здесь: системе и остальной
Jessica доверять государственному CA незачем.

VPN. API режется по IP выходного узла так же, как ISS Мосбиржи, и узел
меняется сам по себе. Поэтому разовый обрыв повторяется, а когда API не отвечает
совсем, модуль возвращает None, а не половину портфеля: половина состава хуже
вчерашнего полного. Сведения о бумагах (ISIN, эмитент) не меняются, их можно
не запрашивать повторно — известные передаются через `known`, и обычный сбор
обходится двумя запросами вместо двух десятков.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
CA_BUNDLE = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_root_ca.pem"

BROKER_ACCOUNT = "ACCOUNT_TYPE_TINKOFF"  # ИИС и копилки в Jessica не попадают
OPEN = "ACCOUNT_STATUS_OPEN"

# Сведения о бумаге, которые не меняются и хранятся в кэше у holdings.py.
FIELDS = ("isin", "name", "issuer", "code")

# У секьюритизации borrowName — банк, выдавший кредиты («АО "ТБанк"»), а сама
# бумага выпущена спецфинобществом, и оно стоит в brand.company («СФО ТБ-4»).
# Эмитентом считаем СФО: ежемесячная отчётность банка к бумаге не относится.
SPV = re.compile(r"\bСФО\b")

TIMEOUT = 10
ATTEMPTS = 4
PAUSE = 10


class Unavailable(Exception):
    """API не дал ответа, с которым можно работать. Текст — без токена."""


def _post(method: str, payload: dict, headers: dict) -> dict:
    """Ответ метода API. Обрыв связи и 5xx повторяются, 4xx — нет: протухший
    токен или чужой счёт повтором не лечатся."""
    problem = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(PAUSE)
        try:
            response = requests.post(
                API + method, json=payload, headers=headers, timeout=TIMEOUT, verify=str(CA_BUNDLE)
            )
        except requests.RequestException as exc:
            problem = type(exc).__name__
            continue
        if response.status_code >= 500:
            problem = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise Unavailable(f"{method}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise Unavailable(f"{method}: ответ не JSON") from None
    raise Unavailable(f"{method}: {problem} после {ATTEMPTS} попыток")


def _account(headers: dict, account: str | None) -> str:
    if account:
        return account
    accounts = [
        item
        for item in _post("UsersService/GetAccounts", {}, headers).get("accounts", [])
        if item.get("type") == BROKER_ACCOUNT and item.get("status") == OPEN
    ]
    if len(accounts) != 1:
        # Гадать, чей счёт, нельзя: номер задаётся в config.yaml, tinvest.account.
        raise Unavailable(f"открытых брокерских счетов {len(accounts)}, нужен ровно один")
    return accounts[0]["id"]


def _describe(uid: str, headers: dict) -> dict:
    """ISIN, название выпуска, эмитент и биржевой код бумаги."""
    bond = _post(
        "InstrumentsService/BondBy", {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}, headers
    )["instrument"]
    asset = _post("InstrumentsService/GetAssetBy", {"id": bond["assetUid"]}, headers)["asset"]
    # Эмитент ищет новости: без него у выпуска вроде «АЛИУМ01Р1» маркеров нет вовсе.
    borrower = asset.get("security", {}).get("bond", {}).get("borrowName") or ""
    brand = asset.get("brand", {}).get("company") or ""
    issuer = brand if SPV.search(brand) else borrower or brand
    ticker = bond.get("ticker", "")
    return {
        "isin": bond["isin"],
        "name": bond.get("name", ""),
        "issuer": issuer.strip(),
        # ОФЗ smart-lab ищет по биржевому коду (SU26253RMFS3), а не по ISIN.
        "code": "" if ticker == bond["isin"] else ticker,
    }


def _quantity(value: dict) -> float:
    return int(value.get("units", 0)) + int(value.get("nano", 0)) / 1e9


def fetch_bonds(
    token: str, *, account: str | None = None, known: dict[str, dict] | None = None
) -> list[dict] | None:
    """Облигации брокерского счёта или None, если API не дал полного ответа."""
    if not token:
        log.warning("T-Invest: токен не задан (tinvest.token в config.yaml)")
        return None
    headers = {"Authorization": f"Bearer {token}"}
    known = known or {}
    try:
        portfolio = _post(
            "OperationsService/GetPortfolio", {"accountId": _account(headers, account)}, headers
        )
        bonds = []
        for position in portfolio.get("positions", []):
            if position.get("instrumentType") != "bond":
                continue
            quantity = _quantity(position["quantity"])
            if quantity <= 0:
                continue
            uid = position["instrumentUid"]
            info = known.get(uid) or {}
            if not all(field in info for field in FIELDS):
                info = _describe(uid, headers)
            bonds.append({"uid": uid, **{field: info[field] for field in FIELDS}, "quantity": quantity})
        return bonds
    except Unavailable as exc:
        log.warning("T-Invest API не ответил: %s", exc)
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("T-Invest API: неожиданный ответ (%s: %s)", type(exc).__name__, exc)
    return None
