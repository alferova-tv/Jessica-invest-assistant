"""Операции Альфы из брокерских выписок (xlsx или zip с xlsx).

Из выписки берутся три листа:

* «Завершенные сделки» — покупки и продажи. Количество со знаком: минус —
  бумага ушла со счёта. «Сумма сделки» включает НКД, он дан отдельной колонкой.
  «Незавершенные сделки» (расчёт после конца периода) читаются так же: номер
  тот же, и в следующей выписке завершённая сделка заменит незавершённую.
* «Движение ДС» — купоны, амортизации, дивиденды, бонусы. Строки «Расчеты по
  сделке», «НКД по сделке», «Комиссия по сделке» пропускаются: это та же сделка,
  уже взятая с листа сделок.
* «Динамика позиций» — только у самой ранней выписки, как входящий остаток:
  бумаги, купленные до её начала, иначе остались бы без покупки.

Выписки перекрываются (01.09–10.09 и 01.09–25.09), поэтому сделка узнаётся
по номеру, а денежная операция — по времени, тексту и сумме.

Купон в выписке назван регномером выпуска («4B02-01-55234-E-002P»), ISIN
находит `resolve_regnumber`, которую передаёт вызывающий код.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Callable

from .. import db, paths

log = logging.getLogger(__name__)

PORTFOLIO = "alpha"
SOURCE = "alfa"
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
PERIOD = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})")
REGNUMBER = re.compile(r"(?:погашение купона|погашение номинала|погашение облигаций)\s+(\S+)", re.I)
DIVIDEND_REG = re.compile(r"\(([0-9A-Z-]+-[A-Z])\)")
WITHHELD = re.compile(r"Удержан налог\s+([\d\s.,]+)\s*руб", re.I)
SKIP = ("расчеты по сделке", "нкд по сделке", "комиссия по сделке")


def _rows_from_xlsx(path: Path):
    """Чтение xlsx у Jessica уже отлажено на этих выписках — берём его."""
    if str(paths.JESSICA_CODE) not in sys.path:
        sys.path.insert(0, str(paths.JESSICA_CODE))
    from jessica.holdings import _rows_from_xlsx as read

    return read(path)


def _unpack(path: Path) -> list[Path]:
    """Распаковать архив в свою папку. В папку выписок не пишем ничего:
    Jessica берёт оттуда самый свежий по времени файл, и распакованный заново
    старый архив подменил бы ей настоящую последнюю выписку."""
    if str(paths.JESSICA_CODE) not in sys.path:
        sys.path.insert(0, str(paths.JESSICA_CODE))
    from jessica.holdings import unpack_zip

    target = paths.data_dir() / "alfa-unpacked" / path.stem
    target.mkdir(parents=True, exist_ok=True)
    copy = target / path.name
    if not copy.exists():
        shutil.copy2(path, copy)
        unpack_zip(copy)
    return [p for p in target.iterdir() if p.suffix.lower() in (".xlsx", ".xlsm")]


def _number(value: str) -> float | None:
    cleaned = (value or "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _header(rows: list[list[str]], *required: str) -> tuple[int, dict[str, int]] | None:
    for index, row in enumerate(rows):
        cells = {_norm(cell): pos for pos, cell in enumerate(row) if cell.strip()}
        if all(any(req in header for header in cells) for req in required):
            return index, cells
    return None


def _col(headers: dict[str, int], name: str) -> int | None:
    for header, pos in headers.items():
        if name in header:
            return pos
    return None


def _cell(row: list[str], pos: int | None) -> str:
    return row[pos] if pos is not None and pos < len(row) else ""


def _date(value: str) -> str | None:
    value = value.strip()
    for pattern, fmt in ((r"\d{2}\.\d{2}\.\d{4}", "%d.%m.%Y"), (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d")):
        match = re.search(pattern, value)
        if match:
            return dt.datetime.strptime(match.group(0), fmt).date().isoformat()
    return None


def _sheet(sheets, title: str):
    for name, rows in sheets:
        if name.strip().lower() == title.lower():
            return rows
    return None


def period(sheets) -> tuple[str, str] | None:
    for _, rows in sheets[:1]:
        for row in rows[:12]:
            match = PERIOD.search(" ".join(row))
            if match:
                return tuple(_date(part) for part in match.groups())
    return None


def parse_trades(rows: list[list[str]]) -> list[dict]:
    found = _header(rows, "№ сделки", "isin", "количество", "сумма сделки")
    if not found:
        return []
    start, headers = found
    c_id, c_date = _col(headers, "№ сделки"), _col(headers, "дата заключен")
    c_isin, c_name = _col(headers, "isin"), _col(headers, "актив")
    c_qty, c_sum = _col(headers, "количество"), _col(headers, "сумма сделки")
    c_aci, c_fee = _col(headers, "нкд"), _col(headers, "комиссия")
    c_cur = _col(headers, "валюта расчетов")
    ops = []
    for row in rows[start + 1:]:
        isin = _cell(row, c_isin).strip()
        quantity = _number(_cell(row, c_qty))
        total = _number(_cell(row, c_sum))
        if not ISIN.match(isin) or quantity is None or total is None:
            continue
        deal = _cell(row, c_id).split("\n")[0].strip()
        accrued = _number(_cell(row, c_aci)) or 0.0
        fee = _number(_cell(row, c_fee)) or 0.0
        buy = quantity > 0
        currency = _cell(row, c_cur).strip()
        ops.append({
            "portfolio": PORTFOLIO, "source": SOURCE, "ext_id": f"deal:{deal}",
            "date": _date(_cell(row, c_date)), "kind": db.BUY if buy else db.SELL,
            "isin": isin, "quantity": abs(quantity),
            "amount": -total if buy else total, "accrued": accrued, "fee": fee,
            "note": "" if currency in ("", "RUB") else f"валюта {currency}",
            "_name": _cell(row, c_name).strip(),
        })
    return ops


def classify_cash(comment: str) -> str | None:
    text = comment.lower()
    if "частичное погашение номинала" in text:
        return db.AMORTIZATION
    if "погашение номинала" in text or "погашение облигаций" in text:
        return db.REDEMPTION
    if "погашение купона" in text or "купон" in text:
        return db.COUPON
    if "дивиденд" in text:
        return db.DIVIDEND
    if "акция в подарок" in text or "кэшбэк" in text:
        return db.BONUS
    if "налог" in text:
        return db.TAX
    return None


def parse_cash(rows: list[list[str]], resolve_regnumber: Callable[[str], str | None]) -> tuple[list[dict], list[str]]:
    """Денежные операции, кроме расчётов по сделкам. Второе — нераспознанные строки."""
    found = _header(rows, "наименование операции", "комментарий")
    if not found:
        return [], []
    start, headers = found
    c_op, c_comment = _col(headers, "наименование операции"), _col(headers, "комментарий")
    # Сумма стоит в колонке под подписью валюты (RUB) на строку ниже заголовка.
    sub = {(_norm(cell)): pos for pos, cell in enumerate(rows[start + 1]) if cell.strip()} if start + 1 < len(rows) else {}
    c_amount = sub.get("rub")
    ops, unknown = [], []
    for row in rows[start + 2:]:
        operation, comment = _cell(row, c_op).strip(), _cell(row, c_comment).strip()
        if _norm(comment).startswith("итого") or _norm(operation).startswith("итого"):
            break
        if not operation or any(_norm(operation).startswith(skip) for skip in SKIP):
            continue
        amount = _number(_cell(row, c_amount))
        # В первой строке дня стоят две даты: день (00:00:00) и время операции — берём последнюю.
        stamp = [cell for cell in row if re.match(r"\d{4}-\d{2}-\d{2} \d", cell)]
        when = stamp[-1] if stamp else ""
        if amount is None or not when:
            continue
        kind = classify_cash(comment)
        if kind is None and _norm(operation).startswith("перевод"):
            # Пополнение: «из АО "Альфа-Банк"» с плюсом; вывод — «в …» с минусом.
            if amount > 0 and _norm(comment).startswith("из "):
                kind = db.DEPOSIT
            elif amount < 0 and _norm(comment).startswith(("в ", "на ")):
                kind = db.WITHDRAWAL
        if kind is None:
            # Остальное не гадаем, а показываем.
            unknown.append(f"{when[:10]} {operation}: {comment} ({amount:+.2f})")
            continue
        isin = None
        tax = 0.0
        if kind in (db.COUPON, db.AMORTIZATION, db.REDEMPTION):
            match = REGNUMBER.search(comment)
            isin = resolve_regnumber(match.group(1)) if match else None
        elif kind == db.DIVIDEND:
            match = DIVIDEND_REG.search(comment)
            isin = resolve_regnumber(match.group(1)) if match else None
            withheld = WITHHELD.search(comment)
            tax = _number(withheld.group(1)) if withheld else 0.0
        base = {"portfolio": PORTFOLIO, "source": SOURCE, "date": when[:10], "isin": isin, "note": comment}
        key = f"cash:{when}|{comment}|{amount:.2f}"
        if kind == db.DIVIDEND and tax:
            # В выписке дивиденд уже за вычетом налога; храним брутто и налог отдельно.
            ops.append({**base, "ext_id": key, "kind": kind, "amount": amount + tax})
            ops.append({**base, "ext_id": key + "|tax", "kind": db.TAX, "amount": -tax, "note": "НДФЛ с дивиденда"})
        else:
            ops.append({**base, "ext_id": key, "kind": kind, "amount": amount})
        if isin is None and kind not in (db.BONUS, db.DEPOSIT, db.WITHDRAWAL):
            unknown.append(f"{when[:10]} не найден выпуск: {comment}")
    return ops, unknown


def parse_opening(rows: list[list[str]], date: str, valued_on: str) -> list[dict]:
    """Позиции на начало выписки: ISIN — строкой ниже названия.

    `date` — когда остаток ставится в журнал, `valued_on` — на какой день брокер
    его оценил (начало выписки); по второму считается НКД внутри оценки.
    """
    found = _header(rows, "инструмент", "актив", "кол-во")
    if not found:
        return []
    start, headers = found
    c_name = _col(headers, "актив")
    # Заголовки «на начало» и «на конец» одинаковые — в словаре они склеиваются,
    # поэтому колонки берём по порядку из самой строки: первая — начало периода.
    header_row = [_norm(cell) for cell in rows[start]]
    qty_cols = [pos for pos, cell in enumerate(header_row) if "кол-во" in cell]
    value_cols = [pos for pos, cell in enumerate(header_row) if "стоимость" in cell]
    if not qty_cols or not value_cols:
        return []
    c_qty, c_value = qty_cols[0], value_cols[0]
    ops = []
    for index in range(start + 1, len(rows) - 1):
        row, below = rows[index], rows[index + 1]
        isin = next((cell.strip() for cell in below if ISIN.match(cell.strip())), None)
        quantity, value = _number(_cell(row, c_qty)), _number(_cell(row, c_value))
        if not isin or not quantity or quantity <= 0 or value is None:
            continue
        ops.append({
            "portfolio": PORTFOLIO, "source": SOURCE, "ext_id": f"opening:{valued_on}:{isin}",
            "date": date, "kind": db.OPENING, "isin": isin, "quantity": quantity,
            "amount": -value, "accrued": 0.0, "fee": 0.0,
            "note": "входящий остаток: цена покупки неизвестна, взята стоимость на начало выписки",
            "_name": _cell(row, c_name).strip(),
        })
    return ops


def reports(folder: Path | None = None) -> list[Path]:
    folder = folder or paths.alfa_reports()
    if not folder.exists():
        return []
    unpacked = [p for archive in folder.glob("*.zip") for p in _unpack(archive)]
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in (".xlsx", ".xlsm") and not p.name.startswith("~$")) + unpacked


def gaps(spans: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Дни, не покрытые ни одной выпиской, между первой и последней."""
    holes = []
    reached = None
    for begin, end in sorted(spans):
        if reached and begin > reached:
            after = (dt.date.fromisoformat(reached) + dt.timedelta(days=1)).isoformat()
            before = (dt.date.fromisoformat(begin) - dt.timedelta(days=1)).isoformat()
            if after <= before:
                holes.append((after, before))
        reached = max(reached or end, end)
    return holes


def load(folder: Path | None = None, resolve_regnumber: Callable[[str], str | None] = lambda reg: None) -> dict:
    """Все операции из всех выписок папки. Входящий остаток — от самой ранней."""
    trades: dict[str, dict] = {}
    cash: dict[str, dict] = {}
    unknown: list[str] = []
    earliest: tuple[str, list[dict]] | None = None
    covered_until = None
    spans: list[tuple[str, str]] = []
    files = reports(folder)
    for path in files:
        try:
            sheets = _rows_from_xlsx(path)
        except Exception as exc:  # битый файл не должен останавливать остальные
            log.warning("Альфа: не прочитан %s (%s)", path.name, exc)
            continue
        span = period(sheets)
        deals = parse_trades(_sheet(sheets, "Завершенные сделки") or [])
        pending = parse_trades(_sheet(sheets, "Незавершенные сделки") or [])
        money, problems = parse_cash(_sheet(sheets, "Движение ДС") or [], resolve_regnumber)
        if not deals and not money and not pending:
            continue  # пустая выписка (период до сегодняшнего дня) — ничего не значит
        for op in deals:
            trades[op["ext_id"]] = op
        for op in pending:
            trades.setdefault(op["ext_id"], op)
        for op in money:
            cash[op["ext_id"]] = op
        unknown += problems
        if span and span[0] and span[1]:
            spans.append((span[0], span[1]))
        if span and span[1] and (covered_until is None or span[1] > covered_until):
            covered_until = span[1]
        if span and span[0] and (earliest is None or span[0] < earliest[0]):
            # Сделки, заключённые до начала выписки (29.08) с расчётами в ней (01.09),
            # в остаток на начало не входят. Остаток датируется не позже первой из них,
            # иначе продажа пришлась бы на пустую позицию.
            first_trade = min((op["date"] for op in deals if op["date"]), default=span[0])
            earliest = (span[0], parse_opening(_sheet(sheets, "Динамика позиций") or [],
                                               min(span[0], first_trade), span[0]))
    opening = earliest[1] if earliest else []
    return {
        "files": [p.name for p in files],
        "operations": list(trades.values()) + list(cash.values()) + opening,
        "opening_date": earliest[0] if earliest else None,
        "covered_until": covered_until,
        "gaps": gaps(spans),
        "unknown": sorted(set(unknown)),
    }
