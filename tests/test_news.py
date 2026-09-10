import unittest

from jessica.sources.news import company_markers, silent


class CompanyMarkersTest(unittest.TestCase):
    def test_gk_is_not_an_abbreviation(self):
        # «ГК» — организационная приставка: \bГК\b совпадает с любым «ГК ПИК».
        markers = company_markers("ГК Самолет БО-П20")
        self.assertNotIn("ГК", markers["abbr"])
        self.assertIn("самол", markers["stems"])

    def test_securitization_product_name_gives_no_stems(self):
        # Так API называет выпуск СФО ТБ-4; «кредитн» ловило бы «кредитный рейтинг».
        self.assertEqual(company_markers("Кредитный поток 2.0")["stems"], set())

    def test_glued_issue_code_is_rescued_by_issuer(self):
        # API пишет «АЛИУМ01Р1» слитно, и регэксп кодов съедает слово целиком.
        self.assertEqual(company_markers("АЛИУМ01Р1"), {"stems": set(), "abbr": set()})
        self.assertIn("алиу", company_markers("АЛИУМ01Р1", "Алиум")["stems"])

    def test_government_bond_has_no_markers(self):
        markers = company_markers("ОФЗ 26253", "Министерство финансов Российской Федерации")
        self.assertEqual(markers, {"stems": set(), "abbr": set()})


class SilentTest(unittest.TestCase):
    def test_company_without_markers_is_silent(self):
        self.assertTrue(silent("АЛИУМ01Р1"))

    def test_company_with_markers_is_not_silent(self):
        self.assertFalse(silent("АЛИУМ01Р1", "Алиум"))

    def test_government_bond_is_not_silent(self):
        self.assertFalse(silent("ОФЗ 26253"))

    def test_securitization_is_not_silent(self):
        self.assertFalse(silent("СФО Альфа Фабрика ПК-3 класс А"))
        self.assertFalse(silent("Кредитный поток 2.0", 'ООО "СФО ТБ-4"'))


if __name__ == "__main__":
    unittest.main()
