"""Правила: что считать событием, кому оно адресовано и когда о нём напоминать.

Здесь же живёт фильтр по типу бумаги. Он несимметричный и это намеренно:
по облигации важно, заплатит ли эмитент по долгу, а дивиденды и допэмиссии
держателя долга не касаются. По акции наоборот — интересно всё.
"""

from __future__ import annotations

import datetime as dt
import json

from .config import Config
from .calendar import COUPON, AMORTIZATION, OFFER, MATURITY, upcoming

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
SHORT_MONTHS = (
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)

TITLES = {
    COUPON: "Купон",
    AMORTIZATION: "Амортизация",
    OFFER: "Оферта",
    MATURITY: "Погашение",
}

# Telegram не умеет ни заголовков, ни списков, ни горизонтальных линий:
# разметка ограничена жирным, курсивом и моноширинным. Поэтому структуру
# сообщения держат эмодзи и символьный разделитель — иначе всё сливается
# в сплошную стену текста.
RULE = "➖➖➖➖➖➖➖➖➖➖"

EVENT_EMOJI = {
    COUPON: "💰",
    AMORTIZATION: "📉",
    OFFER: "⚠️",
    MATURITY: "🏁",
}

EVENT_HEADERS = {
    COUPON: "КУПОНЫ",
    AMORTIZATION: "АМОРТИЗАЦИЯ",
    OFFER: "ОФЕРТА",
    MATURITY: "ПОГАШЕНИЕ",
}

# Какие корпоративные новости показывать по бумаге в зависимости от того,
# чем именно вы владеете.
# «legal» — аресты, уголовные дела, банкротство, санкции. Для держателя долга
# это сигнал не хуже дефолта: платить будет нечем раньше, чем это признают официально.
NEWS_FOR_BOND = {"default", "restructuring", "rating", "offer", "report", "legal"}
NEWS_FOR_SHARE = NEWS_FOR_BOND | {"dividend", "issue", "buyback", "split"}


def relevant_news(kind: str, category: str) -> bool:
    """Показывать ли новость категории `category` владельцу бумаги типа `kind`."""
    if kind == "bond":
        return category in NEWS_FOR_BOND
    if kind == "share":
        return category in NEWS_FOR_SHARE
    return category in NEWS_FOR_BOND


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 1 выплата, 2 выплаты, 5 выплат."""
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        return one
    if 2 <= tail <= 4 and not 12 <= hundred <= 14:
        return few
    return many


def number(value: float, places: int = 2) -> str:
    """1234.5 → «1 234,50». Курс ЦБ публикуется с четырьмя знаками, оттого и places."""
    return f"{value:,.{places}f}".replace(",", " ").replace(".", ",")


def money(value: float) -> str:
    """1234.5 → «1 234,50»."""
    return number(value)


def signed(value: float, places: int = 2) -> str:
    """«+0,28» или «-7,12»: без знака рост не отличить от падения."""
    return f"{value:+,.{places}f}".replace(",", " ").replace(".", ",")


def human_date(value: dt.date) -> str:
    return f"{value.day} {MONTHS[value.month - 1]}"


def short_date(value: dt.date) -> str:
    """«13 сен» — для календарных списков, где дата стоит в начале каждой строки."""
    return f"{value.day:2d} {SHORT_MONTHS[value.month - 1]}"


def _sent(config: Config) -> dict:
    path = config.state_file("sent.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _remember(config: Config, keys: list[str]) -> None:
    """Помним отправленное: Jessica просыпается ежечасно и не должна повторяться."""
    log = _sent(config)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    for key in keys:
        log[key] = stamp
    # Записи старше года не нужны — файл не должен расти вечно.
    cutoff = (dt.datetime.now() - dt.timedelta(days=365)).isoformat()
    fresh = {key: value for key, value in log.items() if value >= cutoff}
    config.state_file("sent.json").write_text(
        json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _amortized(config: Config) -> set[str]:
    """Выпуски, где вместе с купоном возвращается часть номинала.

    Сам размер выплаты источник не раскрывает, поэтому Jessica предупреждает
    о ней словами, а не цифрой: обещать точную сумму, которой не знаешь, хуже,
    чем честно сказать, что придёт больше.
    """
    from .calendar import load

    return {
        isin
        for isin, card in load(config).get("details", {}).items()
        if card.get("amortized")
    }


def due(config: Config, today: dt.date | None = None) -> tuple[str, list[str]]:
    """Готовит сообщение на сегодня. Возвращает текст и ключи для отметки об отправке."""
    today = today or dt.date.today()
    horizon = max(max(config.reminders(name), default=0) for name in TITLES)
    events = upcoming(config, days=horizon, today=today)
    already = _sent(config)

    buckets: dict[tuple[int, str], list[dict]] = {}
    keys: list[str] = []
    for event in events:
        date = dt.date.fromisoformat(event["date"])
        lead = (date - today).days
        if lead not in config.reminders(event["type"]):
            continue
        key = f"{event['isin']}|{event['type']}|{event['date']}|{lead}|{event['portfolio']}"
        if key in already:
            continue
        buckets.setdefault((lead, event["type"]), []).append(event)
        keys.append(key)

    if not buckets:
        return "", []

    amortized = _amortized(config)
    blocks: list[str] = []
    for (lead, kind), items in sorted(buckets.items()):
        date = dt.date.fromisoformat(items[0]["date"])
        when = "СЕГОДНЯ" if lead == 0 else f"ЧЕРЕЗ {lead} {plural(lead, 'ДЕНЬ', 'ДНЯ', 'ДНЕЙ')}"
        blocks.append(f"{EVENT_EMOJI[kind]} <b>{EVENT_HEADERS[kind]} {when}</b>")
        blocks.append(f"<i>{human_date(date)}</i>")
        # Оферту нельзя проспать: не подать заявку — остаться с бумагой,
        # которую больше не собирались держать.
        if kind == OFFER:
            blocks.append("❗️ <b>Нужно подать заявку брокеру</b>")
        blocks.append(RULE)

        total = 0.0
        by_portfolio: dict[str, list[dict]] = {}
        for item in items:
            by_portfolio.setdefault(item["portfolio"], []).append(item)

        for portfolio, rows in sorted(by_portfolio.items()):
            blocks.append(f"\n🏦 <b>{portfolio}</b>")
            for row in sorted(rows, key=lambda r: r["name"]):
                if row["total"] is None:
                    blocks.append(f"• {row['name']}\n   ⏳ <i>ставка ещё не объявлена</i>")
                else:
                    total += row["total"]
                    note = " 📉" if kind == COUPON and row["isin"] in amortized else ""
                    blocks.append(
                        f"• {row['name']}{note}\n"
                        f"   <b>{money(row['total'])} ₽</b>"
                        f"  <i>· {row['quantity']:.0f} × {money(row['amount'])}</i>"
                    )
        if total:
            blocks.append(f"\n{RULE}\n💵 <b>Итого: {money(total)} ₽</b>")
        if kind == COUPON and any(item["isin"] in amortized for item in items):
            blocks.append(
                "\n📉 <i>— выпуск с амортизацией: вместе с купоном вернётся "
                "часть номинала, придёт больше указанного.</i>"
            )
        blocks.append("")

    return "\n".join(blocks).strip(), keys


def send_due(config: Config, today: dt.date | None = None, *, dry_run: bool = False) -> str:
    from . import notify

    text, keys = due(config, today)
    if not text:
        return ""
    if dry_run:
        return text
    if notify.send(config, text):
        _remember(config, keys)
    return text


def monthly_calendar(config: Config, today: dt.date | None = None) -> str:
    """Сводка выплат на месяц вперёд, отдельно по каждому портфелю."""
    today = today or dt.date.today()
    events = upcoming(config, days=31, today=today)
    if not events:
        return ""

    by_portfolio: dict[str, list[dict]] = {}
    for event in events:
        by_portfolio.setdefault(event["portfolio"], []).append(event)

    finish = today + dt.timedelta(days=31)
    lines = [
        "📅 <b>ВЫПЛАТЫ НА МЕСЯЦ</b>",
        f"<i>{human_date(today)} — {human_date(finish)}</i>",
        RULE,
    ]
    grand_total = 0.0
    grand_unknown = 0

    for portfolio, rows in sorted(by_portfolio.items()):
        lines.append(f"\n🏦 <b>{portfolio}</b>")
        total = 0.0
        unknown = 0
        for event in rows:
            date = dt.date.fromisoformat(event["date"])
            when = f"<code>{short_date(date)}</code>"
            if event["total"] is None:
                unknown += 1
                lines.append(f"{when} · {event['name']}\n            ⏳ <i>ставка не объявлена</i>")
            else:
                total += event["total"]
                # Купон подписывать незачем — он и так основной случай, а вот
                # погашение и оферту нужно назвать: это возврат номинала, не доход.
                mark = "" if event["type"] == COUPON else f" {EVENT_EMOJI[event['type']]}"
                label = (
                    ""
                    if event["type"] == COUPON
                    else f" <i>({TITLES[event['type']].lower()})</i>"
                )
                lines.append(
                    f"{when} · {event['name']}{mark}\n"
                    f"            <b>{money(event['total'])} ₽</b>{label}"
                )
        word = plural(len(rows), "выплата", "выплаты", "выплат")
        lines.append(f"   <b>{money(total)} ₽</b> — {len(rows)} {word}")
        if unknown:
            issues = plural(unknown, "выпуску", "выпускам", "выпускам")
            lines.append(f"   ⏳ ещё по {unknown} {issues} ставка не объявлена")
        grand_total += total
        grand_unknown += unknown

    if len(by_portfolio) > 1:
        word = plural(len(events), "выплату", "выплаты", "выплат")
        lines.append(f"\n{RULE}")
        lines.append(f"💵 <b>ВСЕГО: {money(grand_total)} ₽</b>")
        lines.append(f"<i>за {len(events)} {word} по двум портфелям</i>")
    if grand_unknown:
        lines.append("\n<i>Как только ставки объявят, пришлю уточнение.</i>")
    return "\n".join(lines)


# ------------------------------------------------------------------- новости

CATEGORY_TITLES = {
    "default": "ДЕФОЛТ",
    "legal": "ПРАВОВОЙ РИСК",
    "restructuring": "РЕСТРУКТУРИЗАЦИЯ",
    "rating": "КРЕДИТНЫЙ РЕЙТИНГ",
    "offer": "ОФЕРТА ИЛИ ДОСРОЧНОЕ ПОГАШЕНИЕ",
    "report": "ОТЧЁТНОСТЬ",
    "dividend": "ДИВИДЕНДЫ",
    "issue": "ДОПЭМИССИЯ",
    "buyback": "ОБРАТНЫЙ ВЫКУП",
    "split": "ДРОБЛЕНИЕ АКЦИЙ",
}

CATEGORY_EMOJI = {
    "default": "🔴",
    "legal": "⚖️",
    "restructuring": "🔄",
    "rating": "📊",
    "offer": "⚠️",
    "report": "📄",
    "dividend": "💵",
    "issue": "📈",
    "buyback": "♻️",
    "split": "✂️",
}

# Эти события присылаются по всему рынку, а не только по своим бумагам:
# чужой дефолт — повод заранее посмотреть на собственные выпуски.
MARKET_WIDE = {"default", "restructuring"}

# Но только если речь именно о долге конкретного эмитента. Обзор ЦБ о том, что
# спрос на реструктуризацию потребительских кредитов снизился, — это макростатистика,
# и в ленте дефолтов ей делать нечего.
BOND_CONTEXT = ("облигац", "выпуск", "эмитент", "купон", "бонд", "займ", "ноты", "цб серии")


def news_digest(config: Config, today: dt.date | None = None) -> tuple[str, list[str]]:
    """Новости по эмитентам портфеля плюс дефолты по рынку.

    Чтобы уведомление ушло, нужны сразу два условия: компания действительно
    упомянута и событие относится к значимой категории. Одного совпадения
    названия мало — в ленте полно новостей, где слово встречается случайно.
    """
    from .sources import news

    today = today or dt.date.today()
    holdings = json.loads(config.state_file("holdings.json").read_text(encoding="utf-8"))
    positions = [
        {**position, "portfolio": block["title"]}
        for block in holdings["portfolios"].values()
        for position in block["positions"]
    ]
    markers = {
        position["isin"]: news.company_markers(position["name"], position.get("issuer", ""))
        for position in positions
    }

    already = _sent(config)
    blocks: list[str] = []
    keys: list[str] = []
    horizon = (today - dt.timedelta(days=2)).isoformat()

    for item in news.fetch():
        if item["date"] < horizon:
            continue
        category = news.classify(item["title"], item.get("source", ""))
        if category is None:
            continue

        mine = [
            position
            for position in positions
            if news.mentions(item["title"], markers[position["isin"]])
            and relevant_news(position["kind"], category)
        ]
        if not mine:
            lowered = item["title"].lower()
            if category not in MARKET_WIDE or not any(w in lowered for w in BOND_CONTEXT):
                continue

        key = f"news|{item['url'] or item['title'][:60]}"
        if key in already:
            continue
        keys.append(key)

        title = CATEGORY_TITLES.get(category, "СОБЫТИЕ")
        emoji = CATEGORY_EMOJI.get(category, "📰")
        header = f"{emoji} <b>{title}</b>" if mine else f"{emoji} <b>{title}</b> <i>· по рынку</i>"
        lines = [header, RULE, notify_escape(item["title"])]
        if mine:
            names = "\n".join(f"• {name}" for name in sorted({p["name"] for p in mine}))
            lines.append(f"\n<b>Ваши бумаги:</b>\n{names}")
        links = []
        if item["url"]:
            links.append(f'<a href="{item["url"]}">📰 источник</a>')
        if mine:
            # Официальное раскрытие закрыто капчей для программ, но не для человека,
            # поэтому даём прямую ссылку на поиск по компании.
            company = mine[0].get("issuer") or mine[0]["name"]
            links.append(f'<a href="{news.disclosure_link(company)}">🏛 раскрытие</a>')
        if links:
            lines.append("\n" + "   ".join(links))
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks), keys


def notify_escape(value: str) -> str:
    from .notify import escape

    return escape(value)


def send_news(config: Config, today: dt.date | None = None, *, dry_run: bool = False) -> str:
    from . import notify

    text, keys = news_digest(config, today)
    if not text or dry_run:
        return text
    if notify.send(config, text, preview=False):
        _remember(config, keys)
    return text


# --------------------------------------------------------------------- рынок

# IMOEX2, а не IMOEX: он считается за весь торговый день, включая утреннюю
# и вечернюю сессии, — к десяти утра по нему уже есть сегодняшнее движение.
MARKET_INDEX = "IMOEX2"


def _trend(change: float) -> str:
    """Стрелка направления: в сплошном тексте знак «минус» глазом теряется."""
    if change > 0:
        return "🔺"
    if change < 0:
        return "🔻"
    return "➡️"


def _change_line(value: float, previous: float, base: str, unit: str = "") -> str:
    """Строка изменения: абсолютное, процентное и к чему именно оно посчитано.

    База называется датой, а не словом «вчера»: после праздников предыдущий
    торговый день отстоит на неделю, и «вчера» было бы неправдой.
    """
    change = value - previous
    percent = change / previous * 100
    return (
        f"   {_trend(change)} <b>{signed(change)}{unit} ({signed(percent)}%)</b>"
        f"  <i>· к {base}</i>"
    )


def _index_block(index: dict | None) -> list[str]:
    if index is None:
        return [f"📊 <b>{MARKET_INDEX}</b> — <i>Мосбиржа не ответила</i>"]

    head = f"📊 <b>{MARKET_INDEX}</b> — {number(index['value'])}"
    date = human_date(dt.date.fromisoformat(index["date"]))
    if not index["live"]:
        # Выходной или праздник: торгов сегодня не было, и выдавать последнее
        # значение за сегодняшнее нельзя.
        head += f"  <i>· закрытие {date}</i>"
    elif index["time"]:
        head += f"  <i>· {index['time'][:5]}</i>"

    lines = [head]
    if index["previous"] is not None:
        base = human_date(dt.date.fromisoformat(index["previous_date"]))
        lines.append(_change_line(index["value"], index["previous"], f"закрытию {base}"))
    return lines


def _usd_block(usd: dict | None, today: dt.date) -> list[str]:
    if usd is None:
        return ["💵 <b>Доллар ЦБ</b> — <i>ЦБ не ответил</i>"]

    head = f"💵 <b>Доллар ЦБ</b> — {number(usd['value'], 4)} ₽"
    # Курс ставится по рабочим дням и действует до следующего установленного:
    # в субботу на экране пятничный, и дату нужно назвать.
    if usd["date"] != today.isoformat():
        head += f"  <i>· на {human_date(dt.date.fromisoformat(usd['date']))}</i>"

    lines = [head]
    if usd["previous"] is not None:
        base = human_date(dt.date.fromisoformat(usd["previous_date"]))
        lines.append(_change_line(usd["value"], usd["previous"], base, unit=" ₽"))
    return lines


def market_digest(config: Config, today: dt.date | None = None) -> str:
    """Утренняя сводка: индекс МосБиржи и официальный курс доллара.

    Источники независимы, и молчание одного не отменяет сводку: ISS Мосбиржи
    из-под VPN держится хуже, чем сайт ЦБ, а курс доллара нужен не меньше индекса.
    Пустая строка возвращается, только если молчат оба, — тогда `run` не отметит
    рассылку выполненной и повторит попытку через час.
    """
    from .sources import market

    today = today or dt.date.today()
    index = market.fetch_index(MARKET_INDEX, timeout=config.moex_timeout, today=today)
    usd = market.fetch_cbr_usd(today=today)
    if index is None and usd is None:
        return ""

    return "\n".join(
        [f"📈 <b>РЫНОК · {human_date(today)}</b>", RULE, ""]
        + _index_block(index)
        + [""]
        + _usd_block(usd, today)
    )


def send_market(config: Config, today: dt.date | None = None, *, dry_run: bool = False) -> str:
    """Отправляет сводку. Пустая строка означает «не ушло» — и повтор через час."""
    from . import notify

    text = market_digest(config, today)
    if not text or dry_run:
        return text
    return text if notify.send(config, text) else ""
