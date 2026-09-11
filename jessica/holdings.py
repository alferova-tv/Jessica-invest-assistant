"""Разбор брокерских выгрузок в единый список позиций.

Состав портфелей приходит только отсюда: API брокеров недоступны, а вручную
тикеры не заносятся. Вы кладёте отчёт в папку портфеля, Jessica берёт самый
свежий файл и вытаскивает из него ISIN, количество и тип бумаги.

Формат отчёта у брокеров разный настолько, что общей эвристикой не обойтись:

* Т-Банк — обычная таблица позиций, плюс отдельный справочник бумаг,
  где указан тип («акции обыкн», «обл прочих рез») и эмитент.
* Альфа — количество и название в одной строке, а ISIN отдельной строкой
  под ней; тип бумаги задан разделом таблицы («Акции», «Облигации»).

Поэтому здесь два точных парсера и общий запасной на случай незнакомого файла.
Колонки ищутся по тексту заголовка, а не по номеру: так разбор переживёт
смещение столбцов при очередной правке формата.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import re
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .config import Config, Portfolio
from .sources import tinvest

log = logging.getLogger(__name__)

# ISIN: две буквы страны, девять знаков, контрольная цифра.
ISIN = re.compile(r"\b(?:[A-Z]{2}[A-Z0-9]{9}\d|SU\d{5}RMFS\d)\b")

BOND, SHARE, FUND, UNKNOWN = "bond", "share", "fund", "unknown"
READABLE = {".xlsx", ".xlsm", ".csv", ".html", ".htm", ".pdf", ".zip"}

# Кэш сведений о бумагах из T-Invest API: ISIN и эмитент выпуска не меняются,
# а каждый лишний запрос — лишний шанс попасть на обрыв VPN.
INSTRUMENTS = "tinvest_instruments.json"

Sheet = tuple[str, list[list[str]]]


@dataclass
class Position:
    isin: str
    quantity: float
    portfolio: str
    name: str = ""
    kind: str = UNKNOWN
    issuer: str = ""
    # Биржевой код: ОФЗ ищутся по нему (SU26253RMFS3), а не по ISIN.
    code: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- чтение файлов

def _rows_from_xlsx(path: Path) -> list[Sheet]:
    import warnings

    from openpyxl import load_workbook

    # Брокерские выгрузки идут без описания стилей — openpyxl предупреждает об этом
    # на каждом файле, засоряя лог. На разбор данных это никак не влияет.
    warnings.filterwarnings("ignore", message=".*no default style.*")

    # read_only=True не годится: отчёт Т-Банка не использует общую таблицу строк,
    # и в потоковом режиме openpyxl отдаёт одну строку вместо всей таблицы.
    book = load_workbook(path, data_only=True)
    sheets = [
        (
            worksheet.title,
            [
                ["" if cell is None else str(cell).strip() for cell in row]
                for row in worksheet.iter_rows(values_only=True)
            ],
        )
        for worksheet in book.worksheets
    ]
    book.close()
    return sheets


def _rows_from_csv(path: Path) -> list[Sheet]:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            with path.open(encoding=encoding, newline="") as fh:
                sample = fh.read(8192)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
                except csv.Error:
                    dialect = csv.excel
                return [(path.stem, [[c.strip() for c in row] for row in csv.reader(fh, dialect)])]
        except UnicodeDecodeError:
            continue
    return []


def _rows_from_html(path: Path) -> list[Sheet]:
    from bs4 import BeautifulSoup

    for encoding in ("utf-8", "cp1251"):
        try:
            soup = BeautifulSoup(path.read_text(encoding=encoding), "lxml")
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    rows = [
        [cell.get_text(strip=True) for cell in row.find_all(["td", "th"])]
        for row in soup.find_all("tr")
    ]
    return [(path.stem, rows)]


def _rows_from_pdf(path: Path) -> list[Sheet]:
    try:
        import pdfplumber
    except ImportError:
        log.error("Для PDF нужен pdfplumber: pip install pdfplumber")
        return []

    rows: list[list[str]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                rows.extend([["" if c is None else str(c).strip() for c in r] for r in table])
            if not rows:
                # Таблицы в брокерских PDF часто без линий — тогда extract_tables пуст.
                rows.extend(line.split() for line in (page.extract_text() or "").split("\n"))
    return [(path.stem, rows)]


def unpack_zip(path: Path) -> Path | None:
    """Альфа отдаёт отчёт в архиве. Распаковываем рядом и работаем с содержимым.

    Имена внутри архива записаны в кодировке DOS — восстанавливаем русские названия,
    иначе файл появится на диске под нечитаемым именем.
    """
    extracted: list[Path] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            try:
                name = info.filename.encode("cp437").decode("cp866")
            except (UnicodeEncodeError, UnicodeDecodeError):
                name = info.filename
            target = path.parent / Path(name).name
            if Path(name).suffix.lower() in READABLE - {".zip"}:
                target.write_bytes(archive.read(info))
                extracted.append(target)
    return max(extracted, key=lambda p: p.stat().st_size) if extracted else None


READERS = {
    ".xlsx": _rows_from_xlsx,
    ".xlsm": _rows_from_xlsx,
    ".csv": _rows_from_csv,
    ".html": _rows_from_html,
    ".htm": _rows_from_html,
    ".pdf": _rows_from_pdf,
}


# ------------------------------------------------------------------ примитивы

def _number(value: str) -> float | None:
    cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not cleaned or not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None
    return float(cleaned)


def _norm(value: str) -> str:
    """Заголовки содержат переносы строк и сноски вида «кол-во³, шт./грамм⁵»."""
    return re.sub(r"\s+", " ", value).strip().lower()


def _find_header(rows: list[list[str]], *required: str) -> tuple[int, dict[str, int]] | None:
    """Первая строка, где встречаются все заданные подстроки заголовков.

    Возвращает её номер и отображение «текст заголовка → индекс колонки»,
    чтобы дальше обращаться к колонкам по смыслу, а не по номеру.
    """
    for index, row in enumerate(rows):
        cells = {_norm(cell): position for position, cell in enumerate(row) if cell.strip()}
        if all(any(req in header for header in cells) for req in required):
            return index, cells
    return None


def _column(headers: dict[str, int], *candidates: str) -> int | None:
    """Индекс колонки по первому совпавшему названию — порядок задаёт приоритет."""
    for candidate in candidates:
        for header, index in headers.items():
            if candidate in header:
                return index
        # Точного совпадения нет — ищем частичное среди всех заголовков.
    return None


def _cell(row: list[str], index: int | None) -> str:
    return row[index].strip() if index is not None and index < len(row) else ""


def _classify(text: str) -> str:
    lowered = text.lower()
    if "обл" in lowered:
        return BOND
    if "акци" in lowered:
        return SHARE
    if any(word in lowered for word in ("пиф", "фонд", "прочее", "етф", "etf")):
        return FUND
    return UNKNOWN


# -------------------------------------------------------------- парсеры брокеров

def _parse_tbank(sheets: list[Sheet], key: str) -> tuple[list[Position], list[str]]:
    rows = sheets[0][1]

    # Справочник бумаг: он даёт тип (акция или облигация) и эмитента —
    # именно по типу дальше решается, какие новости присылать, а какие нет.
    catalogue: dict[str, tuple[str, str]] = {}
    catalogue_rows: set[int] = set()  # чтобы не считать их пропущенными позициями
    search_from = 0
    while (found := _find_header(rows[search_from:], "isin", "тип", "наименование эмитента")):
        offset, headers = found
        start = search_from + offset
        isin_col = _column(headers, "isin")
        kind_col = _column(headers, "тип")
        issuer_col = _column(headers, "наименование эмитента")
        catalogue_rows.add(start)
        for number, row in enumerate(rows[start + 1:], start=start + 1):
            isin = _cell(row, isin_col)
            if not ISIN.fullmatch(isin):
                break
            catalogue[isin] = (_classify(_cell(row, kind_col)), _cell(row, issuer_col))
            catalogue_rows.add(number)
        search_from = start + 1

    found = _find_header(rows, "isin", "исходящий остаток")
    if not found:
        return [], ["не найден раздел позиций"]
    start, headers = found
    isin_col = _column(headers, "isin")
    name_col = _column(headers, "наименование актива")
    code_col = _column(headers, "код актива")
    # «Плановый» учитывает незавершённые сделки — это и есть актуальная позиция.
    qty_col = _column(headers, "плановый исходящий остаток") or _column(headers, "исходящий остаток")

    positions, skipped = [], []
    for number, row in enumerate(rows[start + 1:], start=start + 1):
        isin = _cell(row, isin_col)
        if not ISIN.fullmatch(isin):
            # Строка со «своим» ISIN не в колонке позиций — либо справочник
            # (он уже разобран выше), либо действительно потерянная бумага.
            if number not in catalogue_rows and ISIN.search(" ".join(row)):
                skipped.append(" | ".join(c for c in row if c.strip())[:160])
            continue
        quantity = _number(_cell(row, qty_col))
        if not quantity:  # ноль — бумага продана внутри периода, в портфеле её нет
            continue
        kind, issuer = catalogue.get(isin, (UNKNOWN, ""))
        code = _cell(row, code_col)
        positions.append(
            Position(isin, quantity, key, _cell(row, name_col), kind, issuer,
                     code="" if code == isin else code)
        )
    return positions, skipped


def _parse_alpha(sheets: list[Sheet], key: str) -> tuple[list[Position], list[str]]:
    rows = next((rows for title, rows in sheets if "позиц" in title.lower()), None)
    if rows is None:
        rows = sheets[0][1]

    found = _find_header(rows, "инструмент", "кол-во")
    if not found:
        return [], ["не найден раздел позиций"]
    start, headers = found
    section_col = _column(headers, "инструмент")
    name_col = _column(headers, "актив")
    # Три колонки количества: на начало, на конец, плановое. Берём плановое —
    # последнее по порядку, оно учитывает незавершённые сделки.
    qty_col = max(index for header, index in headers.items() if "кол-во" in header)

    positions, skipped = [], []
    section = ""
    pending: tuple[str, float, str] | None = None

    for row in rows[start + 1:]:
        label = _cell(row, section_col)
        if label and not _number(label):
            section = label

        name = _cell(row, name_col)
        if ISIN.fullmatch(name):
            # Строка с одним ISIN относится к предыдущей строке с количеством.
            if pending is None:
                skipped.append(f"ISIN без позиции: {name}")
                continue
            title, quantity, kind = pending
            positions.append(Position(name, quantity, key, title, kind))
            pending = None
            continue

        quantity = _number(_cell(row, qty_col))
        if name and quantity:
            pending = (name, quantity, _classify(section))

    return positions, skipped


def _parse_generic(sheets: list[Sheet], key: str) -> tuple[list[Position], list[str]]:
    """Запасной разбор для незнакомого формата: ищем строки, где есть ISIN и число."""
    positions, skipped = [], []
    for _, rows in sheets:
        for row in rows:
            joined = " ".join(row)
            match = ISIN.search(joined)
            if not match:
                continue
            isin_at = next((i for i, cell in enumerate(row) if match.group(0) in cell), -1)
            quantity = next(
                (
                    value
                    for cell in row[isin_at + 1:]
                    if (value := _number(cell)) and value > 0 and value == int(value)
                ),
                None,
            )
            if quantity:
                positions.append(Position(match.group(0), quantity, key))
            else:
                skipped.append(joined[:160])
    return positions, skipped


def _detect(sheets: list[Sheet]) -> str:
    titles = " ".join(title.lower() for title, _ in sheets)
    if "динамика позиций" in titles:
        return "alpha"
    for _, rows in sheets:
        if _find_header(rows, "isin", "исходящий остаток"):
            return "tbank"
        if _find_header(rows, "инструмент", "кол-во"):
            return "alpha"
    return "generic"


PARSERS = {"tbank": _parse_tbank, "alpha": _parse_alpha, "generic": _parse_generic}


# ------------------------------------------------------------------ точка входа

def latest_report(portfolio: Portfolio) -> Path | None:
    """Самый свежий пригодный файл в папке портфеля."""
    if portfolio.reports_dir is None:
        return None
    if not portfolio.reports_dir.exists():
        return None
    files = [
        path
        for path in portfolio.reports_dir.iterdir()
        if path.is_file() and path.suffix.lower() in READABLE and not path.name.startswith("~$")
    ]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def parse_portfolio(portfolio: Portfolio) -> tuple[list[Position], list[str], Path | None, str]:
    path = latest_report(portfolio)
    if path is None:
        return [], [], None, ""
    if path.suffix.lower() == ".zip":
        unpacked = unpack_zip(path)
        if unpacked is None:
            return [], ["в архиве нет пригодного файла"], path, ""
        path = unpacked

    sheets = READERS[path.suffix.lower()](path)
    if not sheets:
        return [], ["файл не удалось прочитать"], path, ""

    broker = _detect(sheets)
    positions, skipped = PARSERS[broker](sheets, portfolio.key)

    # Одна бумага может встретиться в нескольких разделах — берём наибольшее количество.
    merged: dict[str, Position] = {}
    for position in positions:
        current = merged.get(position.isin)
        if current is None or position.quantity > current.quantity:
            merged[position.isin] = position
    return list(merged.values()), skipped, path, broker


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _collect_tinvest(config: Config, portfolio: Portfolio, previous: dict | None) -> dict:
    """Блок портфеля из T-Invest API. Если API не ответил — прошлый блок
    с пометкой stale: календарь строится на последнем известном составе."""
    cache = config.state_file(INSTRUMENTS)
    bonds = tinvest.fetch_bonds(
        config.tinvest_token, account=config.tinvest_account, known=_read_json(cache)
    )
    if bonds is None:
        block = dict(previous) if previous else {
            "title": portfolio.title,
            "source_file": None,
            "format": "tinvest-api",
            "synced": None,
            "positions": [],
            "skipped": [],
        }
        block["stale"] = True
        return block

    cache.write_text(
        json.dumps(
            {bond["uid"]: {field: bond[field] for field in tinvest.FIELDS} for bond in bonds},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    positions = [
        Position(bond["isin"], bond["quantity"], portfolio.key, bond["name"], BOND,
                 bond["issuer"], bond["code"])
        for bond in bonds
    ]
    return {
        "title": portfolio.title,
        "source_file": None,
        "format": "tinvest-api",
        "synced": dt.datetime.now().isoformat(timespec="seconds"),
        "stale": False,
        "positions": [p.as_dict() for p in sorted(positions, key=lambda p: (p.kind, p.name))],
        "skipped": [],
    }


def collect(config: Config) -> dict:
    """Собирает позиции всех портфелей и пишет holdings.json."""
    target = config.state_file("holdings.json")
    previous = _read_json(target).get("portfolios", {})
    holdings = {"updated": dt.datetime.now().isoformat(timespec="seconds"), "portfolios": {}}
    for portfolio in config.portfolios:
        if portfolio.source == "tinvest":
            holdings["portfolios"][portfolio.key] = _collect_tinvest(
                config, portfolio, previous.get(portfolio.key)
            )
            continue
        if portfolio.source != "reports":
            raise SystemExit(f"Неизвестный источник портфеля «{portfolio.title}»: {portfolio.source}")
        positions, skipped, path, broker = parse_portfolio(portfolio)
        holdings["portfolios"][portfolio.key] = {
            "title": portfolio.title,
            "source_file": path.name if path else None,
            "format": broker,
            "positions": [p.as_dict() for p in sorted(positions, key=lambda p: (p.kind, p.name))],
            "skipped": skipped,
        }
    target.write_text(json.dumps(holdings, ensure_ascii=False, indent=2), encoding="utf-8")
    return holdings
