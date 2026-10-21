"""Локальный сервер: отдаёт страницу и JSON. Слушает только 127.0.0.1.

    GET  /api/report?portfolio=alpha     сводка, позиции, доходы, структура
    GET  /api/operations?portfolio=…     журнал операций
    POST /api/operations                 добавить операцию руками
    DELETE /api/operations/<id>          удалить операцию (только ручную)
    POST /api/sync                       запустить синхронизацию в фоне
    GET  /api/sync                       состояние синхронизации и итог последней

Синхронизация идёт в отдельном потоке со своим соединением: она ходит в сеть
до минуты, и страница не должна висеть всё это время.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, model, sync

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

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
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Портфель: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
