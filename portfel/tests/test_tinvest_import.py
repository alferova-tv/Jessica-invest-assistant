import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from portfel import db, sync
from portfel.importers import tinvest


class TinvestImportTest(unittest.TestCase):
    def test_failed_instrument_lookup_preserves_entire_previous_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "test.db")
            self.addCleanup(conn.close)
            old = {"portfolio": "tinkoff", "source": "tinvest", "ext_id": "old",
                   "date": "2026-01-01", "kind": db.BUY, "isin": "TEST", "quantity": 1, "amount": -1000}
            db.upsert_operation(conn, old)
            before = [tuple(r) for r in conn.execute("SELECT * FROM operations")]
            item = {"id": "new", "date": "2026-02-01", "type": "OPERATION_TYPE_BUY",
                    "instrumentUid": "missing", "quantity": 1, "payment": {"units": -1000}}
            client = Mock()
            client._post.return_value = {"items": [item], "hasNext": False}
            report = {"added": {}, "silent": set(), "unknown": []}
            with patch.object(tinvest, "_client", return_value=(client, {}, "account")), \
                    patch.object(tinvest, "_instrument", return_value=None), \
                    patch.object(tinvest.paths, "jessica_state", return_value=Path(tmp) / "absent.json"):
                sync.import_tinvest(conn, report)
            self.assertEqual(before, [tuple(r) for r in conn.execute("SELECT * FROM operations")])
            self.assertTrue(report["silent"])

    def test_security_operations_require_isin(self):
        for operation in ("BUY", "SELL", "COUPON", "DIVIDEND", "BOND_REPAYMENT", "BOND_REPAYMENT_FULL"):
            with self.subTest(operation=operation), self.assertRaises(tinvest.Unavailable):
                tinvest.convert({"id": "test", "type": "OPERATION_TYPE_" + operation}, None)

    def test_cash_deposit_does_not_require_isin(self):
        op, reason = tinvest.convert({"id": "cash", "date": "2026-01-01", "type": "OPERATION_TYPE_INPUT",
                                     "payment": {"units": 1000}}, None)
        self.assertIsNone(reason)
        self.assertEqual(op["amount"], 1000)

    def test_valid_instrument_replaces_previous_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "test.db")
            self.addCleanup(conn.close)
            old = {"portfolio": "tinkoff", "source": "tinvest", "ext_id": "old",
                   "date": "2026-01-01", "kind": db.BUY, "isin": "TEST", "quantity": 1, "amount": -1000}
            db.upsert_operation(conn, old)
            op, _ = tinvest.convert({"id": "fresh", "date": "2026-02-01", "type": "OPERATION_TYPE_BUY",
                                     "quantity": 2, "payment": {"units": -2000}}, {"isin": "TEST"})
            with patch.object(tinvest, "load", return_value={"operations": [op], "unknown": []}):
                sync.import_tinvest(conn, {"added": {}, "silent": set(), "unknown": []})
            row, = conn.execute("SELECT ext_id, isin, quantity FROM operations").fetchall()
            self.assertEqual(tuple(row), ("fresh", "TEST", 2))
