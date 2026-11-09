"""События выпусков из локального кэша ISS → bonds_events.csv.

Без сети. Отсутствующий или неполный кэш останавливает сборку.
Исторический MATDATE даёт подтверждённую на день наблюдения дату
погашения. Неразмеченные оферты и BUYBACKDATE сохраняются как unknown;
их нельзя использовать как put/call. ISSUEDATE не подменяет размещение.

Из корня Jessica:
    .venv/bin/python research/scripts/bonds_events.py --output /tmp/bonds_events.csv
    .venv/bin/python research/scripts/bonds_events.py
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import events
import history
import iss

OUT = iss.HOME / 'research' / 'out'
COLUMNS = (
    'event_id', 'version', 'secid', 'emitent_id', 'shortname',
    'event_type', 'event_date', 'date_kind', 'offer_start', 'offer_end',
    'status', 'known_from', 'known_from_basis', 'source_kind', 'source',
    'source_field', 'cache_file', 'source_row', 'source_value', 'source_status', 'note',
)
DATE_COLUMNS = ('event_date', 'offer_start', 'offer_end', 'known_from')


def iso_date(value):
    """Нулевые и некорректные даты остаются пропусками, а не догадками."""
    if not isinstance(value, str) or not value:
        return ''
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        return ''
    return parsed.isoformat()


def cached(url, block, fields):
    path = iss._path(url)
    payload = json.loads(path.read_text())
    table = payload[block]
    if not set(fields).issubset(table['columns']):
        raise ValueError(f'{path}: нет обязательных полей {fields}')
    if any(len(r) != len(table['columns']) for r in table['data']):
        raise ValueError(f'{path}: длина строки не совпадает с заголовком')
    return payload, iss.rows(payload, block), path.name


def paged(url, field):
    start = 0
    while True:
        page_url = f'{url}&start={start}'
        payload, rows, file = cached(page_url, 'history', ('TRADEDATE', field))
        for i, row in enumerate(rows):
            if not iso_date(row['TRADEDATE']):
                raise ValueError(f'{file}: некорректная дата наблюдения')
            yield row, (page_url, file, i + 1)
        cursor = iss.rows(payload, 'history.cursor')
        if cursor:
            total = cursor[0]['TOTAL']
            if not rows and start < total:
                raise ValueError(f'{file}: преждевременно пустая страница')
            if start + len(rows) < min(start + 100, total):
                raise ValueError(f'{file}: неполная страница')
            if start + len(rows) >= total:
                return
        elif not rows:
            return
        start += 100


def base(bond, event_id, event_type, **values):
    return dict.fromkeys(COLUMNS, '') | {
        'event_id': f"{bond['secid']}:{event_id}", 'version': 1,
        'secid': bond['secid'], 'emitent_id': bond['emitent_id'],
        'shortname': bond['shortname'], 'event_type': event_type,
        'status': 'unknown', **values,
    }


def historical_versions(bond, observations, field):
    """Сохраняет смену значения поля, включая исчезновение и конфликт.

    Версия относится к состоянию поля ISS. Это не утверждение о том,
    что эмитент в этот день объявил перенос или отмену события.
    """
    days = defaultdict(list)
    for row, evidence in observations:
        if row['TRADEDATE'] >= '2014-01-01':
            days[row['TRADEDATE']].append((row, evidence))
    versions = []
    previous = None
    for day, entries in sorted(days.items()):
        dates = sorted({iso_date(r[field]) for r, _ in entries} - {''})
        state = tuple(dates)
        if state == previous:
            continue
        previous = state
        # Один непустой MATDATE достаточен; разные непустые даты
        # в режимах одного дня — конфликт. Пустота не означает отмену.
        row, evidence = min(entries, key=lambda item: (
            not bool(iso_date(item[0][field])), item[1]))
        date = dates[0] if len(dates) == 1 else ''
        url, file, index = evidence
        maturity = field == 'MATDATE'
        note = ('Первое наблюдение этой версии в дневной истории; '
                'дата публикации решения не установлена.')
        if len(dates) > 1:
            note += ' Конфликт дат между режимами: ' + ', '.join(dates) + '.'
        elif not date:
            note += ' Поле пустое или дата некорректна; это не подтверждение отмены.'
        elif not maturity:
            note += ' Тип оферты и смысл даты выплаты не подтверждены.'
        versions.append(base(
            bond, 'maturity:history' if maturity else 'offer:buyback_history',
            'maturity' if maturity else 'unknown', version=len(versions) + 1,
            event_date=date if maturity else '',
            date_kind='payment' if maturity and date else 'unconfirmed',
            status='scheduled' if maturity and date else 'unknown',
            known_from=day, known_from_basis='historical_observation',
            source_kind='history' if maturity else 'buyback_history',
            source=url, source_field=field, cache_file=file, source_row=index,
            source_value=json.dumps(dates, ensure_ascii=False), note=note,
        ))
    return versions


def build(bonds):
    result = []
    for number, bond in enumerate(bonds, 1):
        secid = bond['secid']
        desc_url = events.SECURITY_URL.format(secid=secid)
        _, description, desc_file = cached(desc_url, 'description', ('name', 'title', 'value'))
        desc = {r['name']: (i + 1, r) for i, r in enumerate(description)}
        issue = desc.get('ISSUEDATE')
        result.append(base(
            bond, 'placement', 'placement', date_kind='unconfirmed',
            source_kind='description', source=desc_url, source_field='ISSUEDATE',
            cache_file=desc_file, source_row=issue[0] if issue else '',
            source_value=issue[1]['value'] if issue else '',
            note='Дата размещения не подтверждена. ISSUEDATE обозначает начало торгов; '
                 'первый день истории и начало купона датой размещения не считаются.',
        ))
        maturity = historical_versions(
            bond, paged(history.HISTORY_URL.format(secid=secid), 'MATDATE'), 'MATDATE')
        if not maturity:
            raise ValueError(f'{secid}: нет дневной истории для выпуска из bonds.csv')
        result.extend(maturity)
        known_dates = {r['event_date'] for r in maturity if r['event_date']}
        # Текущая карточка — дополнительная дата без исторической доступности.
        mat = desc.get('MATDATE')
        if mat and iso_date(mat[1]['value']) and mat[1]['value'] not in known_dates:
            result.append(base(
                bond, 'maturity:description', 'maturity',
                event_date=iso_date(mat[1]['value']), date_kind='payment',
                source_kind='description', source=desc_url, source_field='MATDATE',
                cache_file=desc_file, source_row=mat[0], source_value=mat[1]['value'],
                note='Дата из текущей карточки; историческая доступность не подтверждена.',
            ))
        if bond['last_day'] >= history.BUYBACK_FROM:
            result.extend(historical_versions(
                bond, paged(history.BUYBACK_URL.format(secid=secid), 'BUYBACKDATE'), 'BUYBACKDATE'))
        url = history.BONDIZATION_URL.format(secid=secid)
        payload, offers, file = cached(url, 'offers', ('offerdate', 'offerdatestart', 'offerdateend', 'offertype'))
        cursor = iss.rows(payload, 'offers.cursor')
        if cursor and cursor[0]['TOTAL'] != len(offers):
            raise ValueError(f'{file}: график оферт обрезан')
        for i, offer in enumerate(offers, 1):
            result.append(base(
                bond, f'offer:schedule:{i}', 'unknown', date_kind='unconfirmed',
                offer_start=iso_date(offer['offerdatestart']),
                offer_end=iso_date(offer['offerdateend']),
                source_kind='schedule', source=url, source_field='offers',
                cache_file=file, source_row=i, source_value=offer['offerdate'] or '',
                source_status=offer['offertype'] or '',
                note='Дата оферты и окно из текущего графика. Put/call, дата выплаты '
                     'и историческая доступность не подтверждены; текущий статус '
                     'не перенесён в историю. Связь с BUYBACKDATE не установлена.',
            ))
        if number % 1000 == 0:
            print(f'  выпуски: {number}/{len(bonds)}', flush=True)
    return sorted(result, key=lambda r: (r['secid'], r['event_id'], r['version']))


def validate(rows, bonds):
    keys = [(r['event_id'], r['version']) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('Дубли ключа event_id + version')
    if {r['secid'] for r in rows} != {b['secid'] for b in bonds}:
        raise ValueError('Потеряны выпуски')
    for row in rows:
        for field in DATE_COLUMNS:
            if row[field] and not iso_date(row[field]):
                raise ValueError(f'Некорректная дата: {row}')
        if row['event_type'] == 'unknown' and row['event_date']:
            raise ValueError('Неподтверждённая дата платежа попала в event_date')
        if row['known_from'] and row['known_from_basis'] != 'historical_observation':
            raise ValueError('Дата доступности не имеет основания')
        if row['source_kind'] in ('schedule', 'description') and row['known_from']:
            raise ValueError('Текущим сведениям приписана историческая доступность')
    return {
        'issues': len(bonds), 'rows': len(rows),
        'by_type': dict(Counter(r['event_type'] for r in rows)),
        'by_source': dict(Counter(r['source_kind'] for r in rows)),
        'with_event_date': sum(bool(r['event_date']) for r in rows),
        'with_known_from': sum(bool(r['known_from']) for r in rows),
        'historical_maturity_versions_with_date': sum(
            r['source_kind'] == 'history' and bool(r['event_date']) for r in rows),
        'missing_placement_dates': sum(r['event_type'] == 'placement' and not r['event_date'] for r in rows),
        'unconfirmed_schedule_offers': sum(r['source_kind'] == 'schedule' for r in rows),
        'maturity_conflicts': sum('Конфликт дат' in r['note'] for r in rows if r['source_kind'] == 'history'),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUT / 'bonds_events.csv')
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f'Файл уже существует: {args.output}; перезапись не выполняется')
    with (OUT / 'bonds.csv').open() as fh:
        bonds = [r for r in csv.DictReader(fh) if r['in_study'] == 'True']
    if not bonds:
        raise ValueError('Пустой список выпусков исследования')
    rows = build(bonds)
    summary = validate(rows, bonds)
    # Итог заменяется атомарно только после чтения всего кэша и проверок.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8',
                                         dir=args.output.parent, delete=False) as fh:
            tmp = Path(fh.name)
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        # link не перезаписывает чужой файл, появившийся за время сборки.
        os.link(tmp, args.output)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f'Файл: {args.output}')


if __name__ == '__main__':
    main()
