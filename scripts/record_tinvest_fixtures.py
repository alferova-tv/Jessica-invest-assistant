"""Настоящие ответы T-Invest API — в папку с данными, и сверка маркеров новостей.

Токен скрипт берёт из config.yaml сам, в вывод и файлы он не попадает.
Ответы пишутся в ~/Documents/Tracking/Invest/tinvest-recordings/<время>/,
а не в репозиторий: в них состав портфеля и номер счёта.

Запуск из корня репозитория:
    .venv/bin/python scripts/record_tinvest_fixtures.py [ключ портфеля, по умолчанию tinkoff]
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jessica.config import Config  # noqa: E402
from jessica.sources import news, tinvest  # noqa: E402


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def _markers(name: str, issuer: str) -> str:
    found = news.company_markers(name, issuer)
    return f"stems={sorted(found['stems'])} abbr={sorted(found['abbr'])}"


def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else "tinkoff"
    config = Config.load()
    out = config.root / "tinvest-recordings" / dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out.mkdir(parents=True)
    os.chmod(out, 0o700)

    calls = []
    original = tinvest._post

    def recording(method, payload, headers):
        response = original(method, payload, headers)
        calls.append({"method": method, "payload": payload, "response": response})  # без headers
        return response

    tinvest._post = recording
    try:
        bonds = tinvest.fetch_positions(config.tinvest_token, account=config.tinvest_account)
    finally:
        tinvest._post = original
    _save(out / "calls.json", calls)
    if bonds is None:
        print(f"API не ответил; удачных вызовов записано: {len(calls)} → {out}")
        return 1
    _save(out / "bonds.json", bonds)

    current = json.loads(config.state_file("holdings.json").read_text(encoding="utf-8"))
    old = {p["isin"]: p for p in current["portfolios"][key]["positions"]}
    new = {b["isin"]: b for b in bonds}
    print(f"Записано в {out}\n")
    for isin in sorted(old.keys() | new.keys()):
        before, after = old.get(isin), new.get(isin)
        if before is None or after is None:
            print(f"{'+ только в API ' if before is None else '- только в holdings'} {isin} "
                  f"{(after or before)['name']}")
            continue
        was = _markers(before["name"], before.get("issuer", ""))
        now = _markers(after["name"], after["issuer"])
        quantity = "" if before["quantity"] == after["quantity"] else \
            f"  количество {before['quantity']:.0f} → {after['quantity']:.0f}"
        mark = "  " if was == now else "≠ "
        print(f"{mark}{isin} {after['name']!r}{quantity}")
        if was != now or news.silent(after["name"], after["issuer"]):
            print(f"      сейчас: {before['name']!r} / {before.get('issuer', '')!r}\n              {was}")
            print(f"      из API: {after['name']!r} / {after['issuer']!r}\n              {now}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
