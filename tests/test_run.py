import datetime as dt
import unittest

from jessica.cli import rebuild_needed, refresh_due, stale_portfolios

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
