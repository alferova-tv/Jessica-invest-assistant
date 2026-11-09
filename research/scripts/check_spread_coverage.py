"""Предварительное недельное покрытие спредов: только локальный кэш.

Не рассчитывает признаки или процентили. Число строк с исходными полями —
верхняя оценка покрытия. Второй счётчик использует предварительный тип
по уже прошедшим купонам, без сегодняшнего описания выпуска. Доступность
самих сведений о купонах на историческую дату ещё требует проверки.
"""
from __future__ import annotations

import bisect
import csv
import datetime as dt
import json
import math
import statistics
from collections import Counter, defaultdict

from check_duration import schedule
from check_waprice import DATA, cached_rows


def main():
    with (DATA / 'out' / 'bonds.csv').open() as fh:
        bonds = [r for r in csv.DictReader(fh) if r['in_study'] == 'True']
    with (DATA / 'out' / 'kbd_weekly.csv').open() as fh:
        curves = {r['week'] for r in csv.DictReader(fh)
                  if all(r.get(k) and math.isfinite(float(r[k]))
                         for k in ('B1', 'B2', 'B3', 'T1', *[f'G{i}' for i in range(1, 10)]))
                  and float(r['T1']) > 0}
    dates = {}
    def week(date):
        if date not in dates:
            day = dt.date.fromisoformat(date)
            dates[date] = (day + dt.timedelta(days=4-day.weekday())).isoformat()
        return dates[date]
    counts = defaultdict(Counter)
    for bond in bonds:
        # Никакой выборки по будущему исходу эмитента.
        last = {}
        for r in cached_rows(bond['secid']):
            if r['TRADEDATE'] < '2014-01-01' or not (r['NUMTRADES'] or 0) > 0:
                continue
            w = week(r['TRADEDATE'])
            if w not in last or r['TRADEDATE'] > last[w][0]:
                last[w] = (r['TRADEDATE'], [])
            if r['TRADEDATE'] == last[w][0]:
                last[w][1].append(r)
        coupons = sorted(schedule(bond['secid'])['coupons'], key=lambda c: c['coupondate'])
        coupon_dates, states = [], []
        previous = None
        changes = periods = 0
        for c in coupons:
            rate = c.get('valueprc')
            if rate is None:
                continue
            if previous is not None:
                periods += 1
                changes += rate != previous
            previous = rate
            coupon_dates.append(c['coupondate'])
            # Нужны хотя бы две известные ставки для сравнения.
            states.append(periods > 0 and changes / periods < .5)
        for w, (date, rows) in last.items():
            counts[w]['traded_issues'] += 1
            if w not in curves:
                continue
            # Несколько режимов за последний день: считаем выпуск один раз,
            # если хотя бы одна строка содержит пару. Выбор режима не утверждён.
            if not any(r['YIELDATWAP'] is not None and math.isfinite(r['YIELDATWAP'])
                       and r['DURATION'] is not None and math.isfinite(r['DURATION'])
                       and r['DURATION'] > 0 for r in rows):
                continue
            counts[w]['with_pair_and_curve'] += 1
            # Классификация к концу воскресенья недели, а не к пятнице.
            end = (dt.date.fromisoformat(w) + dt.timedelta(days=2)).isoformat()
            i = bisect.bisect_right(coupon_dates, end) - 1
            if i >= 0 and states[i]:
                counts[w]['preliminary_fixed_coupon'] += 1
    summary = {}
    for year in sorted({w[:4] for w in counts}):
        ws = sorted(w for w in counts if w.startswith(year))
        item = {'weeks': len(ws), 'weeks_without_curve': sum(w not in curves for w in ws)}
        for field in ('with_pair_and_curve', 'preliminary_fixed_coupon'):
            values = sorted(counts[w][field] for w in ws if w in curves)
            if values:
                item[field] = {'min': min(values), 'median': statistics.median(values),
                               'weeks_below_30': sum(v < 30 for v in values),
                               'weeks_below_50': sum(v < 50 for v in values),
                               'weeks_below_100': sum(v < 100 for v in values)}
        summary[year] = item
    print(json.dumps({'issues': len(bonds), 'by_year': summary,
                      'weekly_counts': {w: dict(c) for w, c in sorted(counts.items())}},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
