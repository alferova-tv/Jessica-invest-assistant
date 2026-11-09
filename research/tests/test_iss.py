import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import iss


def table(values, total=None):
    payload = {"history": {"columns": ["value"], "data": [[v] for v in values]}}
    if total is not None:
        payload["history.cursor"] = {"columns": ["TOTAL"], "data": [[total]]}
    return payload


class ISSFailureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(iss, "RAW", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.url = "https://example.test/iss/history.json?test=1"

    def test_first_page_failure_raises_without_empty_result_or_cache(self):
        with patch.object(iss.market, "_get", return_value=None):
            with self.assertRaises(iss.SourceUnavailable):
                iss.paged(self.url, "history")
        self.assertEqual(list(iss.RAW.iterdir()), [])

    def test_failed_later_page_keeps_cache_and_restart_resumes(self):
        with patch.object(iss.market, "_get", side_effect=[Mock(json=lambda: table([1, 2], 3)), None]), \
                patch.object(iss.time, "sleep"):
            with self.assertRaises(iss.SourceUnavailable):
                iss.paged(self.url, "history", page=2)
        first_cache = iss._path(self.url + "&start=0")
        before = first_cache.read_bytes()
        with patch.object(iss.market, "_get", return_value=Mock(json=lambda: table([3], 3))) as fetch, \
                patch.object(iss.time, "sleep"):
            self.assertEqual(iss.paged(self.url, "history", page=2), [{"value": 1}, {"value": 2}, {"value": 3}])
        fetch.assert_called_once_with(self.url + "&start=2", iss.TIMEOUT)
        self.assertEqual(first_cache.read_bytes(), before)

    def test_successful_empty_page_is_valid_end(self):
        with patch.object(iss, "get", side_effect=[table([1, 2]), table([])]):
            self.assertEqual(iss.paged(self.url, "history", page=2), [{"value": 1}, {"value": 2}])

    def test_incomplete_or_missing_table_raises(self):
        for payload in ({}, table([], 3), table([1], 3), {"history": {"columns": ["value"], "data": [[1, 2]]}}):
            with self.subTest(payload=payload), patch.object(iss, "get", return_value=payload):
                with self.assertRaises(iss.SourceUnavailable):
                    iss.paged(self.url, "history", page=2)

    def test_invalid_json_does_not_replace_existing_cache(self):
        path = iss._path(self.url)
        path.write_text(json.dumps(table([1])))
        before = path.read_bytes()
        with patch.object(iss.market, "_get", return_value=Mock(json=Mock(side_effect=ValueError("HTML")))):
            with self.assertRaises(iss.SourceUnavailable):
                iss.get(self.url, refresh=True)
        self.assertEqual(path.read_bytes(), before)
