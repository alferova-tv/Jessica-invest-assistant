"""Локальный сервер: отдаёт страницу и JSON. Слушает только 127.0.0.1.

    GET  /api/report?portfolio=alpha     сводка, позиции, доходы, структура
    GET  /api/operations?portfolio=…     журнал операций
    GET  /api/security?isin=…            карточка бумаги с историей цены за год
    GET  /api/candles?isin=…&range=day   цена внутри дня (10 мин) или недели (1 ч)
    POST /api/operations                 добавить операцию руками
    DELETE /api/operations/<id>          удалить операцию (только ручную)
    POST /api/sync                       запустить синхронизацию в фоне
    GET  /api/sync                       состояние синхронизации и итог последней

Синхронизация идёт в отдельном потоке со своим соединением: она ходит в сеть
до минуты, и страница не должна висеть всё это время. Ещё один поток раз
в 15 минут в часы торгов обновляет только котировки.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import db, iss, model, paths, sync

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

# История цены за год меняется раз в день — не спрашиваем ISS на каждое открытие карточки.
_history_cache: dict[tuple[str, str], list[dict]] = {}

_sync_lock = threading.Lock()
_sync_state = {"running": False, "error": None}


def _run_sync(tinvest_api: bool) -> None:
    try:
        sync.run(tinvest_api=tinvest_api)
        _sync_state["error"] = None
    except Exception as exc:
        log.exception("синхронизация упала")
        _sync_state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _sync_state["running"] = False
        _sync_lock.release()


def start_sync(tinvest_api: bool = True) -> bool:
    if not _sync_lock.acquire(blocking=False):
        return False
    _sync_state["running"] = True
    threading.Thread(target=_run_sync, args=(tinvest_api,), daemon=True).start()
    return True


QUOTES_EVERY = 15 * 60
MSK = ZoneInfo("Europe/Moscow")


def _trading(now: dt.datetime) -> bool:
    """Проверки котировок с 10 до 20 МСК, с учётом календаря фондового рынка."""
    if not dt.time(10) <= now.time() <= dt.time(20):
        return False
    if str(paths.JESSICA_CODE) not in sys.path:
        sys.path.insert(0, str(paths.JESSICA_CODE))
    from jessica.sources import trading_calendar
    day = trading_calendar.get_day(now.date(), paths.invest_home() / "state/trading_calendar.json")
    # При неизвестном календаре пробуем получить котировки. Их фактические даты
    # сохраняются источником; неизвестный день не объявляется торговым.
    return day is None or day["is_traded"] is not False


def _quotes_loop() -> None:
    while True:
        if _trading(dt.datetime.now(MSK)) and _sync_lock.acquire(blocking=False):
            try:
                sync.run_quotes()
            except Exception:  # VPN, ISS молчит — попробуем через 15 минут
                log.exception("котировки не обновились")
            finally:
                _sync_lock.release()
        time.sleep(QUOTES_EVERY)


def _history(security: dict) -> tuple[list[dict] | None, str | None]:
    if not (security.get("secid") and security.get("market") and security.get("board")):
        return None, "биржа не знает эту бумагу"
    key = (security["isin"], dt.date.today().isoformat())
    if key not in _history_cache:
        since = (dt.date.today() - dt.timedelta(days=366)).isoformat()
        try:
            _history_cache[key] = iss.history(security, since)
        except iss.Unavailable as exc:
            return None, f"ISS не ответил ({exc})"
    return _history_cache[key], None


def _candles(security: dict, span: str) -> tuple[list[dict] | None, str | None]:
    """День — десятиминутки последнего торгового дня, неделя — часовые свечи за 7 дней."""
    if not (security.get("secid") and security.get("market") and security.get("board")):
        return None, "биржа не знает эту бумагу"
    days, interval = (5, 10) if span == "day" else (7, 60)
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    try:
        points = iss.candles(security, since, interval)
    except iss.Unavailable as exc:
        return None, f"ISS не ответил ({exc})"
    if span == "day" and points:
        last_day = points[-1]["date"][:10]
        points = [p for p in points if p["date"].startswith(last_day)]
    return points, None


def _manual_operation(body: dict) -> dict:
    """Ручной ввод: сумма и количество — положительные, знак ставим по виду."""
    kind = body.get("kind")
    if kind not in db.KINDS:
        raise ValueError("неизвестный вид операции")
    date = dt.date.fromisoformat(body.get("date") or "").isoformat()
    quantity = abs(float(body.get("quantity") or 0))
    amount = abs(float(body.get("amount") or 0))
    accrued = abs(float(body.get("accrued") or 0))
    fee = abs(float(body.get("fee") or 0))
    isin = (body.get("isin") or "").strip().upper() or None
    if kind in db.TRADES and (not isin or quantity <= 0):
        raise ValueError("для сделки нужны ISIN и количество")
    negative = kind in (db.BUY, db.OPENING, db.TAX, db.WITHDRAWAL)
    return {
        "portfolio": body.get("portfolio") or "manual", "source": "manual",
        "ext_id": f"manual:{dt.datetime.now().isoformat()}", "date": date, "kind": kind,
        "isin": isin, "quantity": quantity, "amount": -amount if negative else amount,
        "accrued": accrued, "fee": fee, "note": (body.get("note") or "").strip(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "portfel"

    def log_message(self, fmt, *args):  # без шума в консоли на каждый запрос
        log.debug(fmt, *args)

    def _send(self, status: int, payload, content_type: str = "application/json; charset=utf-8") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _guard(self, action):
        try:
            action()
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            log.error("%s", traceback.format_exc())
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    def do_DELETE(self):
        self._guard(self._delete)

    def _get(self):
        path = urlparse(self.path).path
        query = self._query()
        portfolio = query.get("portfolio") or None
        if path == "/api/report":
            with db.session() as conn:
                report = model.build(conn, portfolio)
                report["last_sync"] = model.last_sync(conn)
                report["portfolios"] = model.PORTFOLIO_TITLES
            report["syncing"] = _sync_state["running"]
            return self._send(HTTPStatus.OK, report)
        if path == "/api/operations":
            with db.session() as conn:
                return self._send(HTTPStatus.OK, model.operations(conn, portfolio))
        if path == "/api/security":
            with db.session() as conn:
                card = model.security(conn, query.get("isin") or "", portfolio)
            card["history"], card["history_error"] = _history(card["security"])
            card["portfolios"] = model.PORTFOLIO_TITLES
            return self._send(HTTPStatus.OK, card)
        if path == "/api/candles":
            with db.session() as conn:
                row = conn.execute("SELECT * FROM securities WHERE isin = ?", (query.get("isin") or "",)).fetchone()
            if not row:
                raise ValueError("нет такой бумаги")
            points, error = _candles(dict(row), query.get("range") or "day")
            return self._send(HTTPStatus.OK, {"history": points, "history_error": error})
        if path == "/api/sync":
            with db.session() as conn:
                return self._send(HTTPStatus.OK, {**_sync_state, "last": model.last_sync(conn)})
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        file = (STATIC / name).resolve()
        if file.parent != STATIC or not file.exists():
            return self._send(HTTPStatus.NOT_FOUND, {"error": "нет такой страницы"})
        self._send(HTTPStatus.OK, file.read_bytes(), TYPES.get(file.suffix, "application/octet-stream"))

    def _post(self):
        path = urlparse(self.path).path
        if path == "/api/sync":
            started = start_sync(tinvest_api=self._query().get("tinvest", "1") != "0")
            return self._send(HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT,
                              {"started": started, **_sync_state})
        if path == "/api/operations":
            op = _manual_operation(self._body())
            with db.session() as conn:
                db.upsert_operation(conn, op)
            return self._send(HTTPStatus.CREATED, {"ok": True})
        self._send(HTTPStatus.NOT_FOUND, {"error": "нет такого метода"})

    def _delete(self):
        path = urlparse(self.path).path
        if path.startswith("/api/operations/"):
            op_id = int(path.rsplit("/", 1)[1])
            with db.session() as conn:
                deleted = conn.execute("DELETE FROM operations WHERE id = ? AND source = 'manual'", (op_id,)).rowcount
            if not deleted:
                raise ValueError("удалить можно только операцию, внесённую руками")
            return self._send(HTTPStatus.OK, {"ok": True})
        self._send(HTTPStatus.NOT_FOUND, {"error": "нет такого метода"})


def serve(port: int) -> None:
    db.connect().close()  # создать базу до первого запроса
    threading.Thread(target=_quotes_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Портфель: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
