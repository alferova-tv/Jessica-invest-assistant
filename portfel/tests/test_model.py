"""Учётные правила на синтетических операциях, без сети и без настоящих данных."""

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from portfel import db, model
from portfel.importers import alfa


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PORTFEL_HOME"] = self.tmp.name
        os.environ["JESSICA_HOME"] = self.tmp.name  # без holdings.json — сверка пустая
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.upsert_security(self.conn, {"isin": "RU000A000001", "name": "Бумага", "kind": "bond", "facevalue": 1000})
        self.conn.execute("INSERT INTO quotes (isin, price, accrued, date, source) VALUES ('RU000A000001', 990, 5, '2026-10-01', 'test')")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def op(self, date, kind, quantity=0, amount=0, accrued=0, fee=0):
        db.upsert_operation(self.conn, {"portfolio": "p", "source": "t", "ext_id": f"{date}{kind}{amount}",
                                        "date": date, "kind": kind, "isin": "RU000A000001",
                                        "quantity": quantity, "amount": amount, "accrued": accrued, "fee": fee})

    def test_average_cost_and_realized(self):
        # 10 шт. по 950 и 10 по 1000 (+НКД), продаём 5 по 1010 (+НКД 4 на штуку).
        self.op("2026-01-10", db.BUY, 10, -(9500 + 30), 30, 3)
        self.op("2026-02-10", db.BUY, 10, -(10000 + 20), 20, 3)
        self.op("2026-03-10", db.SELL, 5, 5050 + 20, 20, 2)
        self.op("2026-03-20", db.COUPON, 0, 300)
        report = model.build(self.conn, today=dt.date(2026, 10, 1))
        p = report["positions"][0]
        self.assertAlmostEqual(p["quantity"], 15)
        self.assertAlmostEqual(p["cost"], 19500 * 15 / 20)
        self.assertAlmostEqual(p["realized"], 5050 - 975 * 5)
        # Прибыль = вся разница денег: стоимость с НКД + полученное − уплаченное.
        cash = -(9530 + 3) - (10020 + 3) + (5070 - 2) + 300
        self.assertAlmostEqual(p["profit"], 15 * (990 + 5) + cash)

    def test_amortization_returns_capital(self):
        self.op("2026-01-10", db.BUY, 10, -10000)
        self.op("2026-06-10", db.AMORTIZATION, 0, 3000)
        p = model.build(self.conn, today=dt.date(2026, 10, 1))["positions"][0]
        self.assertAlmostEqual(p["cost"], 7000)
        self.assertAlmostEqual(p["coupons"], 0)

    def test_opening_goes_first_on_its_day(self):
        # Продажа в день входящего остатка не должна упираться в пустую позицию.
        self.op("2026-09-01", db.SELL, 4, 4000)
        self.op("2026-09-01", db.OPENING, 10, -10000)
        p = model.build(self.conn, today=dt.date(2026, 10, 1))["positions"][0]
        self.assertAlmostEqual(p["quantity"], 6)
        self.assertNotIn("oversold", p["flags"])

    def test_xirr(self):
        rate = model.xirr([(dt.date(2025, 1, 1), -1000), (dt.date(2026, 1, 1), 1100)])
        self.assertAlmostEqual(rate, 0.10, places=4)


class AlfaCashTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(alfa.classify_cash("частичное погашение номинала 4-01-00916-R (облигации ООО \"СФО ТБ-4\")"), db.AMORTIZATION)
        self.assertEqual(alfa.classify_cash("погашение купона 4B02-01-55234-E-002P (облигации ПАО \"Группа ЛСР\")"), db.COUPON)
        self.assertEqual(alfa.classify_cash("Дивиденды за 1 полугодие 2026 г. по акциям МКПАО \"ЯНДЕКС\""), db.DIVIDEND)

    def test_dividend_split_into_gross_and_tax(self):
        rows = [
            ["Дата", "Наименование операции", "Комментарий", ""],
            ["", "", "", "RUB"],
            ["2026-09-24 12:40:32", "Перевод",
             "Дивиденды за 1 полугодие 2026 г. по акциям МКПАО \"ЯНДЕКС\" (1-01-16777-A). (Удержан налог 14 руб.)", "96.0"],
        ]
        ops, unknown = alfa.parse_cash(rows, lambda reg: "RU000A107T19")
        self.assertEqual([(o["kind"], o["amount"]) for o in ops], [(db.DIVIDEND, 110.0), (db.TAX, -14.0)])
        self.assertEqual(unknown, [])


if __name__ == "__main__":
    unittest.main()
