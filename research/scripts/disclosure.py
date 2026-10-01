"""Сообщения «Неисполнение обязательств» с сервера раскрытия Интерфакса.

Источник дня ноль из первоисточника: в сообщении эмитента есть плановая
дата платежа, выпуск и отметка «технический дефолт» или «дефолт».

e-disclosure закрыт антиботом, иногда с капчей. Капчу проходит человек
в открытом окне браузера; скрипт её не решает, а ждёт, пока пройдут.
Профиль браузера с отметкой о проверке лежит рядом с сырыми данными.

    .venv/bin/python research/scripts/disclosure.py list    # список сообщений по годам
    .venv/bin/python research/scripts/disclosure.py texts   # тексты сообщений наших эмитентов
    .venv/bin/python research/scripts/disclosure.py parse   # таблица disclosure_defaults.csv
    .venv/bin/python research/scripts/disclosure.py extra-texts  # тексты эмитентов вне «Д»
    .venv/bin/python research/scripts/disclosure.py extra        # эпизоды техдефолтов вне «Д»
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


def _search(page, start: dt.date, finish: dt.date) -> dict:
    form = {
        "eventTypeTerm": "", "radView": "0", "eventTypeCheckboxGroup": str(EVENT_TYPE),
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


def cmd_list() -> None:
    """Все сообщения типа 178 с 2014 года, по году на запрос."""
    today = dt.date.today()
    with session() as page:
        for year in range(FIRST_YEAR, today.year + 1):
            path = RAW / f"sevents_{year}.json"
            if path.exists() and year < today.year:
                continue
            periods = [(dt.date(year, 1, 1), min(dt.date(year, 12, 31), today))]
            found = []
            while periods:
                start, finish = periods.pop()
                reply = _search(page, start, finish)
                if reply["allFoundEvents"] > MAX_SHOWN:
                    middle = start + (finish - start) / 2
                    periods += [(start, middle), (middle + dt.timedelta(days=1), finish)]
                    continue
                found += reply["foundEventsList"]
                _pause()
            path.write_text(json.dumps(found, ensure_ascii=False))
            print(f"  {year}: {len(found)} сообщений", flush=True)


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
    low = re.sub(r"\(технический дефолт,? (?:или )?дефолт\)|по его вине \(дефолт\)|\(частичн\w+ неисполнени\w+\)",
                 " ", text.lower())
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


# Льготный срок — 10 рабочих дней, то есть 14–16 календарных. Окно 25 дней
# его покрывает и не дотягивается до предыдущего ежемесячного купона
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
    r"|операционн\w* риск|поступил\w* на счет депозитария на след",
    re.I,
)
CREDIT = re.compile(
    r"(?:отсутств|недостат|нехватк|недостаточн)\w*[^.]{0,60}денежн|денежн\w* средств\w*[^.]{0,40}(?:отсутств|недостат)"
    r"|задержк\w* поступлени|заблокирован|блокировк\w* (?:расч|счет|счёт)|арест|обеспечительн\w* мер"
    r"|финансов\w* затруднени",
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


def _extra_candidates() -> tuple[list[tuple[dict, list[str]]], dict[str, dict]]:
    """Сообщения с 2014 года от эмитентов облигаций, которых нет среди событий «Д»."""
    with (OUT / "events_issuers.csv").open() as fh:
        ours = {row["emitent_id"] for row in csv.DictReader(fh)}
    emitents = _bond_emitents()
    by_name: dict[str, list[str]] = {}
    found = []
    for msg in _messages():
        if msg["eventDate"][:10] < f"{FIRST_YEAR}-01-01":
            continue
        name = msg["companyName"]
        if name not in by_name:
            by_name[name] = [eid for eid, item in emitents.items() if _same(item["title"], name)]
        ids = [eid for eid in by_name[name] if eid not in ours]
        if ids and len(ids) == len(by_name[name]):
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
        day_zero = _episode_day_zero(episode) if declared else ""
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
    technical = [r for r in rows if r["bond"] and r["status"] != "дефолт"
                 and _cause_class(r["cause"], False) == "техническая"]
    print(f"сообщений эмитентов вне «Д»: {len(rows)} (про облигации {sum(r['bond'] for r in rows)}, "
          f"из них технических неплатежей {len(technical)} у {len({r['emitent_id'] for r in technical})} эмитентов — "
          f"в эпизоды не входят)")
    print(f"эпизодов: {len(table)}, эмитентов: {len({t['emitent_id'] for t in table})}")
    for role in ("дефолт", "только техдефолт", "исключён"):
        mine = [t for t in table if t["role"] == role]
        print(f"  {role}: {len(mine)} эпизодов, эмитентов {len({t['emitent_id'] for t in mine})}, "
              f"первых у эмитента {sum(t['first_for_issuer'] for t in mine)}")


if __name__ == "__main__":
    commands = {"list": cmd_list, "texts": cmd_texts, "parse": cmd_parse,
                "extra-texts": cmd_extra_texts, "extra": cmd_extra}
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    commands[sys.argv[1]](*sys.argv[2:])
