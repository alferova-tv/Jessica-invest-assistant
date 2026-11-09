"""Проверки исторической доступности и неполных источников событий."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bonds_events as be


BOND = {'secid': 'TEST', 'emitent_id': '1', 'shortname': 'Тест', 'last_day': '2014-12-31'}


def observations(field, values):
    return [({'TRADEDATE': day, field: value}, ('https://example.test', 'cache.json', i))
            for i, (day, value) in enumerate(values, 1)]


class HistoricalDates(unittest.TestCase):
    def test_invalid_dates_remain_missing(self):
        for value in (None, '', '0000-00-00', '2024-02-30', 'произвольная дата'):
            self.assertEqual(be.iso_date(value), '')
        self.assertEqual(be.iso_date('2024-02-29'), '2024-02-29')

    def test_change_is_known_only_when_observed(self):
        rows = be.historical_versions(BOND, observations('MATDATE', [
            ('2014-01-09', '2020-01-01'), ('2014-01-10', '2020-01-01'),
            ('2014-02-03', '2022-01-01'),
        ]), 'MATDATE')
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['known_from'] for r in rows], ['2014-01-09', '2014-02-03'])
        before = [r for r in rows if r['known_from'] <= '2014-02-01']
        self.assertEqual(before[-1]['event_date'], '2020-01-01')
        self.assertEqual(rows[-1]['event_date'], '2022-01-01')

    def test_disappearance_does_not_claim_cancellation(self):
        rows = be.historical_versions(BOND, observations('MATDATE', [
            ('2014-01-09', '2020-01-01'), ('2014-01-10', None),
            ('2014-01-13', '2020-01-01'),
        ]), 'MATDATE')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]['event_date'], '')
        self.assertEqual(rows[1]['status'], 'unknown')

    def test_conflicting_boards_leave_missing_date(self):
        rows = be.historical_versions(BOND, observations('MATDATE', [
            ('2014-01-09', '2020-01-01'), ('2014-01-09', '2021-01-01'),
        ]), 'MATDATE')
        self.assertEqual(rows[0]['event_date'], '')
        self.assertIn('Конфликт', rows[0]['note'])

    def test_buyback_does_not_invent_put_or_payment_date(self):
        row = be.historical_versions(BOND, observations('BUYBACKDATE', [
            ('2018-01-09', '2020-01-01'),
        ]), 'BUYBACKDATE')[0]
        self.assertEqual(row['event_type'], 'unknown')
        self.assertEqual(row['event_date'], '')
        self.assertEqual(row['known_from'], '2018-01-09')
        self.assertEqual(json.loads(row['source_value']), ['2020-01-01'])

    def test_current_cancelled_offer_is_not_cancelled_in_history(self):
        description = [dict(name='ISSUEDATE', title='Дата начала торгов', value='2013-01-01')]
        offer = dict(offerdate='2015-01-01', offerdatestart='2014-12-01',
                     offerdateend='2014-12-10', offertype='Оферта (отменено)')
        def cached(url, block, fields):
            if block == 'description':
                return {}, description, 'description.json'
            return {}, [offer], 'schedule.json'
        with patch.object(be, 'cached', side_effect=cached), patch.object(
            be, 'paged', return_value=observations('MATDATE', [('2014-01-09', '2020-01-01')])
        ):
            rows = be.build([BOND])
        placement = next(r for r in rows if r['event_type'] == 'placement')
        self.assertEqual(placement['event_date'], '')
        schedule = next(r for r in rows if r['source_kind'] == 'schedule')
        self.assertEqual(schedule['known_from'], '')
        self.assertEqual(schedule['event_date'], '')
        self.assertEqual(schedule['status'], 'unknown')
        self.assertEqual(schedule['source_status'], 'Оферта (отменено)')


class CacheIntegrity(unittest.TestCase):
    def test_missing_page_stops_instead_of_returning_empty_data(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            be.iss, '_path', return_value=Path(folder) / 'missing.json'
        ):
            with self.assertRaises(FileNotFoundError):
                list(be.paged('https://example.test?x=1', 'MATDATE'))

    def test_truncated_page_stops(self):
        payload = {'history.cursor': {'columns': ['TOTAL'], 'data': [[200]]}}
        chunk = [{'TRADEDATE': '2014-01-09', 'MATDATE': '2020-01-01'}]
        with patch.object(be, 'cached', return_value=(payload, chunk, 'partial.json')):
            with self.assertRaises(ValueError):
                list(be.paged('https://example.test?x=1', 'MATDATE'))


if __name__ == '__main__':
    unittest.main()
