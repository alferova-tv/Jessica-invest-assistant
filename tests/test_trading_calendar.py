import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jessica import events
from jessica.config import Config
from jessica.sources import market, trading_calendar as calendar


def payload(day="2026-11-07", flag=1, session="2026-11-09"):
    return {"off_days": {"columns": ["tradedate", "stock_workday", "stock_trade_session_date", "stock_reason"],
                         "data": [[day, flag, session, "W" if flag else "H"]]}}


class TradingCalendarTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "trading_calendar.json"
        self.bundle = self.root / "bundled.json"
        self.day = dt.date(2026, 11, 7)
        self.now = dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc)
        self.bundle.write_text(json.dumps(self.snapshot(payload(), self.now)))
        p = patch.object(calendar, "BUNDLED", self.bundle)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(calendar.dt, "datetime", wraps=dt.datetime)
        self.clock = p.start()
        self.clock.now.return_value = self.now
        self.addCleanup(p.stop)

    def snapshot(self, data, verified):
        return {"version": 1, "source": "moex", "verified_at": verified.isoformat(), "payload": data}

    def store(self, data, verified):
        self.path.write_text(json.dumps(self.snapshot(data, verified)))

    def test_bundled_weekend_maps_to_next_session_without_network(self):
        with patch.object(calendar, "_get") as get:
            row = calendar.get_day(self.day, self.path)
        self.assertTrue(row["is_traded"])
        self.assertEqual(row["session_date"], "2026-11-09")
        get.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_six_calendar_months_including_short_month(self):
        self.assertEqual(calendar.six_months_later(dt.date(2026, 8, 31)), dt.date(2027, 2, 28))
        self.assertEqual(calendar.six_months_later(dt.date(2026, 11, 9)), dt.date(2027, 5, 9))

    def test_half_year_refresh_uses_new_dates_and_preserves_bundle(self):
        self.clock.now.return_value = dt.datetime(2027, 5, 9, 12, tzinfo=dt.timezone.utc)
        before = self.bundle.read_bytes()
        with patch.object(calendar, "_fetch", return_value=calendar.parse(payload(flag=0, session=None))) as fetch:
            self.assertFalse(calendar.get_day(self.day, self.path)["is_traded"])
        fetch.assert_called_once_with(dt.date(2027, 5, 9), timeout=3)
        self.assertEqual(self.bundle.read_bytes(), before)
        with patch.object(calendar, "_fetch") as fetch:
            self.assertFalse(calendar.get_day(self.day, self.path)["is_traded"])
        fetch.assert_not_called()

    def test_failed_refresh_preserves_dates_and_retries_no_more_than_daily(self):
        self.clock.now.return_value = dt.datetime(2027, 5, 9, 12, tzinfo=dt.timezone.utc)
        with patch.object(calendar, "_fetch", side_effect=ValueError("HTML")) as fetch:
            with self.assertLogs(calendar.log, "WARNING"):
                self.assertTrue(calendar.get_day(self.day, self.path)["is_traded"])
            calendar.get_day(self.day, self.path)
            self.assertEqual(fetch.call_count, 1)
            self.clock.now.return_value += dt.timedelta(days=1)
            with self.assertLogs(calendar.log, "WARNING"):
                calendar.get_day(self.day, self.path)
            self.assertEqual(fetch.call_count, 2)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["verified_at"], self.now.isoformat())
        self.assertTrue(calendar.parse(stored["payload"])[self.day.isoformat()]["is_traded"])

    def test_unknown_date_fetches_early_but_does_not_guess_when_offline(self):
        with patch.object(calendar, "_fetch", side_effect=ValueError("blocked")) as fetch, self.assertLogs(calendar.log, "WARNING"):
            self.assertIsNone(calendar.get_day(dt.date(2027, 1, 2), self.path))
            self.assertIsNone(calendar.get_day(dt.date(2027, 1, 2), self.path))
        self.assertEqual(fetch.call_count, 1)

    def test_newer_bundled_version_wins_over_old_cache(self):
        self.store(payload(flag=0, session=None), self.now - dt.timedelta(days=1))
        with patch.object(calendar, "_fetch") as fetch:
            self.assertTrue(calendar.get_day(self.day, self.path)["is_traded"])
        fetch.assert_not_called()

    def test_unknown_remote_flag_does_not_erase_confirmed_date(self):
        self.clock.now.return_value = dt.datetime(2027, 5, 9, 12, tzinfo=dt.timezone.utc)
        with patch.object(calendar, "_fetch", return_value=calendar.parse(payload(flag=None, session=None))):
            self.assertTrue(calendar.get_day(self.day, self.path)["is_traded"])

    def test_corrupt_cache_falls_back_to_bundle(self):
        self.path.write_text("broken JSON")
        with patch.object(calendar, "_fetch") as fetch:
            self.assertTrue(calendar.get_day(self.day, self.path)["is_traded"])
        fetch.assert_not_called()

    def test_half_year_request_is_chunked_and_does_not_cross_year(self):
        def respond(url, timeout):
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            since, until = (dt.date.fromisoformat(query[k][0]) for k in ("from", "till"))
            self.assertEqual(since.year, until.year)
            self.assertLessEqual((until - since).days, 30)
            rows = [[(since + dt.timedelta(days=i)).isoformat(), 1, None, "N"] for i in range((until-since).days+1)]
            return SimpleNamespace(text=json.dumps({"off_days": {"columns": payload()["off_days"]["columns"], "data": rows}}))
        with patch.object(calendar, "_get", side_effect=respond):
            days = calendar._fetch(dt.date(2026, 11, 9), timeout=3)
        self.assertEqual(min(days), "2026-11-09")
        self.assertEqual(max(days), "2027-05-08")
        self.assertEqual(len(days), (dt.date(2027, 5, 9)-dt.date(2026, 11, 9)).days)

    def test_html_or_missing_days_abort_entire_update(self):
        for text in ("<html>Blocked</html>", json.dumps(payload())):
            with self.subTest(text=text), patch.object(calendar, "_get", return_value=SimpleNamespace(text=text)):
                with self.assertRaises(ValueError):
                    calendar._fetch(dt.date(2026, 11, 9), timeout=3)

    def test_bundled_calendar_matches_moex_announcements(self):
        # Читаем настоящий файл проекта, но не обращаемся в сеть и к рабочему кэшу.
        path = Path(calendar.__file__).resolve().parents[1] / "data/trading_calendar.json"
        stored = json.loads(path.read_text())
        days = calendar.parse(stored["payload"])
        known = [r for k, r in days.items() if k.startswith("2026-")]
        self.assertEqual(len(known), 365)
        self.assertEqual(sum(r["is_traded"] for r in known), 337)
        for day in ("2026-11-28", "2026-11-29", "2026-12-31"):
            self.assertFalse(days[day]["is_traded"])
        for day in ("2026-12-05", "2026-12-06", "2027-01-04"):
            self.assertTrue(days[day]["is_traded"])
        self.assertEqual(days["2026-12-05"]["session_date"], "2026-12-07")
        self.assertEqual(days["2026-06-12"]["session_date"], "2026-06-15")
        self.assertIsNone(days["2027-01-02"]["is_traded"])
        self.assertTrue(all(s["url"].startswith("https://www.moex.com/") for s in stored["sources"]))


class CalendarDigestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config({}, Path(tmp.name))
        self.today = dt.date(2026, 11, 7)
        self.index = {"secid": "IMOEX2", "value": 2000, "previous": 1990,
                      "previous_date": "2026-11-06", "date": "2026-11-07", "time": "10:15", "live": True}

    def test_closed_weekday_skips_scheduled_digest(self):
        with patch.object(calendar, "get_day", return_value={"is_traded": False, "session_date": "2026-11-04"}), \
                patch.object(market, "fetch_index") as fetch:
            self.assertEqual(events.market_digest(self.config, dt.date(2026, 11, 4), scheduled=True), "")
            fetch.assert_not_called()

    def test_trading_weekend_requests_the_correct_session(self):
        day = {"is_traded": True, "session_date": "2026-11-09"}
        with patch.object(calendar, "get_day", return_value=day), \
                patch.object(market, "fetch_index", return_value=self.index) as fetch, \
                patch.object(market, "fetch_cbr_usd", return_value=None):
            text = events.market_digest(self.config, self.today, scheduled=True)
            self.assertIn("РЫНОК · 7 ноября", text)
            self.assertEqual(fetch.call_args.args[0], "IMOEX")
            self.assertEqual(fetch.call_args.kwargs["session_date"], dt.date(2026, 11, 9))

    def test_unknown_calendar_only_accepts_a_fresh_dated_index(self):
        for index, allowed in ((self.index, True), ({**self.index, "live": False}, False),
                               ({**self.index, "date": "2026-11-06"}, False), (None, False)):
            with self.subTest(index=index), patch.object(calendar, "get_day", return_value=None), \
                    patch.object(market, "fetch_index", return_value=index), \
                    patch.object(market, "fetch_cbr_usd", return_value=None):
                self.assertEqual(bool(events.market_digest(self.config, self.today, scheduled=True)), allowed)

    def test_manual_digest_can_show_last_close_on_holiday(self):
        with patch.object(calendar, "get_day", return_value={"is_traded": False, "session_date": "2026-11-07"}), \
                patch.object(market, "fetch_index", return_value={**self.index, "live": False}), \
                patch.object(market, "fetch_cbr_usd", return_value=None):
            self.assertIn("закрытие 7 ноября", events.market_digest(self.config, self.today))


class IndexCalendarTest(unittest.TestCase):
    def test_unknown_mapping_does_not_invent_future_close(self):
        current = {"marketdata": {"columns": ["CURRENTVALUE", "TRADEDATE"], "data": [[2000, "2026-11-09"]]}}
        history = {"history": {"columns": ["TRADEDATE", "CLOSE"], "data": [["2026-11-06", 1990]]}}
        with patch.object(market, "_get", side_effect=[SimpleNamespace(text=json.dumps(x)) for x in (current, history)]):
            index = market._index_iss("IMOEX2", 3, dt.date(2026, 11, 7))
        self.assertIsNone(index["date"])
        text = "\n".join(events._index_block(index))
        self.assertIn("торговая дата 9 ноября", text)
        self.assertNotIn("закрытие 9 ноября", text)

    def test_iss_weekend_trade_date_is_not_a_future_close(self):
        current = {"marketdata": {"columns": ["CURRENTVALUE", "TRADEDATE", "UPDATETIME"],
                                  "data": [[2000, "2026-11-09", "10:15:00"]]}}
        history = {"history": {"columns": ["TRADEDATE", "CLOSE"], "data": [["2026-11-06", 1990]]}}
        replies = [SimpleNamespace(text=json.dumps(x)) for x in (current, history)]
        with patch.object(market, "_get", side_effect=replies):
            index = market._index_iss("IMOEX2", 3, dt.date(2026, 11, 7), dt.date(2026, 11, 9))
        self.assertTrue(index["live"])
        self.assertEqual(index["date"], "2026-11-07")
        self.assertEqual(index["session_date"], "2026-11-09")
        self.assertEqual(index["previous_date"], "2026-11-06")

    def test_alor_candle_for_next_session_is_current_on_working_weekend(self):
        def candle(day, value):
            stamp = int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp())
            return {"time": stamp, "close": value}
        data = {"history": [candle("2026-11-06", 1990), candle("2026-11-09", 2000)]}
        with patch.object(market, "_get", return_value=SimpleNamespace(text=json.dumps(data))):
            index = market._index_alor("IMOEX2", dt.date(2026, 11, 7), dt.date(2026, 11, 9))
        self.assertTrue(index["live"])
        self.assertEqual(index["date"], "2026-11-07")
        self.assertEqual(index["previous_date"], "2026-11-06")

    def test_smartlab_does_not_invent_quote_date_or_previous_business_day(self):
        raw = '<a href="/gr/MOEX.IMOEX">IMOEX</a></td><td>2 000,00</td><td>+1,0 %'
        with patch.object(market, "_get", return_value=SimpleNamespace(text=raw)):
            index = market._index_smartlab(dt.date(2026, 11, 7))
        self.assertIsNone(index["date"])
        self.assertIsNone(index["previous_date"])
        self.assertIsNone(index["previous"])
        self.assertIn("дата котировки неизвестна", "\n".join(events._index_block(index)))
