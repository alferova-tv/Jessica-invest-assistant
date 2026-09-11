"""Новости по эмитентам портфеля и дефолты по рынку.

Официальный источник раскрытия — e-disclosure.ru — закрыт капчей, а капчу
Jessica не обходит. Поэтому факт события она узнаёт из открытых лент, а ссылку
даёт на Интерфакс: человека сайт пускает без препятствий.

Дефолты отслеживаются по всему рынку, а не только по своим бумагам: чужой дефолт
в том же секторе — повод посмотреть на свои выпуски заранее.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
import urllib.parse
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger(__name__)

# Ленты разной природы, и это намеренно. smart-lab даёт телеграфные заголовки
# корпоративных событий — под них и писались CATEGORIES. The Bell пишет
# развёрнутыми фразами и в основном про макроэкономику, но иногда первым
# сообщает о неплатежах конкретной компании, а такая новость приходит раньше,
# чем событие признают дефолтом.
#
# Ленты опрашиваются независимо: молчание одной не должно лишать слова вторую.
# The Bell вдобавок заблокирован в РФ и держится на VPN, поэтому отваливаться
# он будет чаще остальных — и это штатная ситуация, а не сбой.
FEEDS: tuple[tuple[str, str], ...] = (
    ("smart-lab", "https://smart-lab.ru/news/rss/"),
    ("the-bell", "https://thebell.io/feed"),
)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
DISCLOSURE_SEARCH = "https://www.e-disclosure.ru/poisk-po-kompaniyam?queryString={query}"

# Категория новости определяется по формулировке заголовка. Маркеры — регулярные
# выражения, потому что часть слов опасно искать подстрокой: «иск» сидит внутри
# «поиска», «суд» — внутри «судно». Порядок важен: «дефолт по облигациям» —
# это дефолт, а не погашение.
CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("default", (r"дефолт", r"не исполнил обязательств", r"неисполнени\w* обязательств",
                 r"просрочил\w* выплат", r"допустил\w* просрочк", r"не выплатил\w* купон")),
    ("legal", (r"\bарест", r"\bуголовн", r"\bобыск", r"\bзадержан", r"\bмошенничеств",
               r"\bбанкротств", r"\bследстви", r"\bск рф\b", r"\bсуд(?:а|ом|у|е|ом)?\b",
               r"\bиск(?:а|ом|у|ов|и)?\b", r"\bпод стражу", r"\bналоговы\w* претензи",
               r"\bсанкц", r"\bвнешн\w* управлени", r"\bнационализац")),
    ("restructuring", (r"реструктуризац\w* (?:долг|облигац|выпуск|займ)", r"пересмотр условий выпуска")),
    ("rating", (r"рейтинг", r"прогноз изменен", r"отозвал\w* рейтинг")),
    ("offer", (r"оферт", r"выкуп облигаци", r"досрочно погасит", r"досрочное погашение")),
    ("report", (r"\bмсфо\b", r"\bрсбу\b", r"отч[её]тность", r"финансовые результаты",
                r"результаты за", r"чистая прибыль", r"выручка за", r"отчитал")),
    ("dividend", (r"дивиденд", r"отсечк", r"закрытие реестра")),
    ("issue", (r"допэмисс", r"дополнительн\w* выпуск", r"\bspo\b", r"\bipo\b", r"размещени\w* акци")),
    ("buyback", (r"buyback", r"обратн\w* выкуп акци")),
    ("split", (r"сплит", r"дроблени\w* акци")),
)

# Часть формулировок опознаёт не событие, а манеру издания, и общей таблице
# только вредит. «Проблемы с выплатами» у The Bell — это почти всегда неплатёж
# конкретной компании, а на потоке smart-lab та же фраза цепляет обзоры рынка
# потребкредитования. Такие маркеры живут отдельно и применяются только к своей
# ленте; общая таблица остаётся сводом формулировок, верных для всех источников.
#
# Категория берётся из CATEGORIES — завести здесь новую нельзя, и намеренно:
# набор категорий определяет `relevant_news` в events.py, а он про ленты не знает.
SOURCE_CATEGORIES: dict[str, dict[str, tuple[str, ...]]] = {
    "the-bell": {
        # Развёрнутые описания неплатежа. До слова «дефолт» такая новость
        # доходит месяцами, а держателю облигации знать нужно сразу.
        "default": (r"проблем\w* с выплат", r"задерж\w*(?:ает|ки|ку)? выплат",
                    r"перестал\w* платить", r"не мож\w* расплатиться",
                    r"нечем платить", r"не расплатил"),
        "restructuring": (r"просит отсрочк", r"договаривается с кредитор",
                          r"перенос\w* выплат", r"уговарива\w* кредитор"),
    },
}


def _compile(extra: dict[str, tuple[str, ...]]) -> tuple[tuple[str, tuple[re.Pattern, ...]], ...]:
    """Общая таблица плюс маркеры одной ленты. Порядок категорий сохраняется."""
    return tuple(
        (name, tuple(re.compile(m, re.I) for m in markers + extra.get(name, ())))
        for name, markers in CATEGORIES
    )


COMPILED = _compile({})
COMPILED_BY_SOURCE = {source: _compile(extra) for source, extra in SOURCE_CATEGORIES.items()}

# Слова, не помогающие опознать компанию: организационные формы и родовые части
# названий. Без их отсева «Общество с ограниченной ответственностью» совпадало бы
# с любым эмитентом страны.
NOISE = re.compile(
    r"\b(ооо|оао|пао|ао|мкпао|нао|зао|обб?|бо|об|серии|выпуск|класс|холдинг|финанс\w*|"
    r"капитал|групп\w*|компани\w*|акционерн\w*|обществ\w*|ограниченной|ответственностью|"
    r"публичн\w*|открыт\w*|специализированн\w*|международн\w*|министерств\w*|"
    r"российск\w*|федерац\w*|лизинг|секьюритизаци\w*|фабрика)\b",
    re.I,
)

# Коды выпусков вида «БО-П20», «001Р-07R», «оббП05» — они мешают выделить имя компании.
CODE = re.compile(r"[A-Za-zА-Яа-я]*\d[\w\-.]*|\bП?\d+[A-Za-zА-Яа-я]?\b")

# Государственные бумаги из новостной ленты не отслеживаются: у Минфина
# не бывает корпоративных событий, а его название совпадает с половиной ленты.
# «Правительство РФ» — так эмитента ОФЗ называет T-Invest API.
GOVERNMENT = re.compile(r"офз|министерств\w* финансов|правительств\w*", re.I)


# Слова, которые встречаются в половине новостей и опознают что угодно.
STOPWORDS = {
    "центр", "россии", "рынок", "рынка", "технологии", "корпоративный", "денежный",
    "имени", "шашина", "грузов", "перевозке", "контейнерах", "мобильные", "альфа",
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

ABBREVIATION = re.compile(r"\b[А-ЯЁA-Z]{2,5}\b")


def company_markers(name: str, issuer: str = "") -> dict[str, set[str]]:
    """Признаки для опознания компании в заголовке новости.

    Возвращает две группы. `stems` — основы длинных слов: русский язык склоняет
    названия («Татнефть» → «Татнефти»), поэтому сравнивается начало слова, а не
    слово целиком. `abbr` — аббревиатуры вроде РЖД или ЛСР: они короткие, и искать
    их можно только целым словом, иначе совпадёт что угодно.
    """
    source = f"{name} {issuer}".strip()
    if GOVERNMENT.search(source):
        return {"stems": set(), "abbr": set()}

    abbr = {a for a in ABBREVIATION.findall(source) if a not in STOP_ABBR}

    cleaned = CODE.sub(" ", source.replace("«", " ").replace("»", " ").replace('"', " "))
    cleaned = NOISE.sub(" ", cleaned)
    stems = set()
    for word in re.split(r"[\s,\-/()]+", cleaned):
        word = word.strip(".").lower()
        if len(word) >= 4 and word not in STOPWORDS and "." not in word:
            # Отрезаем окончание: двух букв хватает, чтобы покрыть падежи,
            # но у коротких слов основу дальше резать нельзя.
            stems.add(word[:-2] if len(word) > 5 else word[:-1])
    return {"stems": stems, "abbr": abbr}


def mentions(title: str, markers: dict[str, set[str]]) -> bool:
    """Упомянута ли компания в заголовке."""
    lowered = title.lower()
    if any(stem in lowered for stem in markers["stems"]):
        return True
    return any(re.search(rf"\b{re.escape(a)}\b", title, re.I) for a in markers["abbr"])


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


def classify(title: str, source: str = "") -> str | None:
    """Категория события. Без указания ленты работают только общие маркеры."""
    for category, markers in COMPILED_BY_SOURCE.get(source, COMPILED):
        if any(marker.search(title) for marker in markers):
            return category
    return None


def disclosure_link(name: str) -> str:
    """Ссылка на поиск по компании в Интерфаксе — открывать её будет человек."""
    return DISCLOSURE_SEARCH.format(query=urllib.parse.quote(name))


def _download(name: str, url: str, timeout: float) -> str:
    """Тело ленты. Пустая строка, если она так и не ответила.

    VPN изредка моргает, поэтому разовый таймаут — повод повторить попытку,
    а не считать ленту мёртвой.
    """
    for attempt in range(2):
        try:
            response = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            if attempt:
                log.warning("Лента %s недоступна: %s", name, exc)
            else:
                time.sleep(2)
    return ""


def _parse(raw: str, source: str) -> list[dict]:
    """Записи ленты. Формат RSS у всех источников одинаков, разбор общий."""
    items = []
    for chunk in re.findall(r"<item>(.*?)</item>", raw, re.S):
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", chunk, re.S)
        link = re.search(r"<link>(.*?)</link>", chunk, re.S)
        published = re.search(r"<pubDate>(.*?)</pubDate>", chunk, re.S)
        if not title:
            continue
        when = None
        if published:
            try:
                when = parsedate_to_datetime(published.group(1).strip()).date().isoformat()
            except (TypeError, ValueError):
                when = None
        items.append(
            {
                "title": re.sub(r"\s+", " ", title.group(1)).strip(),
                "url": link.group(1).strip() if link else "",
                "date": when or dt.date.today().isoformat(),
                "source": source,
            }
        )
    return items


def fetch(*, timeout: float = 20) -> list[dict]:
    """Последние новости всех лент. Недоступная лента просто не даёт записей."""
    items: list[dict] = []
    for name, url in FEEDS:
        items.extend(_parse(_download(name, url, timeout), name))
    return items


def feed_status(*, timeout: float = 20) -> list[tuple[str, int]]:
    """Сколько записей отдаёт каждая лента — для `jessica doctor`."""
    return [(name, len(_parse(_download(name, url, timeout), name))) for name, url in FEEDS]
