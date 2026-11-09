import datetime as dt
import json
import unittest
from unittest.mock import Mock, patch

from jessica.sources import market


class MarketFallbackTest(unittest.TestCase):
    def test_malformed_iss_response_uses_alor(self):
        candle = {"time": int(dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc).timestamp()), "close": 2500}
        for payload in ({"marketdata": {}}, [], {"marketdata": {"columns": ["CURRENTVALUE"], "data": [["bad", 1]]}}):
            with self.subTest(payload=payload), \
                    patch.object(market, "_get", side_effect=[Mock(text=json.dumps(payload)), None,
                                                           Mock(text=json.dumps({"history": [candle]}))]), \
                    patch.object(market, "_index_smartlab") as last, \
                    self.assertLogs(market.log, "WARNING"):
                index = market.fetch_index(today=dt.date(2026, 11, 9))
            self.assertEqual(index["value"], 2500)
            last.assert_not_called()

    def test_bad_alor_candles_use_smartlab(self):
        expected = {"secid": "IMOEX", "value": 2500, "previous": None, "previous_date": None,
                    "date": None, "time": "", "live": False}
        for history in ([None], [{"time": "invalid", "close": 2500}], {"bad": "structure"}):
            with self.subTest(history=history), patch.object(market, "_index_iss", return_value=None), \
                    patch.object(market, "_get", return_value=Mock(text=json.dumps({"history": history}))), \
                    patch.object(market, "_index_smartlab", return_value=expected), \
                    self.assertLogs(market.log, "WARNING"):
                self.assertEqual(market.fetch_index(), expected)

    def test_all_malformed_sources_return_none(self):
        with patch.object(market, "_index_iss", side_effect=KeyError("columns")), \
                patch.object(market, "_index_alor", side_effect=ValueError("bad candles")), \
                patch.object(market, "_index_smartlab", side_effect=ValueError("bad number")), \
                self.assertLogs(market.log, "WARNING"):
            self.assertIsNone(market.fetch_index())

    def test_invalid_index_numbers_use_next_source(self):
        good = {"secid": "IMOEX", "value": 2500, "previous": None, "date": None, "time": "", "live": False}
        for value in ("2500", float("nan"), float("inf"), 0):
            with self.subTest(value=value), patch.object(market, "_index_iss", return_value={**good, "value": value}), \
                    patch.object(market, "_index_alor", return_value=good), self.assertLogs(market.log, "WARNING"):
                self.assertEqual(market.fetch_index(), good)
