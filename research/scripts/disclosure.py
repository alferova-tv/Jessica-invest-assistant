"""Сообщения «Неисполнение обязательств» с сервера раскрытия Интерфакса.

Источник дня ноль из первоисточника: в сообщении эмитента есть плановая
дата платежа, выпуск и отметка «технический дефолт» или «дефолт».

e-disclosure закрыт антиботом, иногда с капчей. Капчу проходит человек
в открытом окне браузера; скрипт её не решает, а ждёт, пока пройдут.
Профиль браузера с отметкой о проверке лежит рядом с сырыми данными.

    .venv/bin/python research/scripts/disclosure.py list    # список сообщений по годам
    .venv/bin/python research/scripts/disclosure.py texts   # тексты сообщений наших эмитентов
    .venv/bin/python research/scripts/disclosure.py parse   # таблица disclosure_defaults.csv
    .venv/bin/python research/scripts/disclosure.py cid-texts    # по тексту на компанию без ИНН
    .venv/bin/python research/scripts/disclosure.py extra-texts  # тексты эмитентов вне «Д» и ранние у «Д»
    .venv/bin/python research/scripts/disclosure.py extra        # эпизоды техдефолтов вне «Д»
    .venv/bin/python research/scripts/disclosure.py cures-texts  # сообщения о выплате после неплатежей
    .venv/bin/python research/scripts/disclosure.py cures        # даты выплат и k → cures.csv
    .venv/bin/python research/scripts/disclosure.py restruct-list  # решения ОСВО, новации, списания субордов
    .venv/bin/python research/scripts/disclosure.py restruct-texts # их тексты
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import pathlib
import random
import re
import sys
import time
import urllib.parse
from contextlib import contextmanager

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from jessica.browser import STEALTH, UA  # noqa: E402

DATA = pathlib.Path.home() / "Documents/Tracking/Invest/research"
RAW = DATA / "raw/edisclosure"
OUT = DATA / "out"
SITE = "https://www.e-disclosure.ru"

# «Неисполнение обязательств эмитента перед владельцами его эмиссионных
# ценных бумаг» в справочнике /api/data/sevent-types.
EVENT_TYPE = 178
FIRST_YEAR = 2014
# Выдача поиска обрезается на 1200 записях; год столько не набирает
# (2025 — 339), но если наберёт, год делится пополам.
MAX_SHOWN = 1200
# Окно вокруг перевода в «Д»: цепочка техдефолтов у РКК тянулась 15 месяцев.
WINDOW_BEFORE = 450
WINDOW_AFTER = 30


@contextmanager
def session():
    """Окно браузера на странице поиска; ждёт, пока человек пройдёт капчу."""
    from playwright.sync_api import sync_playwright

    RAW.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(RAW / "profile"),
            headless=False,
            user_agent=UA,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(STEALTH)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()
        _open(page, f"{SITE}/poisk-po-soobshheniyam", "#sEventSearchForm__button-search")
        try:
            yield page
        finally:
            ctx.close()


def _open(page, url: str, ready: str) -> None:
    """Открыть страницу и дождаться селектора `ready`, пропуская человека к капче."""
    # VPN изредка рвёт соединение: разовый обрыв — повод повторить, а не упасть.
    for attempt in range(5):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            break
        except Exception as exc:  # noqa: BLE001 — Playwright бросает свои типы
            if attempt == 4:
                raise
            print(f"  обрыв ({str(exc)[:60]}), повтор через 20 с", flush=True)
            time.sleep(20)
    started = time.time()
    warned = False
    while time.time() - started < 1800:
        try:
            if page.locator(ready).count():
                return
        except Exception:  # noqa: BLE001 — страница на редиректе антибота
            pass
        if not warned and time.time() - started > 20:
            print("  ⚠ похоже на капчу — пройдите её в окне браузера", flush=True)
            warned = True
        time.sleep(3)
    raise SystemExit(f"не дождалась страницы {url}: капча не пройдена за 30 минут")


def _search(page, start: dt.date, finish: dt.date, types: str = str(EVENT_TYPE)) -> dict:
    form = {
        "eventTypeTerm": "", "radView": "0", "eventTypeCheckboxGroup": types,
        "dateStart": start.strftime("%d.%m.%Y"), "dateFinish": finish.strftime("%d.%m.%Y"),
        "textfieldEvent": "", "radReg": "FederalDistricts", "districtsCheckboxGroup": "-1",
        "regionsCheckboxGroup": "-1", "branchesCheckboxGroup": "-1", "textfieldCompany": "",
        "lastPageSize": "2147483647", "lastPageNumber": "1", "query": "", "queryEvent": "",
    }
    body = urllib.parse.urlencode(form)
    text = page.evaluate(
        """body => fetch('/api/search/sevents', {method: 'POST', body,
             headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                       'X-Requested-With': 'XMLHttpRequest'}}).then(r => r.text())""",
        body,
    )
    return json.loads(text)


def _pause() -> None:
    time.sleep(random.uniform(4, 8))


def cmd_list(types: str = str(EVENT_TYPE), prefix: str = "sevents") -> None:
    """Все сообщения типа 178 (или `types`) с 2014 года, по году на запрос."""
    today = dt.date.today()
    with session() as page:
        for year in range(FIRST_YEAR, today.year + 1):
            path = RAW / f"{prefix}_{year}.json"
            if path.exists() and year < today.year:
                continue
            periods = [(dt.date(year, 1, 1), min(dt.date(year, 12, 31), today))]
            found = []
            while periods:
                start, finish = periods.pop()
                reply = _search(page, start, finish, types)
                if reply["allFoundEvents"] > MAX_SHOWN:
                    middle = start + (finish - start) / 2
                    periods += [(start, middle), (middle + dt.timedelta(days=1), finish)]
                    continue
                found += reply["foundEventsList"]
                _pause()
            path.write_text(json.dumps(found, ensure_ascii=False))
            print(f"  {year}: {len(found)} сообщений", flush=True)


# Реструктуризация без неплатежа: решения общего собрания владельцев
# облигаций (253), новация или отступное (311), списание субординированных
# облигаций банка и отказ от их купона (338, 339). Номера — из справочника
# /api/data/sevent-types (raw/edisclosure/sevent_types.json).
# Несколько типов через запятую сайт не принимает (пустой ответ),
# поэтому по запросу на тип.
RESTRUCT_TYPES = (253, 311, 338, 339)


def cmd_restruct_list() -> None:
    """Сообщения о решениях ОСВО, новациях и списаниях с 2014 года."""
    for event_type in RESTRUCT_TYPES:
        print(f"тип {event_type}", flush=True)
        cmd_list(str(event_type), f"restruct{event_type}")


def _restruct_messages() -> list[dict]:
    found = []
    for event_type in RESTRUCT_TYPES:
        for path in sorted(RAW.glob(f"restruct{event_type}_*.json")):
            found += [dict(m, type=event_type) for m in json.loads(path.read_text())]
    return found


def cmd_restruct_texts(limit: str = "") -> None:
    """Тексты всех сообщений `restruct-list`: эмитента ищем потом по ИНН в тексте,
    так как в выдаче название сокращено («ПАО "ДВМП"», «ООО "СЖИ"»)."""
    _download([m["pseudoGUID"] for m in _restruct_messages()], limit)


# --- сопоставление с эмитентами исследования ---------------------------------

FORMS = (r"\b(ООО|АО|ПАО|НАО|ОАО|ЗАО|МФК|КБ|АКБ|Общество с ограниченной ответственностью"
         r"|Непубличное акционерное общество|Публичное акционерное общество"
         r"|Акционерное общество|Открытое акционерное общество|Закрытое акционерное общество"
         r"|Микрофинансовая компания)\b")


def _key(name: str) -> str:
    name = re.sub(FORMS, " ", name, flags=re.I)
    return re.sub(r"[^а-яёa-z0-9]+", "", name.lower())


def _messages() -> list[dict]:
    rows = []
    for path in sorted(RAW.glob("sevents_*.json")):
        rows += json.loads(path.read_text())
    return rows


def _issuers() -> list[dict]:
    """События исследования с датой перевода, начиная с 2014 года."""
    with (OUT / "events_issuers.csv").open() as fh:
        issuers = {row["emitent_id"]: row for row in csv.DictReader(fh)}
    with (OUT / "day_zero.csv").open() as fh:
        rows = [row for row in csv.DictReader(fh) if row["day_zero"]]
    for row in rows:
        row["inn"] = issuers[row["emitent_id"]]["emitent_inn"]
        row["secids"] = issuers[row["emitent_id"]]["secids"]
    return rows


def _brand(name: str) -> str:
    """Имя в последних кавычках: «ИА «Кама»» и «Ипотечный агент «Кама»» сходятся на «Кама»."""
    quoted = re.findall(r"[«\"“]([^«»\"“”]+)", name)
    return _key(quoted[-1]) if quoted else _key(name)


def _tail(name: str) -> str:
    words = re.findall(r"[а-яёa-z0-9]+", (re.findall(r"[«\"“]([^«»\"“”]+)", name) or [name])[-1].lower())
    return words[-1] if words else ""


# Слова, которыми кончаются названия у многих: по ним одним не сопоставлять.
GENERIC = {"финанс", "компания", "капитал", "инвест", "банк", "групп", "холдинг", "лизинг",
           "недвижимость", "пао", "ао", "ооо", "плюс", "рус", "трейд", "девелопмент", "ресурс"}


def _same(ours: str, theirs: str) -> bool:
    """Похоже ли имя из выдачи на имя эмитента. Ложные совпадения отсекает ИНН в тексте."""
    a, b = _key(ours), _key(theirs)
    if not a or not b:
        return False
    if a == b or _brand(ours) == _brand(theirs):
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 8 and short in long_ and len(short) / len(long_) >= 0.6:
        return True
    tail = _tail(ours)
    return len(tail) >= 3 and tail not in GENERIC and tail == _tail(theirs)


def _matches(issuer: dict, messages: list[dict]) -> list[dict]:
    """Сообщения эмитента в окне вокруг перевода в «Д».

    В выдаче поиска нет ИНН, только краткое имя, поэтому кандидаты
    подбираются по имени, а ИНН сверяется потом по тексту сообщения.
    """
    transfer = dt.date.fromisoformat(issuer["transfer_date"])
    lo = transfer - dt.timedelta(days=WINDOW_BEFORE)
    hi = transfer + dt.timedelta(days=WINDOW_AFTER)
    return [
        msg for msg in messages
        if lo <= dt.date.fromisoformat(msg["eventDate"][:10]) <= hi
        and _same(issuer["emitent_title"], msg["companyName"])
    ]


def cmd_texts(limit: str = "") -> None:
    """Тексты сообщений наших эмитентов; уже скачанные не трогает."""
    messages = _messages()
    todo = []
    for issuer in _issuers():
        todo += [m["pseudoGUID"] for m in _matches(issuer, messages)]
    _download(todo, limit)


def _download(guids: list[str], limit: str = "") -> None:
    """Скачать тексты сообщений; уже скачанные не трогает."""
    todo = [guid for guid in dict.fromkeys(guids) if not (RAW / "events" / f"{guid}.html").exists()]
    print(f"скачать текстов: {len(todo)}", flush=True)
    if limit:
        todo = todo[: int(limit)]
    if not todo:
        return
    (RAW / "events").mkdir(exist_ok=True)
    with session() as page:
        for number, guid in enumerate(todo, 1):
            _open(page, f"{SITE}/portal/event.aspx?EventId={guid}", "#cont_wrap, .events-container, h4")
            (RAW / "events" / f"{guid}.html").write_text(page.content())
            if number % 10 == 0 or number == len(todo):
                print(f"  {number}/{len(todo)}", flush=True)
            _pause()


# --- разбор текстов ----------------------------------------------------------

DATE = r"(\d{2})\.(\d{2})\.(\d{4})"


def _text(path: pathlib.Path) -> str:
    raw = path.read_text()
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    raw = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "\n".join(re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines() if line.strip())


MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7,
          "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
# Старые сообщения пишут дату словами: «23 января 2017 года», бывает
# и с кавычками вокруг числа: «29» июля 2016 г.
ANY_DATE = (r"(\d{1,2})\.(\d{2})\.(\d{4})"
            r"|[«\"“]?(\d{1,2})[»\"”]?\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})")


def _first_date(fragment: str) -> str:
    match = re.search(ANY_DATE, fragment, flags=re.I)
    if not match:
        return ""
    if match.group(1):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    else:
        day, year = int(match.group(4)), int(match.group(6))
        word = match.group(5).lower()
        month = next(num for stem, num in sorted(MONTHS.items(), key=lambda kv: -len(kv[0])) if word.startswith(stem))
    return f"{year:04d}-{month:02d}-{day:02d}"


def _after(text: str, label: str) -> str:
    """Первая дата в пункте формы, начатом подписью `label`.

    Пункт тянется до следующего номера («2.5.»): в старых сообщениях
    значение стоит строкой ниже подписи.
    """
    match = re.search(label + r"[^\n]*", text, flags=re.I)
    if not match:
        return ""
    line = match.group(0)
    tail = line.split(":", 1)[1] if ":" in line else line
    rest = re.split(r"\n\s*\d\.\d+\.?\s", text[match.end():], maxsplit=1)[0]
    return _first_date(tail) or _first_date(rest[:600])


def _parse(text: str) -> dict:
    """Плановая дата, выпуск, вид обязательства и статус из текста сообщения."""
    body = text[text.find("2. Содержание сообщения"):] if "2. Содержание сообщения" in text else text
    due = _after(body, r"должн[оа] быть исполнен") or _after(body, r"(?:дату|дата) окончания[^\n]{0,60}?купонного периода")
    event = _after(text, r"Дата наступления (?:события|существенного факта)")
    inns = re.findall(r"ИНН[^\n\d]{0,40}(\d{10})", text)
    # Шаблон формы перечисляет оба исхода: «в том числе по его вине
    # (технический дефолт, дефолт): дефолт». Скобки шаблона убираются,
    # иначе любое сообщение выглядело бы техдефолтом.
    # Старая форма: «в том числе по его вине (дефолт):» — тоже шаблон.
    # Статус ищется только в теле сообщения: на страницах, скачанных
    # с октября 2026, сбоку лента новостей Интерфакса («первичный дефолт
    # в 2027 году…»), и по ней любое сообщение выглядело бы дефолтом.
    signed = body.find("3. Подпись")
    low = re.sub(r"\(технический дефолт,? (?:или )?дефолт\)|по его вине \(дефолт\)|\(частичн\w+ неисполнени\w+\)",
                 " ", (body[:signed] if signed > 0 else body).lower())
    if re.search(r"(?<!технический )(?<!технического )\bдефолт\b|на срок более 10|более десяти рабочих|по истечении 10", low):
        status = "дефолт"
    elif re.search(r"техническ\w* дефолт", low):
        status = "техдефолт"
    else:
        status = ""
    kinds = []
    for word, kind in (("купон", "купон"), ("амортиз", "амортизация"), ("частичн\\w* погашени", "амортизация"),
                       ("приобрет", "оферта"), ("оферт", "оферта"), ("досрочн\\w* погашени", "досрочное погашение"),
                       ("номинальной стоимости", "номинал"), ("погашени", "номинал")):
        if re.search(word, low) and kind not in kinds:
            kinds.append(kind)
    return {
        "event_date": event,
        "due_date": due,
        "inn": " ".join(dict.fromkeys(inns)),
        "isins": " ".join(dict.fromkeys(re.findall(r"RU000[A-Z0-9]{7}", text))),
        "kind": "+".join(kinds),
        "status": status,
        "partial": "частично" if re.search(r"частичн\w* неисполнени|исполнен\w* частично", low) else "",
    }


def _own(row: dict, issuer: dict) -> bool:
    """Сообщение точно этого эмитента: совпал ИНН или ISIN дефолтного выпуска.

    ISIN нужен для реорганизаций: Нэппи Клаб до перехода из ООО в АО
    подписывал сообщения прежним ИНН.
    """
    return issuer["inn"] in row["inn"].split() or bool(set(row["isins"].split()) & set(issuer["secids"].split()))


# Льготный срок — 10 рабочих дней, то есть 14–16 календарных, а на новогодних
# каникулах до 23 (от 30 декабря — около 22 января; прикидка, не расчёт по
# производственному календарю). Окно 25 дней его покрывает и не дотягивается до предыдущего ежемесячного купона
# (у Нэппи Клаба купоны 13.03 и 13.04, дефолт по второму).
GRACE_WINDOW = 25


def _summary(issuer: dict, own: list[dict]) -> dict:
    """День ноль и первый неплатёж по сообщениям эмитента.

    День ноль — плановая дата платежа, по которому эмитент первым сообщил
    о дефолте. В сообщении о дефолте «дата окончания срока» бывает концом
    льготного периода («с 27.11 по 10.12 — технический дефолт»), поэтому
    берётся самый ранний неплатёж по тому же выпуску в пределах льготного
    срока до неё. Если о дефолте эмитент не сообщал, дня ноль из раскрытия
    нет: остаётся только первый неплатёж, решение за человеком.
    """
    dated = []
    for r in own:
        if not r["due_date"]:
            continue
        lag = (dt.date.fromisoformat(r["list_date"]) - dt.date.fromisoformat(r["due_date"])).days
        # Срок позже публикации больше чем на неделю или раньше на 4 месяца —
        # опечатка в сообщении (РЖД-Развитие вокзалов: «25.07.2013» вместо 2014).
        if not -7 <= lag <= 120:
            continue
        # Повторное сообщение о том же платеже после льготного срока — это
        # сообщение о дефолте, даже без самого слова (МОНОПОЛИЯ, 04.12 → 18.12).
        if lag >= 10 and not r["status"]:
            r = {**r, "status": "дефолт"}
        dated.append(r)
    dated.sort(key=lambda r: (r["list_date"], r["due_date"]))
    defaults = [r for r in dated if r["status"] == "дефолт"]
    first = min((r["due_date"] for r in dated), default="")
    day_zero, source = "", {}
    if defaults:
        source = defaults[0]
        end = dt.date.fromisoformat(source["due_date"])
        isins = set(source["isins"].split())
        near = [
            r["due_date"] for r in dated
            if r["list_date"] <= source["list_date"]
            and (not isins or set(r["isins"].split()) & isins)
            and 0 <= (end - dt.date.fromisoformat(r["due_date"])).days <= GRACE_WINDOW
        ]
        day_zero = min(near + [source["due_date"]])
    elif dated:
        source = min(dated, key=lambda r: r["due_date"])
    # Без сообщения о дефолте берётся первый неплатёж в 70 днях до перевода:
    # старая форма (2015–2018) слово «дефолт» часто не писала вовсе.
    unpaid = ""
    if not day_zero and issuer["transfer_date"]:
        transfer = dt.date.fromisoformat(issuer["transfer_date"])
        inside = [r for r in dated if 0 <= (transfer - dt.date.fromisoformat(r["due_date"])).days <= 70]
        if inside:
            source = min(inside, key=lambda r: r["due_date"])
            unpaid = source["due_date"]
    return {
        "emitent_id": issuer["emitent_id"],
        "emitent_title": issuer["emitent_title"],
        "inn": issuer["inn"],
        "transfer_date": issuer["transfer_date"],
        "day_zero_current": issuer["day_zero"],
        "source_current": issuer["source"],
        "disclosure_day_zero": day_zero,
        # Когда техдефолт стал дефолтом: дата публикации первого сообщения
        # о дефолте. Нужна задаче «вылечится ли техдефолт» (HYPOTHESES.md).
        "disclosure_default_date": defaults[0]["list_date"] if defaults else "",
        "disclosure_unpaid": unpaid,
        "disclosure_first": first,
        "defaulted_isin": source.get("isins", ""),
        "kind": source.get("kind", ""),
        "messages": len(own),
        "url": source.get("url", ""),
    }


def cmd_parse() -> None:
    messages = _messages()
    rows, summary = [], []
    foreign = 0
    for issuer in _issuers():
        own = []
        for msg in _matches(issuer, messages):
            path = RAW / "events" / f"{msg['pseudoGUID']}.html"
            if not path.exists():
                continue
            row = {
                "emitent_id": issuer["emitent_id"],
                "emitent_title": issuer["emitent_title"],
                "company_name": msg["companyName"],
                "list_date": msg["eventDate"][:10],
                "corrected": msg["isCorrectedByAnotherEvent"],
                **_parse(_text(path)),
                "url": f"{SITE}/portal/event.aspx?EventId={msg['pseudoGUID']}",
            }
            if _own(row, issuer):
                own.append(row)
            else:
                foreign += 1
        rows += own
        if own:
            summary.append(_summary(issuer, own))
    rows.sort(key=lambda r: (r["emitent_title"], r["list_date"]))
    for name, table in (("disclosure_defaults.csv", rows), ("disclosure_day_zero.csv", summary)):
        with (OUT / name).open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
    print(f"сообщений эмитентов: {len(rows)} (чужих отброшено: {foreign}), эмитентов: {len(summary)}")
    print(f"с объявленным дефолтом: {sum(1 for r in summary if r['disclosure_day_zero'])}")


# --- техдефолты вне режима «Д» ------------------------------------------------
#
# Решение владелицы 2026-09-26: выборка расширяется эмитентами, у которых
# был неплатёж, но биржа в «Д» их не переводила (техдефолт погашен или
# дефолт не дошёл до перевода). Для них известен только первый неплатёж —
# это второе целевое событие исследования («первый техдефолт»).

# Госбумаги и бумаги ЦБ в исследование не входят.
NOT_CORPORATE = {"ofz_bond", "cb_bond", "subfederal_bond", "municipal_bond", "ifi_bond"}
# После 2022 года часть «неисполнений» — невыплата иностранным держателям
# из-за санкций и блокировок в расчётной инфраструктуре, а не нехватка денег.
SANCTIONS = re.compile(
    r"санкц|ограничительн\w* мер|недружественн|euroclear|евроклир|clearstream|клирстрим"
    r"|иностранн\w* (?:держател|депозитари|платёжн|платежн)|блокир\w* (?:средств|платеж)",
    re.I,
)
# Причина неплатежа (пункт «Причина неисполнения»). Техническая — деньги
# были, но не дошли: держатели не дали реквизиты (Акрон, Транснефть),
# ошибка в платёжке (КЛВЗ Кристалл), банк-корреспондент, НРД. Такой
# неплатёж не кредитное событие. Арест счетов — кредитное: платить нечем.
TECHNICAL = re.compile(
    r"техническ\w* (?:ошибк|причин|сложност|механизм|особенност|сбо|проблем)|неявк\w* владельц|реквизит"
    r"|информаци\w* о (?:лицах|владельц)|списк\w* владельцев|обработк\w* банком|корреспондент"
    r"|операционн\w* риск|поступил\w* на счет депозитария на след"
    r"|техническ\w* задержк|акцепт\w* специализированн\w* депозитари",
    re.I,
)
CREDIT = re.compile(
    r"(?:отсутств|недостат|нехватк|недостаточн)\w*[^.]{0,60}денежн|денежн\w* средств\w*[^.]{0,40}(?:отсутств|недостат)"
    r"|задержк\w* поступлени|заблокирован|блокировк\w* (?:расч|счет|счёт)|арест|обеспечительн\w* мер"
    r"|финансов\w* затруднени|(?:несвоевременн|недо)\w*\s?поступлени|приостановк\w* операций"
    r"|реструктуризаци|новаци",
    re.I,
)


def _cause(text: str) -> str:
    """Текст пункта «Причина неисполнения» — после подписи пункта до следующего."""
    flat = re.sub(r"\s+", " ", text)
    match = re.search(r"[Пп]ричина неисполнения[^:]*:", flat)
    if not match:
        return ""
    tail = flat[match.end():]
    return re.split(r"\s(?:2\.\d+\.?|3\. Подпись)\s", tail, maxsplit=1)[0][:400].strip()


def _cause_class(cause: str, default_declared: bool) -> str:
    if default_declared:
        return "кредитная"
    if TECHNICAL.search(cause):
        return "техническая"
    if CREDIT.search(cause):
        return "кредитная"
    return "неясно"


# Неплатежи одного эмитента с разрывом больше 200 дней — разные эпизоды.
# Не 180: полугодовые купоны идут через 181–184 дня, и неплатежи подряд
# по ним — один эпизод (ТТБ Инвест Продакшн: 08.11.2018, 09.05.2019).
EPISODE_GAP = 200


def _bond_emitents() -> dict[str, dict]:
    """Эмитенты корпоративных облигаций из справочника ISS (кэш `events.py universe`)."""
    found: dict[str, dict] = {}
    for path in sorted((DATA / "raw").glob("securities.json*")):
        block = json.loads(path.read_text())["securities"]
        for values in block["data"]:
            row = dict(zip(block["columns"], values))
            if not row["emitent_id"] or row["type"] in NOT_CORPORATE:
                continue
            item = found.setdefault(str(row["emitent_id"]), {
                "title": row["emitent_title"] or "", "inn": row["emitent_inn"] or "", "isins": set()})
            item["isins"].add(row["isin"] or row["secid"])
    return found


def _cid_inns() -> dict[int, str]:
    """companyID → ИНН эмитента по скачанным текстам.

    В выдаче поиска ИНН нет, только краткое имя, а по имени сходятся не
    все: Стройжилинвест записан как «ООО "СЖИ"». Номер компании в выдаче
    есть всегда, а ИНН — в первом пункте каждого текста.
    """
    ids = _company_ids()
    found: dict[int, dict[str, int]] = {}
    for path in (RAW / "events").glob("*.html"):
        if path.stem not in ids:
            continue
        inns = re.findall(r"ИНН[^\n\d]{0,40}(\d{10})", _text(path))
        if inns:
            counts = found.setdefault(ids[path.stem][0], {})
            counts[inns[0]] = counts.get(inns[0], 0) + 1
    return {cid: max(counts, key=counts.get) for cid, counts in found.items()}


def cmd_cid_texts(limit: str = "") -> None:
    """По одному тексту на компанию, у которой ИНН ещё неизвестен (последнее сообщение)."""
    known = _cid_inns()
    latest: dict[int, dict] = {}
    for msg in _messages():
        if msg["eventDate"][:10] < f"{FIRST_YEAR}-01-01" or msg["companyID"] in known:
            continue
        if msg["companyID"] not in latest or msg["eventDate"] > latest[msg["companyID"]]["eventDate"]:
            latest[msg["companyID"]] = msg
    print(f"компаний без ИНН: {len(latest)}", flush=True)
    _download([msg["pseudoGUID"] for msg in latest.values()], limit)


def _extra_candidates() -> tuple[list[tuple[dict, list[str]]], dict[str, dict]]:
    """Сообщения с 2014 года от эмитентов облигаций, которых `parse` не разбирает.

    Это эмитенты вне «Д» и ранние сообщения эмитентов «Д»: раньше окна
    `WINDOW_BEFORE` до перевода (Вейл Финанс не заплатил купон 14.03.2018,
    а в «Д» попал 21.09.2021). Компания узнаётся по ИНН из `_cid_inns`,
    а без него — по имени.
    """
    with (OUT / "events_issuers.csv").open() as fh:
        ours = {row["emitent_id"]: (dt.date.fromisoformat(row["transfer_date"])
                                    - dt.timedelta(days=WINDOW_BEFORE)).isoformat()
                for row in csv.DictReader(fh)}
    emitents = _bond_emitents()
    cid_inn = _cid_inns()
    by_inn: dict[str, list[str]] = {}
    for eid, item in emitents.items():
        if item["inn"]:
            by_inn.setdefault(item["inn"], []).append(eid)
    by_name: dict[str, list[str]] = {}
    found = []
    for msg in _messages():
        day = msg["eventDate"][:10]
        if day < f"{FIRST_YEAR}-01-01":
            continue
        name = msg["companyName"]
        if msg["companyID"] in cid_inn:
            matched = by_inn.get(cid_inn[msg["companyID"]], [])
        else:
            if name not in by_name:
                by_name[name] = [eid for eid, item in emitents.items() if _same(item["title"], name)]
            matched = by_name[name]
        ids = [eid for eid in matched if eid not in ours or day < ours[eid]]
        if ids and len(ids) == len(matched):
            found.append((msg, ids))
    return found, emitents


def cmd_extra_texts(limit: str = "") -> None:
    """Тексты сообщений эмитентов вне «Д»."""
    candidates, _ = _extra_candidates()
    _download([msg["pseudoGUID"] for msg, _ in candidates], limit)


def _manual_extra() -> dict[str, dict]:
    """Решения владелицы по отдельным эпизодам вне «Д»: ИНН → решение."""
    path = pathlib.Path(__file__).resolve().parents[1] / "manual" / "manual_techdefault.csv"
    if not path.exists():
        return {}
    with path.open() as fh:
        return {row["inn"]: row for row in csv.DictReader(fh)}


def _episode_day_zero(episode: list[dict]) -> str:
    """День ноль эпизода с объявленным дефолтом — по тому же правилу, что в `_summary`."""
    dated = sorted((r for r in episode if r["due_date"]), key=lambda r: (r["list_date"], r["due_date"]))
    defaults = [r for r in dated if r["status"] == "дефолт"]
    if not defaults:
        return ""
    source = defaults[0]
    end = dt.date.fromisoformat(source["due_date"])
    isins = set(source["isins"].split())
    near = [
        r["due_date"] for r in dated
        if r["list_date"] <= source["list_date"]
        and (not isins or set(r["isins"].split()) & isins)
        and 0 <= (end - dt.date.fromisoformat(r["due_date"])).days <= GRACE_WINDOW
    ]
    return min(near + [source["due_date"]])


def cmd_extra() -> None:
    """Эпизоды неплатежей эмитентов вне «Д» → techdefault_events.csv.

    Роль в исследовании (решение владелицы 2026-09-26): для цели «первый
    техдефолт» эпизод — положительный случай; для цели «дефолт» эмитент
    не участвует, если сам не объявил дефолт. Объявленный дефолт — факт,
    и такой эпизод считается дефолтом с днём ноль из сообщения, хотя
    перевода в «Д» не было.
    """
    manual = _manual_extra()
    candidates, emitents = _extra_candidates()
    with (OUT / "events_issuers.csv").open() as fh:
        ours = {row["emitent_id"] for row in csv.DictReader(fh)}
    rows = []
    for msg, ids in candidates:
        path = RAW / "events" / f"{msg['pseudoGUID']}.html"
        if not path.exists():
            continue
        text = _text(path)
        parsed = _parse(text)
        inns = parsed["inn"].split()
        isins = set(parsed["isins"].split())
        owner = next((eid for eid in ids if emitents[eid]["inn"] in inns), None) or next(
            (eid for eid in ids if isins & emitents[eid]["isins"]), None)
        if not owner:
            continue
        head = text[text.find("2.1"): text.find("2.1") + 600].lower()
        bond = bool(isins & emitents[owner]["isins"]) or ("облигац" in head and "акци" not in head[:120])
        rows.append({
            "emitent_id": owner,
            "emitent_title": emitents[owner]["title"],
            "inn": emitents[owner]["inn"],
            "list_date": msg["eventDate"][:10],
            **parsed,
            "bond": bond,
            "sanctions": bool(SANCTIONS.search(text)),
            "cause": _cause(text),
            "url": f"{SITE}/portal/event.aspx?EventId={msg['pseudoGUID']}",
        })
    rows.sort(key=lambda r: (r["emitent_id"], r["due_date"] or r["list_date"]))
    with (OUT / "techdefault_messages.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    episodes = []
    for eid in dict.fromkeys(r["emitent_id"] for r in rows):
        # Технические неплатежи в эпизоды не входят: копеечная невыплата
        # держателям без реквизитов (Мечел, 2017) не начинает кредитный эпизод.
        mine = [r for r in rows if r["emitent_id"] == eid and r["bond"]
                and (r["status"] == "дефолт" or _cause_class(r["cause"], False) != "техническая")]
        current: list[dict] = []
        for row in mine:
            day = row["due_date"] or row["list_date"]
            if current and (dt.date.fromisoformat(day)
                            - dt.date.fromisoformat(current[-1]["due_date"] or current[-1]["list_date"])).days > EPISODE_GAP:
                episodes.append(current)
                current = []
            current.append(row)
        if current:
            episodes.append(current)
    table = []
    for episode in episodes:
        first = episode[0]
        declared = any(r["status"] == "дефолт" for r in episode)
        classes = {_cause_class(r["cause"], declared) for r in episode}
        cause_class = next(c for c in ("кредитная", "неясно", "техническая") if c in classes)
        decision = (manual.get(first["inn"]) or {}).get("decision", "")
        if decision == "исключить":
            cause_class = "исключён вручную"
        elif decision:
            cause_class = decision
        # Дефолт эмитента «Д» уже записан по переводу, поэтому его ранний
        # эпизод — только техдефолт, даже с отметкой «дефолт» (решение владелицы 2026-10-21).
        day_zero = _episode_day_zero(episode) if declared and first["emitent_id"] not in ours else ""
        role = ("исключён" if cause_class not in ("кредитная",)
                else "дефолт" if day_zero else "только техдефолт")
        table.append({
            "emitent_id": first["emitent_id"],
            "emitent_title": first["emitent_title"],
            "inn": first["inn"],
            "role": role,
            "tech_first": first["due_date"] or first["list_date"],
            "day_zero": day_zero,
            "default_date": next((r["list_date"] for r in episode if r["status"] == "дефолт"), "") if day_zero else "",
            "date_from": "плановый платёж" if first["due_date"] else "дата сообщения",
            "last": episode[-1]["due_date"] or episode[-1]["list_date"],
            "messages": len(episode),
            "default_declared": declared,
            "cause_class": cause_class,
            "cause": first["cause"][:200],
            "sanctions": any(r["sanctions"] for r in episode),
            "isins": " ".join(dict.fromkeys(i for r in episode for i in r["isins"].split())),
            "url": first["url"],
        })
    # Неплатежи, которые эмитент не раскрыл на e-disclosure, известны только
    # по ручной проверке: строка `manual_techdefault.csv` с датой `tech_first`
    # (Фабрикс, 09.08.2026 — есть только в списке smart-lab).
    known = {(t["inn"], t["tech_first"]) for t in table}
    by_inn = {item["inn"]: (eid, item) for eid, item in emitents.items() if item["inn"]}
    for inn, row in manual.items():
        if not row.get("tech_first") or (inn, row["tech_first"]) in known or inn not in by_inn:
            continue
        eid, item = by_inn[inn]
        table.append({
            "emitent_id": eid,
            "emitent_title": item["title"],
            "inn": inn,
            "role": "только техдефолт" if row["decision"] == "кредитная" else "исключён",
            "tech_first": row["tech_first"],
            "day_zero": "",
            "default_date": "",
            "date_from": "ручная проверка",
            "last": row["tech_first"],
            "messages": 0,
            "default_declared": False,
            "cause_class": row["decision"],
            "cause": row["note"][:200],
            "sanctions": False,
            "isins": " ".join(sorted(item["isins"])),
            "url": "",
        })
    # Повторные эпизоды того же эмитента (ТелеХаус три года подряд не
    # платит один и тот же купон) — продолжение одной беды. Помечается
    # первый кредитный эпизод эмитента; повторные остаются для справки.
    seen: set[str] = set()
    for row in sorted(table, key=lambda r: r["tech_first"]):
        row["first_for_issuer"] = row["role"] != "исключён" and row["emitent_id"] not in seen
        if row["first_for_issuer"]:
            seen.add(row["emitent_id"])
    with (OUT / "techdefault_events.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    # Ручное решение «техническая» перекрывает статус «дефолт» в сообщении
    # (Петербургснаб, 2026: «дефолт» из-за ошибки в платёжке, оплачено назавтра).
    manual_technical = {inn for inn, row in manual.items() if row["decision"] == "техническая"}
    technical = [r for r in rows if r["bond"] and (
        r["inn"] in manual_technical
        or (r["status"] != "дефолт" and _cause_class(r["cause"], False) == "техническая"))]
    print(f"сообщений эмитентов вне «Д»: {len(rows)} (про облигации {sum(r['bond'] for r in rows)}, "
          f"из них технических неплатежей {len({(r['emitent_id'], r['due_date']) for r in technical})} "
          f"({len(technical)} сообщений) у {len({r['emitent_id'] for r in technical})} эмитентов — "
          f"в эпизоды не входят)")
    print(f"эпизодов: {len(table)}, эмитентов: {len({t['emitent_id'] for t in table})}")
    for role in ("дефолт", "только техдефолт", "исключён"):
        mine = [t for t in table if t["role"] == role]
        print(f"  {role}: {len(mine)} эпизодов, эмитентов {len({t['emitent_id'] for t in mine})}, "
              f"первых у эмитента {sum(t['first_for_issuer'] for t in mine)}")


# --- даты выплат по исполненным неплатежам (задача B) -------------------------

# «Выплаченные доходы или иные выплаты» — сообщение, в котором эмитент
# пишет дату фактической выплаты. Номер сверяется со справочником
# /api/data/sevent-types при первом запуске (`cures-texts` печатает название).
CURE_TYPE = 167
# Окно поиска после плановой даты: льготный срок до 10 рабочих дней плюс
# выплаты после него (КИВИ Финанс заплатил через 50 дней после оферты).
CURE_WINDOW = 60


def _company_ids() -> dict[str, tuple[int, str]]:
    """pseudoGUID сообщения → (companyID, companyName) из выдачи поиска."""
    return {m["pseudoGUID"]: (m["companyID"], m["companyName"]) for m in _messages()}


def _cure_candidates() -> list[dict]:
    """Неплатежи, по которым ищется выплата: ранние у эмитентов «Д» и все не-дефолты вне «Д».

    Неплатёж в день ноль эмитента «Д» сюда не входит, так как его исход —
    дефолт. Один неплатёж = эмитент и плановая дата, сколько бы выпусков
    и сообщений на неё ни приходилось.
    """
    ids = _company_ids()
    with (OUT / "day_zero.csv").open() as fh:
        zero = {r["emitent_id"]: r["day_zero"] for r in csv.DictReader(fh)}
    excluded = {inn for inn, row in _manual_extra().items() if row.get("decision") == "исключить"}
    found: dict[tuple, dict] = {}

    def add(row: dict, group: str) -> None:
        guid = row["url"].rsplit("EventId=", 1)[-1]
        if guid not in ids or row["due_date"] < f"{FIRST_YEAR}-01-01":
            return
        company_id, company = ids[guid]
        key = (company_id, row["due_date"])
        item = found.setdefault(key, {
            "emitent_id": row["emitent_id"], "emitent_title": row["emitent_title"],
            "company_id": company_id, "company": company, "due_date": row["due_date"],
            "group": group, "isins": set(), "causes": []})
        item["isins"].update(row["isins"].split())
        if row.get("cause"):
            item["causes"].append(row["cause"])

    with (OUT / "disclosure_defaults.csv").open() as fh:
        for row in csv.DictReader(fh):
            if zero.get(row["emitent_id"]) and row["due_date"] < zero[row["emitent_id"]]:
                add(row, "ранний у «Д»")
    with (OUT / "techdefault_messages.csv").open() as fh:
        for row in csv.DictReader(fh):
            if row["bond"] == "True" and row["status"] != "дефолт" and row["inn"] not in excluded:
                add(row, "ранний у «Д»" if row["emitent_id"] in zero else "вне «Д»")
    return sorted(found.values(), key=lambda r: (r["due_date"], r["company_id"]))


def _search_company(page, company: str, start: dt.date, finish: dt.date) -> dict:
    form = {
        "eventTypeTerm": "", "radView": "0", "eventTypeCheckboxGroup": str(CURE_TYPE),
        "dateStart": start.strftime("%d.%m.%Y"), "dateFinish": finish.strftime("%d.%m.%Y"),
        "textfieldEvent": "", "radReg": "FederalDistricts", "districtsCheckboxGroup": "-1",
        "regionsCheckboxGroup": "-1", "branchesCheckboxGroup": "-1", "textfieldCompany": company,
        # Фильтр по компании сайт берёт из `query`, а `textfieldCompany` —
        # только поле ввода: с одним им выдача идёт по всем эмитентам.
        "lastPageSize": "2147483647", "lastPageNumber": "1", "query": company, "queryEvent": "",
    }
    body = urllib.parse.urlencode(form)
    text = page.evaluate(
        """body => fetch('/api/search/sevents', {method: 'POST', body,
             headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                       'X-Requested-With': 'XMLHttpRequest'}}).then(r => r.text())""",
        body,
    )
    return json.loads(text)


def cmd_cures_texts(limit: str = "") -> None:
    """Поиск сообщений о выплате после каждого неплатежа и скачивание их текстов."""
    candidates = _cure_candidates()
    folder = RAW / "cures"
    folder.mkdir(exist_ok=True)
    todo = [c for c in candidates
            if not (folder / f"{c['company_id']}_{c['due_date']}.json").exists()]
    print(f"неплатежей: {len(candidates)} ({sum(c['group'] == 'ранний у «Д»' for c in candidates)} ранних у «Д»), "
          f"искать: {len(todo)}", flush=True)
    if limit:
        todo = todo[: int(limit)]
    if todo:
        with session() as page:
            types = page.evaluate("() => fetch('/api/data/sevent-types').then(r => r.text())")
            (RAW / "sevent_types.json").write_text(types)
            name = re.search(rf'"id"\s*:\s*{CURE_TYPE}\b[^}}]*?"name"\s*:\s*"([^"]+)"', types)
            print(f"  тип {CURE_TYPE}: {name.group(1) if name else 'не найден в справочнике'}", flush=True)
            for number, c in enumerate(todo, 1):
                start = dt.date.fromisoformat(c["due_date"])
                # Окно не заходит в будущее: на такую дату сайт отвечает 400.
                finish = min(start + dt.timedelta(days=CURE_WINDOW), dt.date.today())
                reply = _search_company(page, c["company"], start, finish)
                if "foundEventsList" not in reply:
                    raise SystemExit(f"поиск {c['company']} {c['due_date']} не удался: {str(reply)[:200]}")
                (folder / f"{c['company_id']}_{c['due_date']}.json").write_text(
                    json.dumps(reply, ensure_ascii=False))
                if number % 10 == 0 or number == len(todo):
                    print(f"  поиск: {number}/{len(todo)}", flush=True)
                _pause()
    guids = []
    for c in candidates:
        path = folder / f"{c['company_id']}_{c['due_date']}.json"
        if path.exists():
            guids += [m["pseudoGUID"] for m in json.loads(path.read_text())["foundEventsList"]
                      if m["companyID"] == c["company_id"]]
    _download(guids, limit)


def _all_dates(text: str) -> list[str]:
    """Все даты текста в ISO: и «23.01.2017», и «23 января 2017»."""
    out = []
    for groups in re.findall(ANY_DATE, text):
        if groups[0]:
            out.append(f"{groups[2]}-{groups[1]}-{int(groups[0]):02d}")
        else:
            month = next(n for stem, n in MONTHS.items() if groups[4].lower().startswith(stem))
            out.append(f"{groups[5]}-{month:02d}-{int(groups[3]):02d}")
    return out


def _cure_message(text: str, isins: set[str]) -> dict:
    """Что сообщение о выплате говорит о неплатеже: плановая дата, полнота, факт."""
    flat = re.sub(r"\s+", " ", text)
    # Плановая дата — в пункте «дата, в которую обязательство должно быть
    # исполнено»; до следующего пункта вида « 2.5. ».
    planned = re.search(r"(?:должн[аоы]? быть исполнен|[Пп]лановая дата).{0,700}?(?=\s\d\.\d{1,2}\.\s)", flat)
    volume = re.search(r"Объем \(в процентах\), в котором исполнено обязательство[^:]*:\s*([^%]{0,40}?)"
                       r"(\d{1,3}(?:[.,]\d+)?)\s*%", flat)
    i = flat.lower().find("причин")
    clause = flat[i:i + 500] if i >= 0 else ""
    after = clause[clause.find(":") + 1:][:150] if ":" in clause else clause
    full_words = (bool(re.search(r"(исполнен\w*|выплачен\w*)( эмитентом)? в полном объ[её]ме", clause, re.I))
                  and not re.search(r"выплачен\w* (не|в неполном)|не исполнен", after, re.I))
    j = flat.find("актическ")
    found = set(re.findall(r"RU[0-9A-Z]{10}", flat))
    return {
        "planned": _all_dates(planned.group(0)) if planned else [],
        "full": (float(volume.group(2).replace(",", ".")) == 100) if volume else full_words,
        "fact": _all_dates(flat[j:j + 250]) if j >= 0 else [],
        "same_issue": not found or not isins or bool(found & isins),
    }


def _business_days(start: str, finish: str, workdays: set[str]) -> int:
    """Рабочие дни после `start` до `finish` включительно."""
    day, end, count = dt.date.fromisoformat(start), dt.date.fromisoformat(finish), 0
    while day < end:
        day += dt.timedelta(days=1)
        count += day.isoformat() in workdays
    return count


def cmd_cures() -> None:
    """Дата выплаты после каждого неплатежа и число рабочих дней k → cures.csv.

    Выплатой считается сообщение типа 167, в котором плановая дата совпадает
    с датой неплатежа, обязательство исполнено полностью и выпуск тот же.
    Дата — фактическая из текста, если она есть, иначе дата сообщения, но
    не в сам день неплатежа: такое сообщение пишут о плановой выплате.
    """
    with (OUT / "business_days.csv").open() as fh:
        workdays = {row["date"] for row in csv.DictReader(fh)}
    rows = []
    for c in _cure_candidates():
        path = RAW / "cures" / f"{c['company_id']}_{c['due_date']}.json"
        cure, how, guid = "", "", ""
        if path.exists():
            found = sorted((m for m in json.loads(path.read_text())["foundEventsList"]
                            if m["companyID"] == c["company_id"]), key=lambda m: m["eventDate"])
            for m in found:
                page = RAW / "events" / f"{m['pseudoGUID']}.html"
                if not page.exists():
                    continue
                msg = _cure_message(_text(page), c["isins"])
                if c["due_date"] not in msg["planned"] or not msg["full"] or not msg["same_issue"]:
                    continue
                fact = [day for day in msg["fact"] if day >= c["due_date"]]
                if fact:
                    cure, how = fact[0], "фактическая дата в тексте"
                elif m["eventDate"][:10] > c["due_date"]:
                    cure, how = m["eventDate"][:10], "дата сообщения"
                if cure:
                    guid = m["pseudoGUID"]
                    break
        rows.append({
            "emitent_id": c["emitent_id"], "emitent_title": c["emitent_title"], "company": c["company"],
            "due_date": c["due_date"], "group": c["group"], "isins": " ".join(sorted(c["isins"])),
            "cure_date": cure, "how": how,
            "k": _business_days(c["due_date"], cure, workdays) if cure else "",
            "searched": path.exists(), "guid": guid,
        })
    with (OUT / "cures.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    dated = [r for r in rows if r["cure_date"]]
    print(f"неплатежей: {len(rows)}, с датой выплаты: {len(dated)}, "
          f"k от {min(r['k'] for r in dated)} до {max(r['k'] for r in dated)} рабочих дней")


if __name__ == "__main__":
    commands = {"list": cmd_list, "texts": cmd_texts, "parse": cmd_parse,
                "cid-texts": cmd_cid_texts, "extra-texts": cmd_extra_texts, "extra": cmd_extra,
                "cures-texts": cmd_cures_texts, "cures": cmd_cures, "restruct-list": cmd_restruct_list,
                "restruct-texts": cmd_restruct_texts}
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    commands[sys.argv[1]](*sys.argv[2:])
