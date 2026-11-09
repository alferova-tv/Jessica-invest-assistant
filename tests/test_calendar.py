import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from jessica import calendar, events
from jessica.config import Config
from jessica.sources import smartlab


PAGE = '<table><tr><td>1</td><td>15-11-2026</td><td>12,50</td></tr></table>'
COUPON = {"isin": "TEST", "type": "coupon", "date": "2026-11-15", "amount": 10,
          "announced": True, "source": "smartlab"}
CARD = {"isin": "TEST", "nominal": "1000", "source": "smartlab", "amortized": False}
CARD_PAGE = '<p>Номинал 1000 Дата погашения 15-11-2027 Дата оферты 15-11-2026</p>'


def response(text=PAGE, status=200):
    result = Mock(text=text, status_code=status)
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


class CouponRetryTest(unittest.TestCase):
    def test_timeout_then_success_keeps_coupon(self):
        with patch.object(smartlab.requests, "get", side_effect=[requests.Timeout(), response()]) as get, \
                patch.object(smartlab.time, "sleep"):
            coupon, = smartlab.fetch_coupons("TEST")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(coupon["amount"], 12.5)

    def test_repeated_timeout_stops_after_three_attempts(self):
        with patch.object(smartlab.requests, "get", side_effect=requests.Timeout()) as get, \
                patch.object(smartlab.time, "sleep"), self.assertLogs(smartlab.log, "WARNING"):
            self.assertEqual(smartlab.fetch_coupons("TEST", "TEST"), [])
        self.assertEqual(get.call_count, 3)

    def test_server_failure_retries(self):
        with patch.object(smartlab.requests, "get", side_effect=[response(status=503), response()]) as get, \
                patch.object(smartlab.time, "sleep"):
            self.assertEqual(len(smartlab.fetch_coupons("TEST")), 1)
        self.assertEqual(get.call_count, 2)

    def test_missing_isin_page_uses_exchange_code_without_retrying_404(self):
        with patch.object(smartlab.requests, "get", side_effect=[response(status=404), response()]) as get, \
                patch.object(smartlab.time, "sleep") as sleep:
            self.assertEqual(len(smartlab.fetch_coupons("TEST", "CODE")), 1)
        self.assertEqual(get.call_count, 2)
        sleep.assert_not_called()
        self.assertIn("/CODE/", get.call_args.args[0])


class DetailsRetryTest(unittest.TestCase):
    def test_timeout_then_success_parses_card(self):
        with patch.object(smartlab.requests, "get", side_effect=[requests.Timeout(), response(CARD_PAGE)]) as get, \
                patch.object(smartlab.time, "sleep"):
            card = smartlab.fetch_details("TEST")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(card["maturity"], "2027-11-15")
        self.assertEqual(card["offer_date"], "2026-11-15")
        self.assertEqual(get.call_args.args[0], smartlab.DETAIL_URL.format(isin="TEST"))

    def test_failed_requests_stop_after_three_attempts(self):
        with patch.object(smartlab.requests, "get", side_effect=requests.Timeout()) as get, \
                patch.object(smartlab.time, "sleep"), self.assertLogs(smartlab.log, "WARNING"):
            self.assertEqual(smartlab.fetch_details("TEST", "TEST"), {})
        self.assertEqual(get.call_count, 3)

    def test_missing_card_uses_alternate_identifier(self):
        with patch.object(smartlab.requests, "get", side_effect=[response(status=404), response(CARD_PAGE)]) as get, \
                patch.object(smartlab.time, "sleep") as sleep:
            self.assertEqual(smartlab.fetch_details("TEST", "CODE")["isin"], "TEST")
        sleep.assert_not_called()
        self.assertIn("/CODE/", get.call_args.args[0])

    def test_http_200_without_card_fields_is_failure(self):
        with patch.object(smartlab.requests, "get", return_value=response("<p>Service unavailable</p>")), \
                self.assertLogs(smartlab.log, "WARNING"):
            self.assertEqual(smartlab.fetch_details("TEST"), {})

    def test_invalid_date_is_failure(self):
        with patch.object(smartlab.requests, "get", return_value=response(CARD_PAGE.replace("15-11-2027", "31-02-2027"))), \
                self.assertLogs(smartlab.log, "WARNING"):
            self.assertEqual(smartlab.fetch_details("TEST"), {})


class CalendarRecoveryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config({}, Path(tmp.name))
        self.config.state_file("holdings.json").write_text(json.dumps({"portfolios": {
            "alpha": {"title": "Альфа", "positions": [{"isin": "TEST", "name": "Выпуск", "quantity": 2, "kind": "bond"}]},
        }}))
        self.path = self.config.state_file("schedule.json")

    def old(self, rows, details=None):
        self.path.write_text(json.dumps({"events": rows, "details": details or {}}))

    def build(self, coupons, card=CARD):
        with patch.object(smartlab, "fetch_coupons", return_value=coupons), \
                patch.object(smartlab, "fetch_details", return_value=card):
            return calendar.build(self.config, pause=0)

    def test_retry_fetches_only_failed_held_bonds_and_drops_sold_bonds(self):
        holdings_path = self.config.state_file("holdings.json")
        data = json.loads(holdings_path.read_text())
        data["portfolios"]["alpha"]["positions"].append({
            "isin": "GOOD", "name": "Другой выпуск", "quantity": 1, "kind": "bond",
        })
        holdings_path.write_text(json.dumps(data))
        good = {**COUPON, "isin": "GOOD"}
        previous = {"incomplete": ["TEST", "SOLD"], "incomplete_details": ["TEST"],
                    "events": [good, {**COUPON, "stale": True}, {**COUPON, "isin": "SOLD"}],
                    "details": {"GOOD": {**CARD, "isin": "GOOD"}}}
        self.path.write_text(json.dumps(previous))
        with patch.object(smartlab, "fetch_coupons", return_value=[COUPON]) as coupons, \
                patch.object(smartlab, "fetch_details", return_value=CARD) as details:
            schedule = calendar.build(self.config, pause=0, retry_failed=True)
        coupons.assert_called_once_with("TEST", "")
        details.assert_called_once_with("TEST", "")
        self.assertEqual(schedule["incomplete"], [])
        self.assertEqual(schedule["incomplete_details"], [])
        self.assertCountEqual(schedule["events"], [good, COUPON])
        self.assertEqual(schedule["details"]["GOOD"], previous["details"]["GOOD"])

    def test_names_are_escaped_in_daily_and_monthly_messages(self):
        path = self.config.state_file("holdings.json")
        data = json.loads(path.read_text())
        data["portfolios"]["alpha"]["title"] = "Мой <портфель> & счёт"
        data["portfolios"]["alpha"]["positions"][0]["name"] = "Компания <Тест> & Ко"
        path.write_text(json.dumps(data))
        for amount in (None, 10):
            with self.subTest(amount=amount):
                self.old([{**COUPON, "amount": amount}])
                daily, keys = events.due(self.config, dt.date(2026, 11, 15))
                monthly = events.monthly_calendar(self.config, dt.date(2026, 11, 15))
                for text in (daily, monthly):
                    self.assertIn("Компания &lt;Тест&gt; &amp; Ко", text)
                    self.assertIn("Мой &lt;портфель&gt; &amp; счёт", text)
                    self.assertNotIn("<Тест>", text)
                self.assertTrue(keys[0].endswith("Мой <портфель> & счёт"))

    def test_news_position_names_are_escaped(self):
        path = self.config.state_file("holdings.json")
        data = json.loads(path.read_text())
        data["portfolios"]["alpha"]["positions"][0]["name"] = "Озон <Тест> & Ко"
        path.write_text(json.dumps(data))
        item = {"title": "Озон: кредитный рейтинг", "date": "2026-11-15", "url": "https://example.test/news"}
        with patch("jessica.sources.news.fetch", return_value=[item]), \
                patch("jessica.sources.news.classify", return_value="rating"):
            text, _ = events.news_digest(self.config, dt.date(2026, 11, 15))
        self.assertIn("Озон &lt;Тест&gt; &amp; Ко", text)

    def test_failed_card_preserves_only_held_bond_events_and_details(self):
        offer = {**COUPON, "type": "offer", "amount": 1000}
        maturity = {**offer, "type": "maturity", "date": "2027-11-15"}
        self.old([offer, maturity, {**offer, "isin": "SOLD"}], {"TEST": CARD, "SOLD": CARD})
        with self.assertLogs(calendar.log, "WARNING"):
            schedule = self.build([COUPON], card={})
        self.assertEqual(schedule["incomplete_details"], ["TEST"])
        self.assertEqual(schedule["details"], {"TEST": {**CARD, "stale": True}})
        self.assertEqual(schedule["events"], [COUPON, {**offer, "stale": True}, {**maturity, "stale": True}])
        with self.assertLogs(calendar.log, "WARNING"):
            self.assertEqual(self.build([COUPON], card={})["events"], schedule["events"])
        with patch("jessica.notify.send") as send:
            text = events.send_due(self.config, dt.date(2026, 11, 15), dry_run=True)
        send.assert_not_called()
        self.assertIn("данные из предыдущей загрузки", text)

    def test_successful_card_replaces_stale_dates_and_removes_absent_offer(self):
        self.old([{**COUPON, "type": "offer", "stale": True}], {"TEST": {**CARD, "stale": True}})
        fresh = {**CARD, "maturity": "2027-11-15"}
        schedule = self.build([COUPON], card=fresh)
        self.assertEqual(schedule["incomplete_details"], [])
        self.assertEqual(schedule["details"], {"TEST": fresh})
        self.assertEqual(schedule["events"], [COUPON, {
            "isin": "TEST", "type": "maturity", "date": "2027-11-15",
            "amount": 1000.0, "announced": True, "source": "smartlab",
        }])

    def test_first_card_failure_does_not_invent_dates(self):
        with self.assertLogs(calendar.log, "WARNING"):
            schedule = self.build([COUPON], card={})
        self.assertEqual(schedule["incomplete_details"], ["TEST"])
        self.assertEqual(schedule["details"], {})
        self.assertEqual(schedule["events"], [COUPON])

    def test_failed_update_preserves_old_coupon_and_marks_it_stale(self):
        self.old([COUPON])
        with self.assertLogs(calendar.log, "WARNING"):
            schedule = self.build([])
        self.assertEqual(schedule["incomplete"], ["TEST"])
        self.assertEqual(schedule["events"], [{**COUPON, "stale": True}])
        self.assertEqual(json.loads(self.path.read_text()), schedule)
        self.assertFalse(self.path.with_suffix(".tmp").exists())
        with self.assertLogs(calendar.log, "WARNING"):
            self.assertEqual(self.build([])["events"], schedule["events"])

    def test_later_success_replaces_old_amount_and_clears_stale_marker(self):
        self.old([{**COUPON, "stale": True}])
        fresh = {**COUPON, "amount": 12.5}
        schedule = self.build([fresh])
        self.assertEqual(schedule["events"], [fresh])
        self.assertEqual(schedule["incomplete"], [])

    def test_recovery_does_not_restore_sold_bonds_or_other_event_types(self):
        self.old([COUPON, {**COUPON, "isin": "SOLD"}, {**COUPON, "type": "maturity"}])
        with self.assertLogs(calendar.log, "WARNING"):
            self.assertEqual(self.build([])["events"], [{**COUPON, "stale": True}])

    def test_first_load_failure_is_incomplete_without_invented_events(self):
        with self.assertLogs(calendar.log, "WARNING"):
            schedule = self.build([])
        self.assertEqual(schedule["events"], [])
        self.assertEqual(schedule["incomplete"], ["TEST"])

    def test_failed_write_leaves_original_schedule_intact(self):
        self.old([COUPON])
        before = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.build([{**COUPON, "amount": 12.5}])
        self.assertEqual(self.path.read_bytes(), before)

    def test_preserved_coupon_still_produces_notification_with_warning(self):
        self.old([COUPON])
        with self.assertLogs(calendar.log, "WARNING"):
            self.build([])
        today = dt.date(2026, 11, 15)
        with patch("jessica.notify.send") as send:
            text = events.send_due(self.config, today, dry_run=True)
            send.assert_not_called()
        self.assertIn("20,00", text)
        self.assertIn("данные из предыдущей загрузки", text)
        self.assertIn("График из предыдущей загрузки", events.monthly_calendar(self.config, today))
