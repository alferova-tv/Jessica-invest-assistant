import unittest

from jessica.sources.news import company_markers, mentions, silent


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
        self.assertIn("алиум", company_markers("АЛИУМ01Р1", "Алиум")["abbr"])

    def test_government_bond_has_no_markers(self):
        markers = company_markers("ОФЗ 26253", "Министерство финансов Российской Федерации")
        self.assertEqual(markers, {"stems": set(), "abbr": set()})

    def test_government_is_recognised_by_api_issuer_name(self):
        # Так эмитента ОФЗ называет T-Invest API; слова «ОФЗ» в названии может и не быть.
        self.assertEqual(company_markers("Выпуск 26253", "Правительство РФ"), {"stems": set(), "abbr": set()})


class MentionsTest(unittest.TestCase):
    def test_ozon_forms_and_latin_name(self):
        for name in ("Озон", "Ozon", "OZON"):
            markers = company_markers(name)
            self.assertNotIn("озо", markers["stems"])
            for form in ("Озон", "Озона", "Озону", "Озоном", "Озоне", "Ozon", "OZON"):
                with self.subTest(name=name, form=form):
                    self.assertTrue(mentions(f"Новость о компании «{form}»", markers))

    def test_ozon_does_not_match_forecast_or_similar_words(self):
        markers = company_markers("Озон БО-01", 'ООО «Озон»')
        for title in ("АКРА подтвердило рейтинг другой компании со стабильным прогнозом",
                      "Прогнозы аналитиков", "Озоновый слой", "Ozone layer", "ОзонФарма опубликовала результаты"):
            with self.subTest(title=title):
                self.assertFalse(mentions(title, markers))

    def test_other_short_names_remain_whole_words(self):
        markers = company_markers("Лента")
        self.assertTrue(mentions("«Лента» опубликовала результаты", markers))
        self.assertFalse(mentions("Новость о талентах сотрудников", markers))
        self.assertNotIn("лент", markers["stems"])

    def test_long_stems_match_declensions_only_at_word_start(self):
        markers = company_markers("Татнефть")
        for title in ("Татнефть опубликовала результаты", "Рейтинг Татнефти подтверждён"):
            self.assertTrue(mentions(title, markers))
        self.assertFalse(mentions("Новость о Нетатнефти", markers))

    def test_abbreviations_match_whole_words(self):
        markers = company_markers("РЖД")
        self.assertTrue(mentions("РЖД разместили облигации", markers))
        self.assertTrue(mentions("Новость о ржд", markers))
        self.assertFalse(mentions("ЧужоеРЖД разместило облигации", markers))


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
