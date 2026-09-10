# Состав портфеля Т-Инвестиций из T-Invest API — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** портфель Т-Инвестиций собирается в `holdings.json` из T-Invest API (read-only токен) вместо брокерской выгрузки; при недоступности API остаётся прошлый состав с пометкой `stale`.

**Architecture:** новый источник `jessica/sources/tinvest.py` отдаёт список словарей по облигациям счёта; `holdings.collect()` выбирает источник по полю `source` портфеля в конфиге, держит кэш сведений о бумагах и подставляет прошлый блок при отказе. `cli.run` повторяет суточное обновление раз в `refresh_retry_hours`, пересобирая календарь только при изменении состава. Поиск компаний в новостях (`news.company_markers`) дорабатывается под названия из API.

**Tech Stack:** Python 3.14 (`~/Projects/jessica/.venv`), `requests`, `PyYAML`, тесты — `unittest` из стандартной библиотеки.

**Spec:** `docs/superpowers/specs/2026-09-10-tinvest-holdings-design.md`

## Global Constraints

- **launchd запускает `jessica run` ежечасно прямо из рабочего дерева репозитория — каждый коммит сразу боевой.** До задачи 7 (переключение конфига хозяйкой) поведение для портфеля Т не должно меняться: он остаётся на выгрузке, `_parse_tbank` не трогать.
- **Агенту запрещено читать и править `~/Documents/Tracking/Invest/config.yaml` и файлы `tinkoff/*token*`** (правила `deny` в `~/.claude/settings.json`). Не пытаться обходить. Все правки конфига делает хозяйка; агент даёт ей готовый фрагмент. Запускать код, который сам читает конфиг (`python -m jessica …`, `scripts/…`), можно — токен не должен попадать в вывод, логи и файлы.
- Новых зависимостей нет, `requirements.txt` не меняется.
- Тесты запускаются из корня репозитория: `.venv/bin/python -m unittest discover -s tests -v`. Сеть в тестах не используется.
- Настоящие ответы API (состав портфеля, номер счёта) в git не попадают. Они лежат в `~/Documents/Tracking/Invest/tinvest-recordings/`. Фикстуры в `tests/fixtures/` — синтетические.
- Russian Trusted Root CA используется только как `verify=` в `jessica/sources/tinvest.py`. Системное хранилище не трогать. SHA-256 корня: `D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31`.
- `fetch_bonds` никогда не возвращает частичный портфель: любая ошибка → `None`.
- Комментарии, докстринги, сообщения и коммиты — по-русски, в стиле окружающего кода: комментарий объясняет «почему», а не «что». Каждый коммит заканчивается строками:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01JPx62kAay6GDwmCREn7uU4
  ```
- Коммиты — в `main`, как вся история проекта. Не пушить без просьбы хозяйки.

## Карта файлов

| файл | что меняется |
|---|---|
| `jessica/sources/news.py` | `ГК` в `STOP_ABBR`, `кредитный`/`поток` в `STOPWORDS`, новая `silent()` |
| `jessica/config.py` | `Portfolio.source`, необязательный `reports_dir`, `tinvest_token`, `tinvest_account`, `refresh_retry_hours` |
| `jessica/sources/tinvest.py` | **новый**: `fetch_bonds()` |
| `jessica/certs/russian_trusted_root_ca.pem` | **новый**: корень Минцифры |
| `jessica/holdings.py` | маршрутизация по `source`, кэш бумаг, `stale`-блок; позже — удаление `_parse_tbank` |
| `jessica/cli.py` | предупреждения в `import`/`refresh`, `refresh_due`/`rebuild_needed` в `run`, проверка API в `doctor` |
| `scripts/record_tinvest_fixtures.py` | **новый**: запись настоящих ответов и сверка маркеров |
| `tests/…` | **новые**: `test_news`, `test_config`, `test_tinvest`, `test_certs`, `test_holdings`, `test_run`, `fixtures/tinvest/responses.json` |
| `CLAUDE.md`, `README.md` | описание нового источника |

---

### Task 1: маркеры новостей под названия из API

**Files:**
- Modify: `jessica/sources/news.py` (блоки `STOPWORDS`, `STOP_ABBR`, новая функция после `mentions`)
- Create: `tests/test_news.py`

**Interfaces:**
- Produces: `news.silent(name: str, issuer: str = "") -> bool` — `True`, если бумага не госбумага, не секьюритизация и маркеров у неё нет. Используется в задаче 5.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_news.py`:

```python
import unittest

from jessica.sources.news import company_markers, silent


class CompanyMarkersTest(unittest.TestCase):
    def test_gk_is_not_an_abbreviation(self):
        # «ГК» — организационная приставка: \bГК\b совпадает с любым «ГК ПИК».
        markers = company_markers("ГК Самолет БО-П20")
        self.assertNotIn("ГК", markers["abbr"])
        self.assertIn("самол", markers["stems"])

    def test_securitization_product_name_gives_no_stems(self):
        # Так API называет выпуск СФО ТБ-4; «кредитн» ловило бы «кредитный рейтинг».
        self.assertEqual(company_markers("Кредитный поток 2.0")["stems"], set())

    def test_glued_issue_code_is_rescued_by_issuer(self):
        # API пишет «АЛИУМ01Р1» слитно, и регэксп кодов съедает слово целиком.
        self.assertEqual(company_markers("АЛИУМ01Р1"), {"stems": set(), "abbr": set()})
        self.assertIn("алиу", company_markers("АЛИУМ01Р1", "Алиум")["stems"])

    def test_government_bond_has_no_markers(self):
        markers = company_markers("ОФЗ 26253", "Министерство финансов Российской Федерации")
        self.assertEqual(markers, {"stems": set(), "abbr": set()})


class SilentTest(unittest.TestCase):
    def test_company_without_markers_is_silent(self):
        self.assertTrue(silent("АЛИУМ01Р1"))

    def test_company_with_markers_is_not_silent(self):
        self.assertFalse(silent("АЛИУМ01Р1", "Алиум"))

    def test_government_bond_is_not_silent(self):
        self.assertFalse(silent("ОФЗ 26253"))

    def test_securitization_is_not_silent(self):
        self.assertFalse(silent("СФО Альфа Фабрика ПК-3 класс А"))
        self.assertFalse(silent("Кредитный поток 2.0", 'ООО "СФО ТБ-4"'))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: ошибка импорта `cannot import name 'silent'`.

- [ ] **Step 3: Реализация**

В `jessica/sources/news.py` заменить

```python
    "село", "северо", "запад", "нефть", "банк", "проект", "новый", "перв", "трейд", "групп",
}
# Организационные сокращения: опознают форму собственности, а не компанию.
STOP_ABBR = {"ПАО", "АО", "ООО", "ЗАО", "ОАО", "НАО", "МКПАО", "СФО", "БПИФ", "БО", "ПК", "ОФЗ"}
```

на

```python
    "село", "северо", "запад", "нефть", "банк", "проект", "новый", "перв", "трейд", "групп",
    # Так API брокера называет секьюритизации («Кредитный поток 2.0»): это имя
    # продукта, а «кредитн» совпало бы с каждым «кредитным рейтингом» в ленте.
    "кредитный", "поток",
}
# Организационные сокращения: опознают форму собственности, а не компанию.
# «ГК» туда же: целым словом она совпадает с любой «ГК ПИК» или «ГК Астра».
STOP_ABBR = {
    "ПАО", "АО", "ООО", "ЗАО", "ОАО", "НАО", "МКПАО", "СФО", "БПИФ", "БО", "ПК", "ОФЗ", "ГК",
}

# Спецфинобщества секьюритизаций: компании, о которой пишут новости, за ними нет.
SECURITIZATION = re.compile(r"\bСФО\b")
```

После функции `mentions` добавить:

```python
def silent(name: str, issuer: str = "") -> bool:
    """Бумага, новости по которой молча не найдутся: у неё нет ни одного маркера.

    Госбумаги и секьюритизации сюда не входят — у них маркеров не должно быть
    по определению. Остальным пустые маркеры означают, что название выпуска
    не удалось разобрать, и об этом нужно сказать вслух.
    """
    source = f"{name} {issuer}"
    if GOVERNMENT.search(source) or SECURITIZATION.search(source):
        return False
    markers = company_markers(name, issuer)
    return not markers["stems"] and not markers["abbr"]
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 8 тестов, все OK.

- [ ] **Step 5: Проверить, что боевые маркеры не пострадали**

Run:
```bash
.venv/bin/python - <<'PY'
import json, pathlib
from jessica.sources.news import company_markers, silent
h = json.loads((pathlib.Path.home()/"Documents/Tracking/Invest/state/holdings.json").read_text())
for block in h["portfolios"].values():
    for p in block["positions"]:
        if silent(p["name"], p.get("issuer", "")):
            print("без маркеров:", p["name"])
        if "ГК" in p["name"]:
            print(p["name"], company_markers(p["name"], p.get("issuer", "")))
PY
```
Expected: `без маркеров: Т-Технологии МКПАО ао` (давняя ошибка, в этот план не входит) и `ГК Самолет …` с `stems={'самол'}`, `abbr=set()`. Других строк нет.

- [ ] **Step 6: Commit**

```bash
git add jessica/sources/news.py tests/test_news.py
git commit -m "$(cat <<'EOF'
Маркеры новостей: ГК, секьюритизации, бумаги без маркеров

«ГК» больше не считается аббревиатурой компании: целым словом она
совпадала с любой «ГК ПИК». «Кредитный поток» — имя продукта у СФО
в API брокера, а не компании. silent() находит бумаги, по которым
новости молча не найдутся. Первые тесты в проекте.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JPx62kAay6GDwmCREn7uU4
EOF
)"
```

---

### Task 2: конфиг — источник портфеля и настройки T-Invest

**Files:**
- Modify: `jessica/config.py` (класс `Portfolio`, свойство `portfolios`, новые свойства после `market_weekends`)
- Modify: `jessica/holdings.py:360-369` (`latest_report`)
- Create: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Portfolio(key: str, title: str, reports_dir: Path | None, source: str = "reports")`
  - `Config.tinvest_token -> str` (пустая строка, если не задан)
  - `Config.tinvest_account -> str | None`
  - `Config.refresh_retry_hours -> int` (по умолчанию 3)
  - `holdings.latest_report(portfolio)` возвращает `None`, если `reports_dir is None`.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_config.py`:

```python
import tempfile
import unittest
from pathlib import Path

from jessica import holdings
from jessica.config import Config

OLD = {
    "portfolios": {
        "tinkoff": {"title": "Т-Инвестиции", "reports_dir": "tinkoff/reports"},
        "alpha": {"title": "Альфа", "reports_dir": "alpha/reports"},
    },
}
NEW = {
    "tinvest": {"token": "t.TEST", "account": "0000000001"},
    "portfolios": {
        "tinkoff": {"title": "Т-Инвестиции", "source": "tinvest"},
        "alpha": {"title": "Альфа", "reports_dir": "alpha/reports"},
    },
    "schedule": {"refresh_retry_hours": 6},
}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_config_keeps_reports(self):
        config = Config(OLD, self.root)
        tinkoff = config.portfolios[0]
        self.assertEqual(tinkoff.source, "reports")
        self.assertEqual(tinkoff.reports_dir, self.root / "tinkoff/reports")
        self.assertEqual(config.tinvest_token, "")
        self.assertIsNone(config.tinvest_account)
        self.assertEqual(config.refresh_retry_hours, 3)

    def test_tinvest_portfolio_has_no_reports_dir(self):
        config = Config(NEW, self.root)
        tinkoff, alpha = config.portfolios
        self.assertEqual(tinkoff.source, "tinvest")
        self.assertIsNone(tinkoff.reports_dir)
        self.assertIsNone(holdings.latest_report(tinkoff))
        self.assertEqual(alpha.source, "reports")
        self.assertEqual(config.tinvest_token, "t.TEST")
        self.assertEqual(config.tinvest_account, "0000000001")
        self.assertEqual(config.refresh_retry_hours, 6)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/python -m unittest tests.test_config -v`
Expected: `AttributeError: 'Portfolio' object has no attribute 'source'`.

- [ ] **Step 3: Реализация**

В `jessica/config.py` заменить класс `Portfolio`:

```python
@dataclass(frozen=True)
class Portfolio:
    key: str
    title: str
    # Папка выгрузок. У портфеля, который берётся из API брокера, её нет.
    reports_dir: Path | None
    # Откуда брать состав: "reports" — брокерские выгрузки, "tinvest" — T-Invest API.
    source: str = "reports"
```

Заменить свойство `portfolios`:

```python
    @cached_property
    def portfolios(self) -> list[Portfolio]:
        return [
            Portfolio(
                key=key,
                title=spec["title"],
                reports_dir=self.root / spec["reports_dir"] if spec.get("reports_dir") else None,
                source=spec.get("source", "reports"),
            )
            for key, spec in self._raw.get("portfolios", {}).items()
        ]
```

После свойства `market_weekends` добавить:

```python
    @property
    def tinvest_token(self) -> str:
        """Read-only токен T-Invest API. Пустая строка — токен не задан."""
        return str((self._raw.get("tinvest") or {}).get("token") or "")

    @property
    def tinvest_account(self) -> str | None:
        """Номер брокерского счёта. Обычно не нужен: счёт один, его находит API."""
        account = (self._raw.get("tinvest") or {}).get("account")
        return str(account) if account else None

    @property
    def refresh_retry_hours(self) -> int:
        """Как часто `run` повторяет сбор состава, если API брокера не ответил."""
        return int(self._raw.get("schedule", {}).get("refresh_retry_hours", 3))
```

В `jessica/holdings.py` в начало `latest_report` (перед `if not portfolio.reports_dir.exists():`) добавить:

```python
    if portfolio.reports_dir is None:
        return None
```

- [ ] **Step 4: Прогнать все тесты и боевую команду**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: все OK.

Run: `.venv/bin/python -m jessica calendar --days 3`
Expected: список выплат как раньше (конфиг хозяйки ещё старый и должен читаться без изменений).

- [ ] **Step 5: Commit**

```bash
git add jessica/config.py jessica/holdings.py tests/test_config.py
git commit -m "$(cat <<'EOF'
Конфиг: источник состава портфеля и настройки T-Invest

У портфеля появляется source (reports или tinvest), папка выгрузок
становится необязательной. Токен, счёт и интервал повторов сбора
читаются из конфига; старый конфиг работает без изменений.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JPx62kAay6GDwmCREn7uU4
EOF
)"
```

---

### Task 3: источник `sources/tinvest.py` и сертификат

**Files:**
- Create: `jessica/certs/russian_trusted_root_ca.pem`
- Create: `jessica/sources/tinvest.py`
- Create: `tests/fixtures/tinvest/responses.json`
- Create: `tests/test_tinvest.py`, `tests/test_certs.py`

**Interfaces:**
- Produces:
  - `tinvest.fetch_bonds(token: str, *, account: str | None = None, known: dict[str, dict] | None = None) -> list[dict] | None` — элемент: `{"uid", "isin", "name", "issuer", "code", "quantity": float}`.
  - `tinvest.FIELDS = ("isin", "name", "issuer", "code")` — поля кэша.
  - `tinvest.CA_BUNDLE: Path`, `tinvest.API: str`, `tinvest._post(method, payload, headers) -> dict` (задача 6 оборачивает его для записи ответов).

- [ ] **Step 1: Положить сертификат с проверкой отпечатка**

Файл с gu-st.ru качается без проверки TLS (сам сайт за этим же CA), поэтому корень принимается **только** при совпадении эталонного отпечатка.

```bash
S=$(mktemp -d)
curl -sSk -o "$S/bundle.pem" https://gu-st.ru/content/Other/doc/russiantrustedca.pem
.venv/bin/python - "$S/bundle.pem" <<'PY'
import hashlib, re, ssl, sys
from pathlib import Path
EXPECTED = "D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31"
text = Path(sys.argv[1]).read_text()
for pem in re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S):
    der = ssl.PEM_cert_to_DER_cert(pem)
    if ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()) == EXPECTED:
        Path("jessica/certs").mkdir(exist_ok=True)
        Path("jessica/certs/russian_trusted_root_ca.pem").write_text(pem + "\n")
        print("корень записан")
        break
else:
    sys.exit("корня с эталонным отпечатком нет — остановиться и сообщить хозяйке")
PY
rm -r "$S"
```
Expected: `корень записан`.

- [ ] **Step 2: Синтетические фикстуры**

`tests/fixtures/tinvest/responses.json` — структура настоящих ответов (сняты на спайке 10.09.2026), данные выдуманные:

```json
{
  "GetAccounts": {
    "accounts": [
      {"id": "0000000001", "type": "ACCOUNT_TYPE_TINKOFF", "name": "Брокерский счёт", "status": "ACCOUNT_STATUS_OPEN"},
      {"id": "0000000002", "type": "ACCOUNT_TYPE_TINKOFF_IIS", "name": "ИИС", "status": "ACCOUNT_STATUS_OPEN"}
    ]
  },
  "GetPortfolio": {
    "positions": [
      {"instrumentUid": "uid-softline", "instrumentType": "bond", "quantity": {"units": "20", "nano": 0}},
      {"instrumentUid": "uid-ofz", "instrumentType": "bond", "quantity": {"units": "7", "nano": 0}},
      {"instrumentUid": "uid-alium", "instrumentType": "bond", "quantity": {"units": "3", "nano": 0}},
      {"instrumentUid": "uid-sfo", "instrumentType": "bond", "quantity": {"units": "12", "nano": 500000000}},
      {"instrumentUid": "uid-share", "instrumentType": "share", "quantity": {"units": "5", "nano": 0}},
      {"instrumentUid": "uid-rub", "instrumentType": "currency", "quantity": {"units": "1234", "nano": 560000000}}
    ]
  },
  "BondBy": {
    "uid-softline": {"instrument": {"isin": "RU000A10EA08", "ticker": "RU000A10EA08", "name": "Софтлайн 002Р-02", "assetUid": "asset-softline"}},
    "uid-ofz": {"instrument": {"isin": "RU000A10D517", "ticker": "SU26253RMFS3", "name": "ОФЗ 26253", "assetUid": "asset-ofz"}},
    "uid-alium": {"instrument": {"isin": "RU000A10FG84", "ticker": "RU000A10FG84", "name": "АЛИУМ01Р1", "assetUid": "asset-alium"}},
    "uid-sfo": {"instrument": {"isin": "RU000A10CFK2", "ticker": "RU000A10CFK2", "name": "Кредитный поток 2.0", "assetUid": "asset-sfo"}}
  },
  "GetAssetBy": {
    "asset-softline": {"asset": {"security": {"bond": {"borrowName": "ПАО \"Софтлайн\""}}, "brand": {"company": "СофтЛайн"}}},
    "asset-ofz": {"asset": {"security": {"bond": {"borrowName": "Министерство финансов Российской Федерации"}}, "brand": {"company": "Минфин России"}}},
    "asset-alium": {"asset": {"security": {"bond": {"borrowName": ""}}, "brand": {"company": "Алиум"}}},
    "asset-sfo": {"asset": {"security": {"bond": {"borrowName": "ООО \"СФО ТБ-4\""}}, "brand": {"company": "Т-Банк"}}}
  }
}
```

- [ ] **Step 3: Написать падающие тесты**

`tests/test_certs.py`:

```python
import hashlib
import ssl
import unittest

from jessica.sources import tinvest

EXPECTED = "D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31"


class CertificateTest(unittest.TestCase):
    def test_root_is_the_ministry_root(self):
        der = ssl.PEM_cert_to_DER_cert(tinvest.CA_BUNDLE.read_text())
        self.assertEqual(":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()), EXPECTED)


if __name__ == "__main__":
    unittest.main()
```

`tests/test_tinvest.py`:

```python
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

import requests

from jessica.sources import tinvest

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "tinvest" / "responses.json").read_text(encoding="utf-8")
)
TOKEN = "t.SECRET-TEST-TOKEN"


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeApi:
    """Подменяет requests.post. failures: метод → список исходов по порядку
    (код HTTP или исключение); когда список кончается, отдаётся фикстура."""

    def __init__(self, fixtures=None, failures=None):
        self.fixtures = fixtures or FIXTURES
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls = []

    def __call__(self, url, json=None, headers=None, timeout=None, verify=None):
        method = url[len(tinvest.API):]
        self.calls.append({"method": method, "payload": json, "headers": headers, "verify": verify})
        queue = self.failures.get(method)
        if queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(outcome, {})
        name = method.split("/")[1]
        data = self.fixtures[name]
        if name in ("BondBy", "GetAssetBy"):
            data = data[json["id"]]
        return FakeResponse(200, data)

    def count(self, name):
        return sum(1 for call in self.calls if call["method"].endswith("/" + name))


class FetchBondsTest(unittest.TestCase):
    def fetch(self, api, **kwargs):
        with mock.patch.object(tinvest.requests, "post", api), mock.patch.object(tinvest, "PAUSE", 0):
            return tinvest.fetch_bonds(TOKEN, **kwargs)

    def test_only_bonds_with_issuer_and_code(self):
        bonds = {b["isin"]: b for b in self.fetch(FakeApi())}
        self.assertEqual(set(bonds), {"RU000A10EA08", "RU000A10D517", "RU000A10FG84", "RU000A10CFK2"})
        self.assertEqual(
            bonds["RU000A10EA08"],
            {"uid": "uid-softline", "isin": "RU000A10EA08", "name": "Софтлайн 002Р-02",
             "issuer": 'ПАО "Софтлайн"', "code": "", "quantity": 20.0},
        )
        self.assertEqual(bonds["RU000A10D517"]["code"], "SU26253RMFS3")
        self.assertEqual(bonds["RU000A10FG84"]["issuer"], "Алиум")  # borrowName пуст → brand.company
        self.assertEqual(bonds["RU000A10CFK2"]["quantity"], 12.5)  # units + nano

    def test_broker_account_is_found_and_iis_ignored(self):
        api = FakeApi()
        self.fetch(api)
        portfolio = next(c for c in api.calls if c["method"].endswith("/GetPortfolio"))
        self.assertEqual(portfolio["payload"], {"accountId": "0000000001"})

    def test_explicit_account_skips_get_accounts(self):
        api = FakeApi()
        self.fetch(api, account="0000000009")
        self.assertEqual(api.count("GetAccounts"), 0)

    def test_two_broker_accounts_without_account_is_none(self):
        fixtures = copy.deepcopy(FIXTURES)
        fixtures["GetAccounts"]["accounts"][1]["type"] = "ACCOUNT_TYPE_TINKOFF"
        self.assertIsNone(self.fetch(FakeApi(fixtures)))

    def test_known_bonds_are_not_described_again(self):
        known = {
            "uid-softline": {"isin": "RU000A10EA08", "name": "Софтлайн 002Р-02", "issuer": 'ПАО "Софтлайн"', "code": ""},
            "uid-ofz": {"isin": "RU000A10D517", "name": "ОФЗ 26253", "issuer": "Минфин", "code": "SU26253RMFS3"},
        }
        api = FakeApi()
        bonds = self.fetch(api, known=known)
        self.assertEqual(len(bonds), 4)
        self.assertEqual(api.count("BondBy"), 2)
        self.assertEqual(api.count("GetAssetBy"), 2)

    def test_incomplete_known_entry_is_described_again(self):
        api = FakeApi()
        self.fetch(api, known={"uid-softline": {"isin": "RU000A10EA08"}})
        self.assertEqual(api.count("BondBy"), 4)

    def test_failure_on_asset_step_gives_none_not_partial(self):
        api = FakeApi(failures={"InstrumentsService/GetAssetBy": [500] * tinvest.ATTEMPTS})
        self.assertIsNone(self.fetch(api))

    def test_connection_error_is_retried(self):
        api = FakeApi(failures={"UsersService/GetAccounts": [requests.ConnectionError("vpn")]})
        self.assertEqual(len(self.fetch(api)), 4)
        self.assertEqual(api.count("GetAccounts"), 2)

    def test_client_error_is_not_retried(self):
        api = FakeApi(failures={"UsersService/GetAccounts": [401]})
        self.assertIsNone(self.fetch(api))
        self.assertEqual(api.count("GetAccounts"), 1)

    def test_no_token_means_no_requests(self):
        api = FakeApi()
        with mock.patch.object(tinvest.requests, "post", api):
            self.assertIsNone(tinvest.fetch_bonds(""))
        self.assertEqual(api.calls, [])

    def test_certificate_is_pinned_for_every_request(self):
        api = FakeApi()
        self.fetch(api)
        self.assertEqual({c["verify"] for c in api.calls}, {str(tinvest.CA_BUNDLE)})
        self.assertEqual({c["headers"]["Authorization"] for c in api.calls}, {f"Bearer {TOKEN}"})

    def test_token_never_reaches_the_log(self):
        api = FakeApi(failures={"UsersService/GetAccounts": [401]})
        with self.assertLogs("jessica.sources.tinvest", level="WARNING") as logs:
            self.fetch(api)
        self.assertNotIn(TOKEN, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Убедиться, что тесты падают**

Run: `.venv/bin/python -m unittest tests.test_tinvest tests.test_certs -v`
Expected: `ImportError: cannot import name 'tinvest'`.

- [ ] **Step 5: Реализация**

`jessica/sources/tinvest.py`:

```python
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
    issuer = (
        asset.get("security", {}).get("bond", {}).get("borrowName")
        or asset.get("brand", {}).get("company")
        or ""
    )
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
```

- [ ] **Step 6: Прогнать тесты**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: все OK (в `test_tinvest` 12 тестов, в `test_certs` 1).

- [ ] **Step 7: Commit**

```bash
git add jessica/certs/russian_trusted_root_ca.pem jessica/sources/tinvest.py tests/fixtures/tinvest/responses.json tests/test_tinvest.py tests/test_certs.py
git commit -m "$(cat <<'EOF'
Источник T-Invest API: облигации брокерского счёта

fetch_bonds отдаёт облигации счёта с эмитентом и биржевым кодом, а при
любой ошибке — None, никогда не половину портфеля. Сертификат Минцифры
лежит в jessica/certs и передаётся в verify только здесь. Известные
бумаги не запрашиваются повторно. Пока нигде не подключён.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JPx62kAay6GDwmCREn7uU4
EOF
)"
```

---

### Task 4: `holdings.collect()` — источник по портфелю, кэш бумаг, `stale`

**Files:**
- Modify: `jessica/holdings.py` (импорты; новые `_read_json`, `_collect_tinvest`; функция `collect`)
- Create: `tests/test_holdings.py`

`_parse_tbank` в этой задаче **не трогать** (Global Constraints).

**Interfaces:**
- Consumes: `tinvest.fetch_bonds(...)`, `tinvest.FIELDS`, `Config.tinvest_token`, `Config.tinvest_account`, `Portfolio.source` (задачи 2–3).
- Produces:
  - `holdings.collect(config) -> dict` — формат прежний; у блока `tinvest`: `source_file: None`, `format: "tinvest-api"`, `synced: str | None`, `stale: bool`.
  - Файл `state/tinvest_instruments.json`: `{uid: {isin, name, issuer, code}}`.
  - `holdings.INSTRUMENTS = "tinvest_instruments.json"`.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_holdings.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jessica import holdings
from jessica.config import Config

RAW = {
    "tinvest": {"token": "t.TEST"},
    "portfolios": {
        "tinkoff": {"title": "Т-Инвестиции", "source": "tinvest"},
        "alpha": {"title": "Альфа", "reports_dir": "alpha/reports"},
    },
}
BONDS = [
    {"uid": "uid-softline", "isin": "RU000A10EA08", "name": "Софтлайн 002Р-02",
     "issuer": 'ПАО "Софтлайн"', "code": "", "quantity": 20.0},
    {"uid": "uid-ofz", "isin": "RU000A10D517", "name": "ОФЗ 26253",
     "issuer": "Министерство финансов Российской Федерации", "code": "SU26253RMFS3", "quantity": 7.0},
]
PREVIOUS_BLOCK = {
    "title": "Т-Инвестиции", "source_file": None, "format": "tinvest-api",
    "synced": "2026-09-01T10:00:00", "stale": False,
    "positions": [{"isin": "RU000A10C8C0", "quantity": 30.0, "portfolio": "tinkoff",
                   "name": "РЖД", "kind": "bond", "issuer": "", "code": ""}],
    "skipped": [],
}


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "alpha" / "reports").mkdir(parents=True)
        self.config = Config(RAW, self.root)
        self.state = self.root / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, bonds):
        with mock.patch.object(holdings.tinvest, "fetch_bonds", return_value=bonds) as fetch:
            return holdings.collect(self.config), fetch

    def write(self, name, data):
        self.state.mkdir(exist_ok=True)
        (self.state / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_fresh_block_from_api(self):
        data, _ = self.collect(BONDS)
        block = data["portfolios"]["tinkoff"]
        self.assertEqual(block["format"], "tinvest-api")
        self.assertIsNone(block["source_file"])
        self.assertFalse(block["stale"])
        self.assertTrue(block["synced"])
        positions = {p["isin"]: p for p in block["positions"]}
        self.assertEqual(positions["RU000A10EA08"]["kind"], "bond")
        self.assertEqual(positions["RU000A10EA08"]["issuer"], 'ПАО "Софтлайн"')
        self.assertEqual(positions["RU000A10D517"]["code"], "SU26253RMFS3")
        self.assertNotIn("uid", positions["RU000A10EA08"])

    def test_reports_portfolio_is_untouched(self):
        data, _ = self.collect(BONDS)
        self.assertNotIn("stale", data["portfolios"]["alpha"])
        self.assertNotIn("synced", data["portfolios"]["alpha"])

    def test_cache_is_written_after_success(self):
        self.collect(BONDS)
        cache = json.loads((self.state / holdings.INSTRUMENTS).read_text(encoding="utf-8"))
        self.assertEqual(set(cache), {"uid-softline", "uid-ofz"})
        self.assertEqual(cache["uid-ofz"], {"isin": "RU000A10D517", "name": "ОФЗ 26253",
                                            "issuer": "Министерство финансов Российской Федерации",
                                            "code": "SU26253RMFS3"})

    def test_cache_is_passed_as_known(self):
        cached = {"uid-softline": {"isin": "RU000A10EA08", "name": "С", "issuer": "И", "code": ""}}
        self.write(holdings.INSTRUMENTS, cached)
        _, fetch = self.collect(BONDS)
        fetch.assert_called_once_with("t.TEST", account=None, known=cached)

    def test_failure_keeps_previous_block_and_cache(self):
        self.write("holdings.json", {"updated": "x", "portfolios": {"tinkoff": PREVIOUS_BLOCK}})
        cached = {"uid-x": {"isin": "RU000A10C8C0", "name": "РЖД", "issuer": "", "code": ""}}
        self.write(holdings.INSTRUMENTS, cached)
        data, _ = self.collect(None)
        block = data["portfolios"]["tinkoff"]
        self.assertTrue(block["stale"])
        self.assertEqual(block["synced"], "2026-09-01T10:00:00")
        self.assertEqual(block["positions"], PREVIOUS_BLOCK["positions"])
        cache = json.loads((self.state / holdings.INSTRUMENTS).read_text(encoding="utf-8"))
        self.assertEqual(cache, cached)

    def test_first_run_failure_gives_empty_stale_block(self):
        data, _ = self.collect(None)
        block = data["portfolios"]["tinkoff"]
        self.assertTrue(block["stale"])
        self.assertIsNone(block["synced"])
        self.assertEqual(block["positions"], [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/python -m unittest tests.test_holdings -v`
Expected: `AttributeError: module 'jessica.holdings' has no attribute 'tinvest'`.

- [ ] **Step 3: Реализация**

В `jessica/holdings.py` после строки `from .config import Config, Portfolio` добавить:

```python
from .sources import tinvest
```

После `READABLE = {...}` добавить:

```python
# Кэш сведений о бумагах из T-Invest API: ISIN и эмитент выпуска не меняются,
# а каждый лишний запрос — лишний шанс попасть на обрыв VPN.
INSTRUMENTS = "tinvest_instruments.json"
```

Заменить функцию `collect` целиком на:

```python
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
```

- [ ] **Step 4: Прогнать тесты и боевой импорт на старом конфиге**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: все OK.

Run: `cp ~/Documents/Tracking/Invest/state/holdings.json /tmp/holdings.before.json && .venv/bin/python -m jessica import > /dev/null && diff <(python3 -c "import json;d=json.load(open('/tmp/holdings.before.json'));d.pop('updated');print(json.dumps(d,sort_keys=True,indent=1))") <(python3 -c "import json,pathlib;d=json.load(open(pathlib.Path.home()/'Documents/Tracking/Invest/state/holdings.json'));d.pop('updated');print(json.dumps(d,sort_keys=True,indent=1))") && echo "состав не изменился"`
Expected: `состав не изменился` — конфиг хозяйки ещё на выгрузках.

- [ ] **Step 5: Commit**

```bash
git add jessica/holdings.py tests/test_holdings.py
git commit -m "$(cat <<'EOF'
holdings: портфель из T-Invest API, кэш бумаг, прошлый состав при отказе

collect выбирает источник по source портфеля. Для tinvest сведения
о бумагах берутся из кэша, а при отказе API остаётся прошлый блок
с пометкой stale и прежним synced. Портфели на выгрузках не меняются.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JPx62kAay6GDwmCREn7uU4
EOF
)"
```

---

### Task 5: `cli` — предупреждения, повторы в `run`, проверка API в `doctor`

**Files:**
- Modify: `jessica/cli.py` (`cmd_import`, `cmd_refresh`, `cmd_doctor`, `cmd_run`; новые `refresh_due`, `rebuild_needed`, `stale_portfolios`, `_warn`)
- Create: `tests/test_run.py`

**Interfaces:**
- Consumes: `holdings.collect`, `news.silent` (задача 1), `tinvest.fetch_bonds`, `Config.refresh_retry_hours`, `Portfolio.source`.
- Produces:
  - `cli.refresh_due(state: dict, now: dt.datetime, retry_hours: int) -> bool`
  - `cli.rebuild_needed(state: dict, now: dt.datetime, before: dict, after: dict) -> bool`
  - `cli.stale_portfolios(holdings: dict) -> list[str]` — названия блоков со `stale`.
  - Новый ключ `last_run.json`: `refresh_attempt` (ISO-время последней попытки сбора).

- [ ] **Step 1: Написать падающие тесты**

`tests/test_run.py`:

```python
import datetime as dt
import unittest

from jessica.cli import rebuild_needed, refresh_due, stale_portfolios

NOW = dt.datetime(2026, 9, 11, 12, 0)


def holdings(quantity: float) -> dict:
    return {"portfolios": {"tinkoff": {"positions": [{"isin": "RU000A10C8C0", "quantity": quantity}]}}}


class RefreshDueTest(unittest.TestCase):
    def test_new_day_without_attempts(self):
        self.assertTrue(refresh_due({}, NOW, 3))

    def test_already_refreshed_today(self):
        self.assertFalse(refresh_due({"refreshed": "2026-09-11"}, NOW, 3))

    def test_failed_attempt_an_hour_ago_waits(self):
        state = {"refreshed": "2026-09-10", "refresh_attempt": "2026-09-11T11:00:00"}
        self.assertFalse(refresh_due(state, NOW, 3))

    def test_failed_attempt_three_hours_ago_retries(self):
        state = {"refreshed": "2026-09-10", "refresh_attempt": "2026-09-11T09:00:00"}
        self.assertTrue(refresh_due(state, NOW, 3))

    def test_attempt_yesterday_retries(self):
        state = {"refreshed": "2026-09-09", "refresh_attempt": "2026-09-10T23:30:00"}
        self.assertTrue(refresh_due(state, NOW, 3))


class RebuildNeededTest(unittest.TestCase):
    def test_first_attempt_of_the_day_rebuilds(self):
        self.assertTrue(rebuild_needed({}, NOW, holdings(30), holdings(30)))
        state = {"refresh_attempt": "2026-09-10T21:00:00"}
        self.assertTrue(rebuild_needed(state, NOW, holdings(30), holdings(30)))

    def test_retry_without_changes_skips_calendar(self):
        state = {"refresh_attempt": "2026-09-11T09:00:00"}
        self.assertFalse(rebuild_needed(state, NOW, holdings(30), holdings(30)))

    def test_retry_with_changes_rebuilds(self):
        state = {"refresh_attempt": "2026-09-11T09:00:00"}
        self.assertTrue(rebuild_needed(state, NOW, holdings(30), holdings(32)))


class StalePortfoliosTest(unittest.TestCase):
    def test_titles_of_stale_blocks(self):
        data = {"portfolios": {"a": {"title": "Т-Инвестиции", "stale": True}, "b": {"title": "Альфа"}}}
        self.assertEqual(stale_portfolios(data), ["Т-Инвестиции"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/python -m unittest tests.test_run -v`
Expected: `ImportError: cannot import name 'rebuild_needed'`.

- [ ] **Step 3: Реализация — функции и предупреждения**

В `jessica/cli.py` после функции `_log` добавить:

```python
def refresh_due(state: dict, now: dt.datetime, retry_hours: int) -> bool:
    """Пора ли собирать состав. Раз в сутки; если сегодняшняя попытка не удалась
    (API брокера не ответил), повтор — не раньше чем через retry_hours."""
    if state.get("refreshed") == now.date().isoformat():
        return False
    try:
        attempt = dt.datetime.fromisoformat(state["refresh_attempt"])
    except (KeyError, ValueError):
        return True
    return attempt.date() != now.date() or now - attempt >= dt.timedelta(hours=retry_hours)


def _composition(data: dict) -> set[tuple[str, str, float]]:
    return {
        (key, position["isin"], position["quantity"])
        for key, block in data.get("portfolios", {}).items()
        for position in block.get("positions", [])
    }


def rebuild_needed(state: dict, now: dt.datetime, before: dict, after: dict) -> bool:
    """Пересобирать ли календарь. Первая попытка дня — всегда: у smart-lab
    обновляются купоны. Повторная — только если состав изменился, иначе это
    сорок секунд и полсотни запросов впустую."""
    first_today = not str(state.get("refresh_attempt", "")).startswith(now.date().isoformat())
    return first_today or _composition(before) != _composition(after)


def stale_portfolios(data: dict) -> list[str]:
    return [block["title"] for block in data.get("portfolios", {}).values() if block.get("stale")]


def _warn(data: dict) -> None:
    """То, что нельзя пропустить: устаревший состав и бумаги, по которым
    новости молча не найдутся."""
    from .sources import news

    for block in data["portfolios"].values():
        if block.get("stale"):
            since = (block.get("synced") or "").replace("T", " ") or block.get("source_file") or "неизвестно когда"
            print(f"⚠ {block['title']}: API не ответил, состав от {since}")
        for position in block["positions"]:
            if news.silent(position["name"], position.get("issuer", "")):
                print(f"⚠ без маркеров: {position['name']} ({position['isin']}) — новости по бумаге не найдутся")
```

Заменить `cmd_import` и `cmd_refresh` на:

```python
def cmd_import(config: Config, args) -> int:
    data = holdings.collect(config)
    for block in data["portfolios"].values():
        origin = block["source_file"] or ("T-Invest API" if block["format"] == "tinvest-api" else "выгрузки нет")
        print(f"\n{block['title']} — {origin} [{block['format']}]")
        for position in block["positions"]:
            print(
                f"  {position['kind']:6} {position['isin']:14}"
                f" {position['quantity']:>8.0f} шт  {position['name'][:36]}"
            )
        print(f"  всего позиций: {len(block['positions'])}")
        for skipped in block["skipped"]:
            print(f"  ПРОПУЩЕНО: {skipped[:120]}")
    print()
    _warn(data)
    return 0


def cmd_refresh(config: Config, args) -> int:
    _warn(holdings.collect(config))
    schedule = cal.build(config)
    print(f"событий в календаре: {len(schedule['events'])}")
    if schedule["incomplete"]:
        print("нет данных по выпускам:", ", ".join(schedule["incomplete"]))
    return 0
```

- [ ] **Step 4: Реализация — `run` и `doctor`**

В `cmd_run` заменить

```python
    if state.get("refreshed") != today:
        holdings.collect(config)
        cal.build(config)
        state["refreshed"] = today
```

на

```python
    # Состав — раз в сутки. Если API брокера не ответил, прошлый состав остаётся,
    # а сбор повторяется через refresh_retry_hours: VPN то пускает к брокеру, то нет.
    if refresh_due(state, now, config.refresh_retry_hours):
        path = config.state_file("holdings.json")
        before = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        after = holdings.collect(config)
        if rebuild_needed(state, now, before, after):
            cal.build(config)
        state["refresh_attempt"] = now.isoformat(timespec="seconds")
        if not stale_portfolios(after):
            state["refreshed"] = today
```

В `cmd_doctor` заменить

```python
    for portfolio in config.portfolios:
        report = holdings.latest_report(portfolio)
        checks.append((f"выгрузка «{portfolio.title}»", report is not None, report.name if report else "нет файла"))
```

на

```python
    for portfolio in config.portfolios:
        if portfolio.source == "tinvest":
            from .sources import tinvest

            bonds = tinvest.fetch_bonds(config.tinvest_token, account=config.tinvest_account)
            checks.append((
                "T-Invest API",
                bonds is not None,
                f"{len(bonds)} облигаций" if bonds is not None else "API не ответил",
            ))
            continue
        report = holdings.latest_report(portfolio)
        checks.append((f"выгрузка «{portfolio.title}»", report is not None, report.name if report else "нет файла"))
```

- [ ] **Step 5: Прогнать тесты и боевые команды на старом конфиге**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: все OK.

Run: `.venv/bin/python -m jessica import | tail -3`
Expected: последняя строка — `⚠ без маркеров: Т-Технологии МКПАО ао (RU000A107UL4) — новости по бумаге не найдутся`; строк про `API не ответил` нет.

Run: `.venv/bin/python -m jessica doctor | grep -E "выгрузка|T-Invest"`
Expected: две строки `OK выгрузка «…»`, строки `T-Invest API` нет (конфиг ещё старый).

`jessica run` вручную **не запускать**: у него есть побочные эффекты (отправка сообщений). Логику `run` покрывают тесты; живая проверка — в задаче 7.

- [ ] **Step 6: Commit**

```bash
git add jessica/cli.py tests/test_run.py
git commit -m "$(cat <<'EOF'
cli: повторы сбора состава, предупреждения, проверка T-Invest API

run собирает состав раз в сутки, а если API брокера не ответил —
повторяет через refresh_retry_hours и пересобирает календарь, только
если состав изменился. import и refresh предупреждают об устаревшем
составе и о бумагах без маркеров, doctor проверяет API.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JPx62kAay6GDwmCREn7uU4
EOF
)"
```

---

### Task 6: запись настоящих ответов и сверка маркеров (контрольная точка с хозяйкой)

**Files:**
- Create: `scripts/record_tinvest_fixtures.py`

**Interfaces:**
- Consumes: `tinvest._post`, `tinvest.fetch_bonds`, `news.company_markers`, `Config.load()`.

- [ ] **Step 1: Скрипт**

`scripts/record_tinvest_fixtures.py`:

```python
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
        bonds = tinvest.fetch_bonds(config.tinvest_token, account=config.tinvest_account)
    finally:
        tinvest._post = original
    _save(out / "calls.json", calls)
    if bonds is None:
        print(f"API не ответил; удачных вызовов записано: {len(calls)} → {out}")
        return 1
    _save(out / "bonds.json", bonds)

    current = json.loads(config.state_file("holdings.json").read_text(encoding="utf-8"))
    old = {p["isin"]: p for p in current["portfolios"][key]["positions"] if p["kind"] == "bond"}
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
```

- [ ] **Step 2: Commit скрипта**

```bash
git add scripts/record_tinvest_fixtures.py
git commit -m "$(cat <<'EOF'
Скрипт записи ответов T-Invest API и сверки маркеров

Пишет настоящие ответы в папку с данными (не в репозиторий) и сравнивает
маркеры новостей по бумагам Т: из выгрузки против названий из API.
Токен берёт из конфига сам и никуда не выводит.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JPx62kAay6GDwmCREn7uU4
EOF
)"
```

- [ ] **Step 3: СТОП — действия хозяйки**

Остановиться и попросить хозяйку (агент в конфиг не заглядывает):
1. отозвать токен, выданный на спайк, и удалить `~/Documents/Tracking/Invest/tinkoff/tbank_mcp_token.txt`;
2. выпустить новый read-only токен и вписать в `config.yaml` блок верхнего уровня
   ```yaml
   tinvest:
     token: "t.…"
   ```
   больше ничего в конфиге не менять;
3. сказать, когда готово.

- [ ] **Step 4: Запись и сверка**

Run: `.venv/bin/python scripts/record_tinvest_fixtures.py`
Expected: `Записано в …`, 11 облигаций, `количество 30 → 32` у `RU000A10C8C0` (или текущее значение), для каждой строки с `≠` — блоки «сейчас / из API».
Если `API не ответил` — VPN режет узел; повторить позже или попросить хозяйку сменить сервер.

- [ ] **Step 5: Разбор расхождений**

Для каждой строки с `≠` или без маркеров решить: допустимо (например, новые маркеры шире, но не ловят общие слова) или нужна правка. Правки — только в `news.py` (`STOPWORDS`, `STOP_ABBR`, `NOISE`) с тестом в `tests/test_news.py` на этот случай, отдельным коммитом. Показать хозяйке таблицу расхождений и решения.

Сверить форму ответов в `calls.json` с синтетическими фикстурами (`tests/fixtures/tinvest/responses.json`): ключи `accounts`, `positions`, `instrumentUid`, `instrumentType`, `quantity.units/nano`, `instrument.isin/ticker/name/assetUid`, `asset.security.bond.borrowName`, `asset.brand.company`. Если что-то расходится — поправить `tinvest.py` и фикстуры, прогнать тесты, отдельный коммит.

Переход к задаче 7 — только с согласия хозяйки после этого разбора.

---

### Task 7: переключение на API и удаление парсера выгрузок Т-Банка

**Files:**
- Modify: `jessica/holdings.py` (модульный докстринг, `_parse_tbank`, `_detect`, `PARSERS`)
- Modify: `tests/test_holdings.py` (новый тест)

- [ ] **Step 1: СТОП — правка конфига хозяйкой**

Дать хозяйке фрагменты и дождаться подтверждения:

```yaml
portfolios:
  tinkoff:
    title: "Т-Инвестиции"
    source: tinvest            # вместо строки reports_dir
  alpha:
    title: "Альфа"
    reports_dir: "alpha/reports"
```

и **строку в существующий блок `schedule:`** (второй блок `schedule:` не создавать — PyYAML молча оставит последний, и пропадут `morning_hour` и `market_hour`):

```yaml
schedule:
  morning_hour: 9           # час рассылки событий дня, МСК
  market_hour: 10           # час сводки по рынку
  refresh_retry_hours: 3    # как часто повторять сбор состава, если API не ответил
```

(остальные строки блока `schedule`, если они есть, остаются как были).

- [ ] **Step 2: Живая проверка**

Run: `.venv/bin/python -c "from jessica.config import Config; c=Config.load(); print([(p.key,p.source) for p in c.portfolios], c.refresh_retry_hours, c.morning_hour, c.market_hour)"`
Expected: `[('tinkoff', 'tinvest'), ('alpha', 'reports')] 3 9 10`.

Run: `.venv/bin/python -m jessica import`
Expected: `Т-Инвестиции — T-Invest API [tinvest-api]`, 11 облигаций, у `RU000A10C8C0` — 32 (или текущее), из `⚠` — только `Т-Технологии`.

Run: `.venv/bin/python -m jessica refresh`
Expected: число событий того же порядка, что раньше; нет `⚠ … API не ответил`.

Run: `.venv/bin/python -m jessica doctor`
Expected: `OK  T-Invest API  11 облигаций`, `OK выгрузка «Альфа»`.

Run: `.venv/bin/python -m jessica check --dry-run` и `.venv/bin/python -m jessica news --dry-run`
Expected: те же уведомления, что до изменений, за вычетом ложных совпадений по «ГК».

Если API не ответил — повторить позже; Jessica при этом работает на прошлом составе.

- [ ] **Step 3: Тест на отсутствие парсера Т-Банка**

В `tests/test_holdings.py` добавить в конец класса `CollectTest`:

```python
    def test_tbank_report_parser_is_gone(self):
        # Портфель Т берётся из API; выгрузку Т-Банка больше некому разбирать точно.
        self.assertEqual(set(holdings.PARSERS), {"alpha", "generic"})
```

Run: `.venv/bin/python -m unittest tests.test_holdings -v`
Expected: FAIL, в `PARSERS` есть `tbank`.

- [ ] **Step 4: Удаление**

В `jessica/holdings.py`:
- удалить функцию `_parse_tbank` целиком;
- в `_detect` удалить строки
  ```python
          if _find_header(rows, "isin", "исходящий остаток"):
              return "tbank"
  ```
- заменить `PARSERS = {"tbank": _parse_tbank, "alpha": _parse_alpha, "generic": _parse_generic}` на `PARSERS = {"alpha": _parse_alpha, "generic": _parse_generic}`;
- заменить модульный докстринг на:

```python
"""Сборка состава портфелей в единый список позиций — state/holdings.json.

Источник задаётся в config.yaml для каждого портфеля:

* `source: tinvest` — T-Invest API брокера (см. sources/tinvest.py). Если API
  не ответил, остаётся прошлый состав с пометкой stale.
* `source: reports` (по умолчанию) — брокерская выгрузка: Jessica берёт самый
  свежий файл в папке портфеля и вытаскивает из него ISIN, количество и тип.

Выгрузка Альфы устроена так: количество и название в одной строке, а ISIN
отдельной строкой под ней; тип бумаги задан разделом таблицы («Акции»,
«Облигации»). Для неё точный парсер, для незнакомого файла — общий запасной.
Колонки ищутся по тексту заголовка, а не по номеру: так разбор переживёт
смещение столбцов при очередной правке формата.
"""
```

- [ ] **Step 5: Прогнать всё**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: все OK.

Run: `.venv/bin/python -m jessica import | head -3`
Expected: `Т-Инвестиции — T-Invest API [tinvest-api]`.

- [ ] **Step 6: Commit**

```bash
git add jessica/holdings.py tests/test_holdings.py
git commit -m "$(cat <<'EOF'
Портфель Т из API: парсер выгрузок Т-Банка больше не нужен

Конфиг переключён на source: tinvest, сверка маркеров и живая проверка
пройдены. Точный парсер выгрузки Т-Банка удалён; при необходимости он
возвращается из истории git.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JPx62kAay6GDwmCREn7uU4
EOF
)"
```

---

### Task 8: документация

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `docs/superpowers/specs/2026-09-10-tinvest-holdings-design.md` (строка статуса)

- [ ] **Step 1: CLAUDE.md**

1. В разделе «Команды» строки `jessica import` и `jessica refresh` заменить на:
   ```
   jessica import                     # собрать состав: Т из T-Invest API, Альфа из выгрузки → holdings.json
   jessica refresh                    # собрать состав и пересобрать календарь → schedule.json (~40 c, 48 HTTP-запросов)
   ```
2. В «Поток данных» первую строку схемы заменить на две:
   ```
   T-Invest API → sources/tinvest.py ┐
   выгрузки Альфы ───────────────────┴→ holdings.py → state/holdings.json   (позиции: ISIN, количество, тип, эмитент)
   ```
   и под схемой добавить абзац:
   > Состав Т собирается раз в сутки в `run`; если API брокера не ответил, в `holdings.json` остаётся прошлый блок с `stale: true`, а `run` повторяет сбор раз в `schedule.refresh_retry_hours` (3 ч) и пересобирает календарь, только если состав изменился. Сведения о бумагах (ISIN, эмитент, код) кэшируются в `state/tinvest_instruments.json` — обычный сбор обходится двумя запросами.
3. В «Сетевые ограничения» пункт про `invest-public-api.tinkoff.ru` заменить на:
   > - `invest-public-api.tbank.ru` — **открыт**, но цепочка сертификата от Russian Trusted Root CA (Минцифры). Корень лежит в `jessica/certs/` (SHA-256 `D2:6D:2D:…:CF:31`, полный — в спеке) и передаётся в `verify=` только в `sources/tinvest.py`; в системное хранилище не ставится. Режется по IP выходного узла VPN так же, как ISS. Токен read-only, живёт в `config.yaml`, блок `tinvest`.
4. Раздел «Брокерские выгрузки: подводные камни»: первый абзац заменить на
   > Состав Альфы приходит **только** из выгрузок, руками не заносится; состав Т — из T-Invest API. В `holdings.py` точный парсер Альфы и общий запасной.
   и удалить пункт **Т-Банк** (4 строки).
5. В «Правила уведомлений» после абзаца про два условия добавить:
   > Маркеры компании строятся из `name` и `issuer` позиции (`news.company_markers`). У Т они приходят из API: эмитент — `borrowName`, а без него — `brand.company`. `ГК` в `STOP_ABBR`, `кредитный`/`поток` в `STOPWORDS` — иначе ложные совпадения. `import` печатает `⚠ без маркеров` для бумаг, по которым новости не найдутся (кроме госбумаг и СФО).
6. «Состояние»: строку `Тестов нет (\`tests/\` пуста), …` заменить на
   > Тесты — `unittest`, без сети: `.venv/bin/python -m unittest discover -s tests -v`. Фикстуры T-Invest в `tests/fixtures/` синтетические; настоящие ответы API — только в `~/Documents/Tracking/Invest/tinvest-recordings/` (скрипт `scripts/record_tinvest_fixtures.py`). `pdfplumber` не установлен — ветка разбора PDF в `holdings.py` сработает только после `pip install pdfplumber`.

   и `Git инициализирован, удалённого репозитория нет.` заменить на `Удалённый репозиторий — GitHub (origin).`
7. В конец раздела «Состояние» добавить:
   > **Секреты агенту не показываются.** Claude Code запрещено читать `config.yaml` и файлы с токенами (`deny` в `~/.claude/settings.json`). Токены читает только код Jessica при запуске; проверка — через вывод `jessica doctor`. Правки конфига делает хозяйка.

- [ ] **Step 2: README.md**

1. В дереве папок строку `├── tinkoff/reports/       брокерские выгрузки Т-Инвестиций (xlsx)` удалить, в блок `state/` добавить `│   ├── tinvest_instruments.json  кэш сведений о бумагах из T-Invest API`.
2. В образце `config.yaml` после блока `telegram:` добавить
   ```yaml
   tinvest:
     token: "read-only токен T-Invest API"
   ```
   блок `tinkoff` в `portfolios` заменить на
   ```yaml
     tinkoff:
       title: "Т-Инвестиции"
       source: tinvest
   ```
   в `schedule:` добавить строку `  refresh_retry_hours: 3   # повтор сбора состава, если API брокера не ответил`.
3. После образца добавить абзац «Токен T-Invest»: выпустить в [настройках Т-Инвестиций](https://www.tbank.ru/invest/settings/) токен уровня **только чтение**, вписать в `config.yaml`; протухает через 3 месяца без использования; отзывается там же, «Список токенов → Удалить доступ».
4. В таблице команд: `jessica import` — «собрать состав → `holdings.json`», `jessica refresh` — «собрать состав и пересобрать календарь → `schedule.json`».
5. В схеме «Как это устроено» первую строку заменить так же, как в CLAUDE.md.
6. В таблице источников строку `invest-public-api.tinkoff.ru | — | таймаут, не используется` заменить на `invest-public-api.tbank.ru | состав портфеля Т | рабочий; сертификат Минцифры; режется по IP узла VPN, как ISS`; строку `iss.moex.com` — на `iss.moex.com | значение IMOEX2 | режется по IP узла VPN; за ним Alor и smart-lab`; добавить строку `api.alor.ru | IMOEX2, если ISS молчит | рабочий, без ключа`.

- [ ] **Step 3: Статус спеки**

В спеке строку статуса заменить на `Дата: 10 сентября 2026. Статус: реализовано.`

- [ ] **Step 4: Проверка**

Run: `grep -n "tinkoff.ru\|Тестов нет\|tinkoff/reports" CLAUDE.md README.md`
Expected: пусто.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md docs/superpowers/specs/2026-09-10-tinvest-holdings-design.md
git commit -m "$(cat <<'EOF'
Документация: портфель Т из T-Invest API

CLAUDE.md и README описывают новый источник состава, сертификат
Минцифры, повторы сбора, маркеры новостей, тесты и правило, что
агент не читает конфиг с секретами.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JPx62kAay6GDwmCREn7uU4
EOF
)"
```
