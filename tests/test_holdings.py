import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jessica import holdings
from jessica.config import Config

RAW = {
    "tinvest": {"token": "t.TEST"},
    "portfolios": {
        "tinkoff": {"title": "Т-Инвестиции", "source": "tinvest"},
        "alpha": {"title": "Альфа", "reports_dir": "alpha/reports"},
    },
}
BONDS = [
    {"uid": "uid-softline", "isin": "RU000A10EA08", "name": "Софтлайн 002Р-02",
     "issuer": 'ПАО "Софтлайн"', "code": "", "quantity": 20.0},
    {"uid": "uid-ofz", "isin": "RU000A10D517", "name": "ОФЗ 26253",
     "issuer": "Министерство финансов Российской Федерации", "code": "SU26253RMFS3", "quantity": 7.0},
]
PREVIOUS_BLOCK = {
    "title": "Т-Инвестиции", "source_file": None, "format": "tinvest-api",
    "synced": "2026-09-01T10:00:00", "stale": False,
    "positions": [{"isin": "RU000A10C8C0", "quantity": 30.0, "portfolio": "tinkoff",
                   "name": "РЖД", "kind": "bond", "issuer": "", "code": ""}],
    "skipped": [],
}


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "alpha" / "reports").mkdir(parents=True)
        self.config = Config(RAW, self.root)
        self.state = self.root / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, bonds):
        with mock.patch.object(holdings.tinvest, "fetch_bonds", return_value=bonds) as fetch:
            return holdings.collect(self.config), fetch

    def write(self, name, data):
        self.state.mkdir(exist_ok=True)
        (self.state / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_fresh_block_from_api(self):
        data, _ = self.collect(BONDS)
        block = data["portfolios"]["tinkoff"]
        self.assertEqual(block["format"], "tinvest-api")
        self.assertIsNone(block["source_file"])
        self.assertFalse(block["stale"])
        self.assertTrue(block["synced"])
        positions = {p["isin"]: p for p in block["positions"]}
        self.assertEqual(positions["RU000A10EA08"]["kind"], "bond")
        self.assertEqual(positions["RU000A10EA08"]["issuer"], 'ПАО "Софтлайн"')
        self.assertEqual(positions["RU000A10D517"]["code"], "SU26253RMFS3")
        self.assertNotIn("uid", positions["RU000A10EA08"])

    def test_reports_portfolio_is_untouched(self):
        data, _ = self.collect(BONDS)
        self.assertNotIn("stale", data["portfolios"]["alpha"])
        self.assertNotIn("synced", data["portfolios"]["alpha"])

    def test_cache_is_written_after_success(self):
        self.collect(BONDS)
        cache = json.loads((self.state / holdings.INSTRUMENTS).read_text(encoding="utf-8"))
        self.assertEqual(set(cache), {"uid-softline", "uid-ofz"})
        self.assertEqual(cache["uid-ofz"], {"isin": "RU000A10D517", "name": "ОФЗ 26253",
                                            "issuer": "Министерство финансов Российской Федерации",
                                            "code": "SU26253RMFS3"})

    def test_cache_is_passed_as_known(self):
        cached = {"uid-softline": {"isin": "RU000A10EA08", "name": "С", "issuer": "И", "code": ""}}
        self.write(holdings.INSTRUMENTS, cached)
        _, fetch = self.collect(BONDS)
        fetch.assert_called_once_with("t.TEST", account=None, known=cached)

    def test_failure_keeps_previous_block_and_cache(self):
        self.write("holdings.json", {"updated": "x", "portfolios": {"tinkoff": PREVIOUS_BLOCK}})
        cached = {"uid-x": {"isin": "RU000A10C8C0", "name": "РЖД", "issuer": "", "code": ""}}
        self.write(holdings.INSTRUMENTS, cached)
        data, _ = self.collect(None)
        block = data["portfolios"]["tinkoff"]
        self.assertTrue(block["stale"])
        self.assertEqual(block["synced"], "2026-09-01T10:00:00")
        self.assertEqual(block["positions"], PREVIOUS_BLOCK["positions"])
        cache = json.loads((self.state / holdings.INSTRUMENTS).read_text(encoding="utf-8"))
        self.assertEqual(cache, cached)

    def test_first_run_failure_gives_empty_stale_block(self):
        data, _ = self.collect(None)
        block = data["portfolios"]["tinkoff"]
        self.assertTrue(block["stale"])
        self.assertIsNone(block["synced"])
        self.assertEqual(block["positions"], [])


if __name__ == "__main__":
    unittest.main()
