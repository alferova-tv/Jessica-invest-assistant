"""База портфеля: SQLite, одна таблица операций и справочники вокруг неё.

Всё, что показывает интерфейс, считается из `operations` на лету. Таблица хранит
движение денег так, как его видит брокерский счёт:

* `amount` — деньги со знаком: покупка минус, купон плюс. Включает НКД, без комиссии.
* `accrued` — НКД внутри `amount` (уплаченный при покупке, полученный при продаже).
* `fee` — комиссия брокера, всегда положительное число, списывается отдельно.
* `quantity` — штуки, всегда положительное число; направление задаёт `kind`.

Виды операций — константы ниже. `opening` — входящий остаток: позиция, купленная
раньше первой загруженной выписки. Её цена неизвестна, и за себестоимость
берётся стоимость на начало выписки; интерфейс помечает такие позиции.

Повторная загрузка той же выписки ничего не задваивает: у каждой операции есть
`ext_id` (номер сделки брокера или отпечаток строки), уникальный в пределах источника.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import paths

BUY, SELL, OPENING = "buy", "sell", "opening"
COUPON, DIVIDEND, AMORTIZATION, REDEMPTION = "coupon", "dividend", "amortization", "redemption"
BONUS, FEE, TAX, DEPOSIT, WITHDRAWAL, OTHER = "bonus", "fee", "tax", "deposit", "withdrawal", "other"

KINDS = (BUY, SELL, OPENING, COUPON, DIVIDEND, AMORTIZATION, REDEMPTION, BONUS, FEE, TAX, DEPOSIT, WITHDRAWAL, OTHER)
TRADES = (BUY, SELL, OPENING)
INCOME = (COUPON, DIVIDEND, BONUS)

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id         INTEGER PRIMARY KEY,
    portfolio  TEXT NOT NULL,
    source     TEXT NOT NULL,            -- alfa | tinvest | manual | jessica
    ext_id     TEXT,
    date       TEXT NOT NULL,            -- ГГГГ-ММ-ДД
    kind       TEXT NOT NULL,
    isin       TEXT,
    quantity   REAL NOT NULL DEFAULT 0,
    amount     REAL NOT NULL,
    accrued    REAL NOT NULL DEFAULT 0,
    fee        REAL NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT '',
    UNIQUE (source, ext_id)
);
CREATE INDEX IF NOT EXISTS operations_isin ON operations (isin);

-- Справочник бумаг: из ISS, а для чего ISS не знает — из выписки.
CREATE TABLE IF NOT EXISTS securities (
    isin       TEXT PRIMARY KEY,
    secid      TEXT,
    name       TEXT,
    kind       TEXT,                     -- bond | share | fund | other
    issuer     TEXT,
    regnumber  TEXT,
    market     TEXT,                     -- bonds | shares (рынок ISS)
    board      TEXT,
    facevalue  REAL,
    currency   TEXT,
    maturity   TEXT,
    updated    TEXT,
    sector     TEXT                      -- сектор экономики по T-Invest: energy, financial…
);
CREATE INDEX IF NOT EXISTS securities_reg ON securities (regnumber);

-- Последняя цена: за штуку в рублях, чистая (без НКД), и НКД на штуку.
CREATE TABLE IF NOT EXISTS quotes (
    isin       TEXT PRIMARY KEY,
    price      REAL NOT NULL,
    accrued    REAL NOT NULL DEFAULT 0,
    date       TEXT NOT NULL,
    source     TEXT NOT NULL,
    prev       REAL,                     -- закрытие прошлого торгового дня, за штуку
    info       TEXT                      -- JSON: купон, доходность к погашению, дюрация
);

-- Будущие выплаты на одну бумагу: купоны, амортизации, погашения.
CREATE TABLE IF NOT EXISTS payments (
    isin       TEXT NOT NULL,
    date       TEXT NOT NULL,
    kind       TEXT NOT NULL,            -- coupon | amortization | redemption
    per_unit   REAL,
    start      TEXT,                     -- начало купонного периода
    estimated  INTEGER NOT NULL DEFAULT 0,   -- 1: сумма купона ещё не объявлена
    PRIMARY KEY (isin, date, kind)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or paths.db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(payments)")}
    if "start" not in columns:  # база первой ночной версии
        conn.execute("ALTER TABLE payments ADD COLUMN start TEXT")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(quotes)")}
    if "prev" not in columns:  # до изменения за день и карточки бумаги
        conn.execute("ALTER TABLE quotes ADD COLUMN prev REAL")
        conn.execute("ALTER TABLE quotes ADD COLUMN info TEXT")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(securities)")}
    if "sector" not in columns:  # до диверсификации по секторам
        conn.execute("ALTER TABLE securities ADD COLUMN sector TEXT")
    return conn


@contextmanager
def session(path: Path | None = None):
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def upsert_operation(conn: sqlite3.Connection, op: dict) -> bool:
    """Добавить операцию, если такой ещё нет. True — добавлена."""
    columns = ("portfolio", "source", "ext_id", "date", "kind", "isin", "quantity", "amount", "accrued", "fee", "note")
    values = [op.get(column) for column in columns]
    for index, default in ((6, 0), (8, 0), (9, 0), (10, "")):
        if values[index] is None:
            values[index] = default
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO operations ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        values,
    )
    return cursor.rowcount == 1


def upsert_security(conn: sqlite3.Connection, info: dict) -> None:
    """Дополнить справочник: пустые поля не затирают известные."""
    columns = [key for key in ("secid", "name", "kind", "issuer", "regnumber", "market", "board",
                               "facevalue", "currency", "maturity", "updated", "sector") if info.get(key) not in (None, "")]
    conn.execute("INSERT OR IGNORE INTO securities (isin) VALUES (?)", (info["isin"],))
    if columns:
        conn.execute(
            f"UPDATE securities SET {', '.join(f'{c} = ?' for c in columns)} WHERE isin = ?",
            [info[c] for c in columns] + [info["isin"]],
        )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
