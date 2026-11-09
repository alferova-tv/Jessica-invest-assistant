import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portfel import paths, server

sys.path.insert(0, str(paths.JESSICA_CODE))
from jessica.sources import trading_calendar


class QuoteCalendarTest(unittest.TestCase):
    def test_calendar_controls_dates_instead_of_weekdays(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"JESSICA_HOME": tmp}):
            with patch.object(trading_calendar, "get_day", return_value={"is_traded": True}):
                self.assertTrue(server._trading(dt.datetime(2026, 11, 7, 12)))
            with patch.object(trading_calendar, "get_day", return_value={"is_traded": False}):
                self.assertFalse(server._trading(dt.datetime(2026, 11, 4, 12)))
            with patch.object(trading_calendar, "get_day", return_value=None):
                self.assertTrue(server._trading(dt.datetime(2026, 11, 7, 12)))

    def test_outside_refresh_hours_does_not_request_calendar(self):
        with patch.object(trading_calendar, "get_day") as get:
            self.assertFalse(server._trading(dt.datetime(2026, 11, 9, 3)))
            get.assert_not_called()
