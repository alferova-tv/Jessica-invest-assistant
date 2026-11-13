import datetime as dt
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jessica.cli import MOSCOW, cmd_run, rebuild_needed, refresh_due, stale_portfolios
from jessica.config import Config

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

    def test_attempt_with_timezone(self):
        now = NOW.replace(tzinfo=MOSCOW)
        # 13:00 в Екатеринбурге — 11:00 в Москве, прошёл только час.
        state = {"refresh_attempt": "2026-09-11T13:00:00+05:00"}
        self.assertFalse(refresh_due(state, now, 3))
        state["refresh_attempt"] = "2026-09-11T11:00:00+05:00"
        self.assertTrue(refresh_due(state, now, 3))

    def test_legacy_attempt_uses_mac_timezone(self):
        try:
            with patch.dict(os.environ, {"TZ": "Asia/Yekaterinburg"}):
                time.tzset()
                now = NOW.replace(tzinfo=MOSCOW)
                state = {"refresh_attempt": "2026-09-11T13:00:00"}
                self.assertFalse(refresh_due(state, now, 3))
                state["refresh_attempt"] = "2026-09-11T11:00:00"
                self.assertTrue(refresh_due(state, now, 3))
        finally:
            time.tzset()


class MarketScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config({"schedule": {"market_hour": 10}}, Path(self.tmp.name))

    def run_at(self, instant):
        # Без явного часового пояса часы возвращают местное время UTC+5.
        local = instant.astimezone(dt.timezone(dt.timedelta(hours=5)))

        def clock(tz=None):
            return local.astimezone(tz) if tz else local.replace(tzinfo=None)

        with patch("jessica.cli.dt.datetime", wraps=dt.datetime) as timer, \
                patch("jessica.cli.refresh_due", return_value=False), \
                patch("jessica.cli.events.send_news"), \
                patch("jessica.cli.events.send_due"), \
                patch("jessica.cli.events.send_market", return_value="сводка") as send:
            timer.now.side_effect = clock
            cmd_run(self.config, None)
            return send

    def test_no_market_before_ten_moscow_even_when_mac_is_past_ten(self):
        for hour, minute in ((8, 0), (9, 59)):
            with self.subTest(hour=hour, minute=minute):
                send = self.run_at(dt.datetime(2026, 11, 5, hour, minute, tzinfo=MOSCOW))
                send.assert_not_called()
                state = json.loads(self.config.state_file("last_run.json").read_text())
                self.assertNotIn("market", state)

    def test_market_at_ten_moscow_only_once(self):
        instant = dt.datetime(2026, 11, 5, 10, tzinfo=MOSCOW)
        self.run_at(instant).assert_called_once_with(self.config, instant.date(), scheduled=True)
        self.run_at(instant + dt.timedelta(hours=1)).assert_not_called()

    def test_missed_hour_is_caught_up(self):
        instant = dt.datetime(2026, 11, 5, 12, tzinfo=MOSCOW)
        self.run_at(instant).assert_called_once_with(self.config, instant.date(), scheduled=True)

    def test_market_is_sent_before_refresh_and_marker_survives_refresh_error(self):
        instant = dt.datetime(2026, 11, 5, 10, tzinfo=MOSCOW)
        marker = self.config.state_file("last_run.json")
        with patch("jessica.cli.dt.datetime", wraps=dt.datetime) as timer, \
                patch("jessica.cli.events.send_market", return_value="сводка") as send, \
                patch("jessica.cli.holdings.collect") as collect:
            timer.now.return_value = instant

            def failed_refresh(config):
                send.assert_called_once_with(config, instant.date(), scheduled=True)
                self.assertEqual(json.loads(marker.read_text())["market"], "2026-11-05")
                raise RuntimeError("источник портфеля недоступен")

            collect.side_effect = failed_refresh
            with self.assertRaises(RuntimeError):
                cmd_run(self.config, None)

        self.run_at(instant).assert_not_called()

    def test_failed_market_send_is_retried(self):
        instant = dt.datetime(2026, 11, 5, 10, tzinfo=MOSCOW)
        with patch("jessica.cli.events.send_market", return_value=""), \
                patch("jessica.cli.dt.datetime", wraps=dt.datetime) as timer, \
                patch("jessica.cli.refresh_due", return_value=False), \
                patch("jessica.cli.events.send_news"), patch("jessica.cli.events.send_due"):
            timer.now.return_value = instant
            cmd_run(self.config, None)
        state = json.loads(self.config.state_file("last_run.json").read_text())
        self.assertNotIn("market", state)
        self.run_at(instant + dt.timedelta(hours=1)).assert_called_once_with(
            self.config, instant.date(), scheduled=True)

    def test_weekend_is_delegated_to_exchange_calendar_in_moscow(self):
        saturday = dt.datetime(2026, 11, 7, 10, tzinfo=MOSCOW)
        self.run_at(saturday).assert_called_once_with(self.config, saturday.date(), scheduled=True)
        # На Mac уже суббота, но в Москве ещё пятница.
        friday = dt.datetime(2026, 11, 6, 23, tzinfo=MOSCOW)
        self.run_at(friday).assert_called_once_with(self.config, friday.date(), scheduled=True)


class NotificationRetryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config({"schedule": {"market_hour": 24}}, Path(tmp.name))

    def run_at(self, day):
        with patch("jessica.cli.dt.datetime", wraps=dt.datetime) as timer, \
                patch("jessica.cli.refresh_due", return_value=False), \
                patch("jessica.cli.events.send_news"):
            timer.now.return_value = dt.datetime(2026, 11, day, 12, tzinfo=MOSCOW)
            cmd_run(self.config, None)
        return json.loads(self.config.state_file("last_run.json").read_text())

    def test_failed_daily_send_retries_and_success_stops_retries(self):
        with patch("jessica.events.due", return_value=("Напоминание", ["event-key"])), \
                patch("jessica.notify.send", side_effect=[False, True]) as send:
            self.assertNotIn("checked", self.run_at(9))
            self.assertFalse(self.config.state_file("sent.json").exists())
            self.assertEqual(self.run_at(9)["checked"], "2026-11-09")
            self.run_at(9)
        self.assertEqual(send.call_count, 2)
        self.assertIn("event-key", json.loads(self.config.state_file("sent.json").read_text()))

    def test_monthly_failure_retries_even_after_daily_success(self):
        with patch("jessica.events.send_due", return_value="") as due, \
                patch("jessica.events.monthly_calendar", return_value="Календарь"), \
                patch("jessica.notify.send", side_effect=[False, True]) as send:
            first = self.run_at(1)
            self.assertIn("checked", first)
            self.assertNotIn("monthly", first)
            self.assertEqual(self.run_at(1)["monthly"], "2026-11-01")
            self.run_at(1)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(due.call_count, 1)

    def test_no_events_counts_as_completed_without_sending(self):
        with patch("jessica.events.due", return_value=("", [])), patch("jessica.notify.send") as send:
            self.assertIn("checked", self.run_at(9))
        send.assert_not_called()


class CalendarRetryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config({"schedule": {"morning_hour": 24, "market_hour": 24}}, Path(tmp.name))
        self.config.state_file("last_run.json").write_text(json.dumps({"refreshed": "2026-11-09"}))
        self.config.state_file("schedule.json").write_text(json.dumps({
            "updated": "2026-11-09T09:00:00+03:00", "incomplete": ["TEST"], "incomplete_details": [],
        }))

    def run_at(self, hour):
        with patch("jessica.cli.dt.datetime", wraps=dt.datetime) as timer, \
                patch("jessica.cli.holdings.collect") as collect, patch("jessica.cli.events.send_news"):
            timer.now.return_value = dt.datetime(2026, 11, 9, hour, tzinfo=MOSCOW)
            cmd_run(self.config, None)
        collect.assert_not_called()

    def test_retry_waits_three_hours_even_when_holdings_are_fresh(self):
        with patch("jessica.cli.cal.build") as build:
            self.run_at(10)
            build.assert_not_called()
            self.run_at(12)
            build.assert_called_once_with(self.config, retry_failed=True)
            self.run_at(13)
            self.assertEqual(build.call_count, 1)
            self.run_at(15)
            self.assertEqual(build.call_count, 2)

    def test_card_only_failure_is_retried_and_success_stops_retries(self):
        path = self.config.state_file("schedule.json")
        path.write_text(json.dumps({"updated": "2026-11-09T09:00:00+03:00", "incomplete": [],
                                    "incomplete_details": ["TEST"]}))

        def success(*args, **kwargs):
            path.write_text(json.dumps({"incomplete": [], "incomplete_details": []}))

        with patch("jessica.cli.cal.build", side_effect=success) as build:
            self.run_at(12)
            self.run_at(15)
        build.assert_called_once_with(self.config, retry_failed=True)


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
