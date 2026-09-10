import tempfile
import unittest
from pathlib import Path

from jessica import holdings
from jessica.config import Config

OLD = {
    "portfolios": {
        "tinkoff": {"title": "Т-Инвестиции", "reports_dir": "tinkoff/reports"},
        "alpha": {"title": "Альфа", "reports_dir": "alpha/reports"},
    },
}
NEW = {
    "tinvest": {"token": "t.TEST", "account": "0000000001"},
    "portfolios": {
        "tinkoff": {"title": "Т-Инвестиции", "source": "tinvest"},
        "alpha": {"title": "Альфа", "reports_dir": "alpha/reports"},
    },
    "schedule": {"refresh_retry_hours": 6},
}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_config_keeps_reports(self):
        config = Config(OLD, self.root)
        tinkoff = config.portfolios[0]
        self.assertEqual(tinkoff.source, "reports")
        self.assertEqual(tinkoff.reports_dir, self.root / "tinkoff/reports")
        self.assertEqual(config.tinvest_token, "")
        self.assertIsNone(config.tinvest_account)
        self.assertEqual(config.refresh_retry_hours, 3)

    def test_tinvest_portfolio_has_no_reports_dir(self):
        config = Config(NEW, self.root)
        tinkoff, alpha = config.portfolios
        self.assertEqual(tinkoff.source, "tinvest")
        self.assertIsNone(tinkoff.reports_dir)
        self.assertIsNone(holdings.latest_report(tinkoff))
        self.assertEqual(alpha.source, "reports")
        self.assertEqual(config.tinvest_token, "t.TEST")
        self.assertEqual(config.tinvest_account, "0000000001")
        self.assertEqual(config.refresh_retry_hours, 6)


if __name__ == "__main__":
    unittest.main()
