import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jessica import events
from jessica.config import Config
from jessica.sources import news, reports


def table(company="Софтлайн", date="09.11.2026", url="https://www.e-disclosure.ru/portal/FileLoad.ashx?Fileid=42", *, bank=False):
    ticker_header = "" if bank else "<th>Тикер</th>"
    ticker_cell = "" if bank else "<td>SOFL</td>"
    return f'''<table><tr><th>Название</th>{ticker_header}<th>Чистая прибыль</th>
    <th>отчет</th><th>Отчет</th><th>През-я</th><th>дата публикации</th></tr>
    <tr><td><a href="/forum/SOFL">{company}</a></td>{ticker_cell}<td>999999</td>
    <td>2026Q3-РСБУ</td><td><a title="Открыть файл отчета" href="{url}"></a></td>
    <td><a title="Открыть файл презентации" href="https://example.org/slides.pdf"></a></td>
    <td>{date}</td></tr></table>'''


class ReportsTest(unittest.TestCase):
    def test_both_table_layouts_and_document_not_presentation(self):
        for bank in (False, True):
            with self.subTest(bank=bank):
                row, = reports.parse(table(bank=bank))
                self.assertEqual(row["ticker"], "SOFL")
                self.assertEqual(row["period"], "2026Q3")
                self.assertEqual(row["standard"], "РСБУ")
                self.assertEqual(row["date"], "2026-11-09")
                self.assertEqual(row["document_key"], "e-disclosure:42")
                self.assertNotIn("999999", str(row))

    def test_missing_document_does_not_substitute_presentation(self):
        raw = table().replace('title="Открыть файл отчета"', 'title="Другое"')
        self.assertEqual(reports.parse(raw), [])

    def test_invalid_date_or_unsafe_url_is_skipped(self):
        self.assertEqual(reports.parse(table(date="нет даты")), [])
        self.assertEqual(reports.parse(table(url="javascript:alert(1)")), [])

    def test_duplicate_document_and_url_variants(self):
        raw = table() + table(url="http://e-disclosure.ru/portal/fileload.ashx?fileid=42")
        self.assertEqual(len(reports.parse(raw)), 1)

    def test_changed_markup_is_reported(self):
        with self.assertRaises(ValueError):
            reports.parse("<html>captcha</html>")
        with patch.object(news, "_download", return_value="captcha"), self.assertLogs(reports.log, "WARNING"):
            self.assertEqual(reports.fetch(), [])

    def test_rss_report_and_forecast_are_not_reporting_notifications(self):
        for source in ("smart-lab", "the-bell"):
            self.assertIsNone(news.classify("Софтлайн опубликовал отчётность по МСФО", source))
            self.assertIsNone(news.classify("Прибыль Софтлайна может вырасти — прогноз", source))
        self.assertEqual(news.classify("Софтлайн рекомендует дивиденды по итогам МСФО", "smart-lab"), "dividend")
        self.assertEqual(news.classify("Софтлайн · 2026Q3 · РСБУ", reports.SOURCE), "report")

    def test_report_source_survives_silent_rss(self):
        row, = reports.parse(table())
        with patch.object(news, "_download", return_value=""), patch.object(reports, "fetch", return_value=[row]):
            self.assertEqual(news.fetch(), [row])

    def test_rss_keeps_timestamp_and_author_for_comparison(self):
        raw = '''<item><title>Отчёт</title><link>https://example.org/blog/1</link>
        <pubDate>Mon, 09 Nov 2026 13:01:20 +0300</pubDate>
        <dc:creator>https://smart-lab.ru/my/disclosure/blog/</dc:creator></item>'''
        item, = news._parse(raw, "smart-lab")
        self.assertEqual(item["published_at"], "2026-11-09T13:01:20+03:00")
        self.assertEqual(item["author"], "https://smart-lab.ru/my/disclosure/blog/")


class ReportNotificationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config({}, Path(tmp.name))
        data = {"portfolios": {"alpha": {"title": "Альфа", "positions": [
            {"isin": "RU_TEST", "name": "Софтлайн 001Р-01", "issuer": "Софтлайн", "kind": "bond"},
        ]}}}
        self.config.state_file("holdings.json").write_text(json.dumps(data))
        self.row, = reports.parse(table())
        self.today = dt.date(2026, 11, 9)

    def test_message_contains_period_and_direct_link_without_profit(self):
        with patch.object(news, "fetch", return_value=[self.row, self.row]):
            text, keys = events.news_digest(self.config, self.today)
        self.assertIn("Софтлайн · 2026Q3 · РСБУ", text)
        self.assertIn('href="https://www.e-disclosure.ru/portal/FileLoad.ashx?Fileid=42"', text)
        self.assertIn("Открыть отчёт", text)
        self.assertNotIn("999999", text)
        self.assertNotIn("поиск", text)
        self.assertNotIn("🏛 раскрытие", text)
        self.assertEqual(keys, ["report|e-disclosure:42"])

    def test_only_portfolio_company_and_recent_documents(self):
        other = {**self.row, "company": "Другой эмитент"}
        old = {**self.row, "date": "2026-01-01"}
        with patch.object(news, "fetch", return_value=[other, old]):
            self.assertEqual(events.news_digest(self.config, self.today), ("", []))

    def test_dry_run_does_not_send_or_remember_then_sent_document_is_skipped(self):
        with patch.object(news, "fetch", return_value=[self.row]), patch("jessica.notify.send", return_value=True) as send:
            events.send_news(self.config, self.today, dry_run=True)
            send.assert_not_called()
            self.assertFalse(self.config.state_file("sent.json").exists())
            events.send_news(self.config, self.today)
            send.assert_called_once()
            self.assertEqual(events.news_digest(self.config, self.today), ("", []))

    def test_link_is_html_escaped(self):
        row = {**self.row, "url": "https://example.org/report?id=1&version=2"}
        with patch.object(news, "fetch", return_value=[row]):
            text, _ = events.news_digest(self.config, self.today)
        self.assertIn("?id=1&amp;version=2", text)
