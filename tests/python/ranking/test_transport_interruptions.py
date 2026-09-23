import unittest
from datetime import date
from http.client import IncompleteRead, RemoteDisconnected
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError

from qdii_ranking.cache.announcements import PeriodicReportCache
from qdii_ranking.cache.performance import PerformanceResultCache
from qdii_ranking.errors import DataError
from qdii_ranking.models import LegalDocument
from qdii_ranking.runtime import HttpClient
from qdii_ranking.transport import HttpTransport, TransportError


URL = "https://example.test/data"


def response(body=b"complete", error=None):
    item = MagicMock()
    item.__enter__.return_value = item
    item.status = 200
    item.headers = {"Last-Modified": "today"}
    item.read.return_value = body
    item.read.side_effect = error
    return item


class InterruptedTransportTests(unittest.TestCase):
    @patch("qdii_ranking.transport.time.sleep")
    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_get_retries_read_interruptions_and_returns_only_complete_body(self, urlopen, sleep):
        for error in (IncompleteRead(b"partial", 12), RemoteDisconnected("closed"), ConnectionResetError("reset")):
            with self.subTest(error=type(error).__name__):
                urlopen.reset_mock()
                sleep.reset_mock()
                broken, complete = response(error=error), response()
                urlopen.side_effect = [broken, complete]
                transport = HttpTransport()
                self.assertEqual("complete", transport.get_text(URL, referer=URL))
                self.assertEqual(2, urlopen.call_count)
                sleep.assert_called_once_with(0.5)
                broken.__exit__.assert_called_once()
                complete.__exit__.assert_called_once()
                metrics = transport.metrics_snapshot()["other"]
                self.assertEqual((1, 2, 1, 8), tuple(metrics[k] for k in ("calls", "attempts", "retries", "bytes")))
                for invocation in urlopen.call_args_list:
                    self.assertEqual(URL, invocation.args[0].get_header("Referer"))
                    self.assertEqual(30, invocation.kwargs["timeout"])

    @patch("qdii_ranking.transport.time.sleep")
    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_exhaustion_keeps_error_type_url_and_cause(self, urlopen, sleep):
        for transport, error_type in ((HttpClient(), DataError), (HttpTransport(), TransportError)):
            with self.subTest(error_type=error_type):
                urlopen.reset_mock()
                sleep.reset_mock()
                error = IncompleteRead(b"partial", 120574)
                urlopen.side_effect = lambda *_a, **_k: response(error=error)
                with self.assertRaises(error_type) as caught:
                    transport.get_bytes(URL)
                self.assertIs(error, caught.exception.__cause__)
                self.assertIn(URL, str(caught.exception))
                self.assertIn("IncompleteRead", str(caught.exception))
                self.assertEqual(4, urlopen.call_count)
                self.assertEqual([call(0.5), call(1.0), call(2.0)], sleep.call_args_list)
                self.assertEqual(4, transport.metrics_snapshot()["other"]["attempts"])
                self.assertEqual(3, transport.metrics_snapshot()["other"]["retries"])

    @patch("qdii_ranking.transport.time.sleep")
    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_conditional_retry_preserves_headers_and_304_contract(self, urlopen, sleep):
        urlopen.side_effect = [
            response(error=IncompleteRead(b"bad", 10)),
            HTTPError(URL, 304, "not modified", {"Last-Modified": "yesterday"}, BytesIO()),
        ]
        transport = HttpTransport()
        self.assertEqual((304, None, "yesterday"), transport.get_conditional_text(URL, last_modified="yesterday"))
        for invocation in urlopen.call_args_list:
            self.assertEqual("yesterday", invocation.args[0].get_header("If-modified-since"))
        metrics = transport.metrics_snapshot()["other"]
        self.assertEqual((2, 1, 1), tuple(metrics[k] for k in ("attempts", "retries", "not_modified")))

    @patch("qdii_ranking.transport.time.sleep")
    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_query_post_retries_without_changing_form(self, urlopen, sleep):
        for error in (IncompleteRead(b'{"wrong":true}', 1), RemoteDisconnected("closed"), ConnectionAbortedError("closed")):
            with self.subTest(error=type(error).__name__):
                urlopen.reset_mock()
                urlopen.side_effect = [response(error=error), response(b'{"ok":true}')]
                transport = HttpTransport()
                self.assertEqual({"ok": True}, transport.post_form_json(URL, {"page": "1"}, referer=URL))
                for invocation in urlopen.call_args_list:
                    request = invocation.args[0]
                    self.assertEqual("POST", request.get_method())
                    self.assertEqual(b"page=1", request.data)
                    self.assertEqual(URL, request.get_header("Referer"))
                self.assertEqual(2, transport.metrics_snapshot()["other"]["attempts"])

    @patch("qdii_ranking.transport.time.sleep")
    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_query_post_exhaustion_is_bounded(self, urlopen, sleep):
        error = IncompleteRead(b"{", 120574)
        urlopen.side_effect = lambda *_a, **_k: response(error=error)
        transport = HttpClient()
        with self.assertRaises(DataError) as caught:
            transport.post_form_json(URL, {"page": "1"})
        self.assertIs(error, caught.exception.__cause__)
        self.assertEqual(4, urlopen.call_count)
        self.assertEqual([call(0.5), call(1.0), call(2.0)], sleep.call_args_list)
        self.assertEqual(3, transport.metrics_snapshot()["other"]["retries"])

    @patch("qdii_ranking.transport.time.sleep")
    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_exhausted_download_does_not_write_pdf_or_nav_cache(self, urlopen, sleep):
        urlopen.side_effect = lambda *_a, **_k: response(error=IncompleteRead(b"partial", 100))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            doc = LegalDocument("fixture", "summary", date(2026, 9, 23), URL, "product_summary")
            with patch("qdii_ranking.cache.announcements._extract_pdf_text") as extract:
                with self.assertRaises(DataError):
                    PeriodicReportCache(root / "pdf").get_text(HttpClient(), doc, URL)
                extract.assert_not_called()
            with patch("qdii_ranking.cache.performance._parse_performance_page") as parse:
                with self.assertRaises(DataError):
                    PerformanceResultCache(root / "nav").get(
                        HttpClient(), {"code": "000001", "fund_page_url": URL}, date(2026, 9, 23), object(),
                    )
                parse.assert_not_called()
            self.assertEqual([], [p for p in root.rglob("*") if p.is_file()])

    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_programming_errors_are_not_retried(self, urlopen):
        for method in (lambda c: c.get_text(URL), lambda c: c.post_form_json(URL, {})):
            urlopen.reset_mock()
            urlopen.side_effect = TypeError("programming error")
            with self.assertRaises(TypeError):
                method(HttpTransport())
            self.assertEqual(1, urlopen.call_count)

    @patch("qdii_ranking.transport.time.sleep")
    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_failed_revalidation_preserves_existing_cache_without_using_it(self, urlopen, sleep):
        urlopen.side_effect = lambda *_a, **_k: response(error=IncompleteRead(b"partial", 100))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            nav_cache = PerformanceResultCache(root / "nav")
            nav_path = root / "nav/nav-history/000001.json"
            nav_cache._save(nav_path, "000001", "yesterday", [
                {"date": date(2026, 9, 22), "nav": 1.0, "equity_return_pct": None, "unit_money": ""},
            ])
            original_nav = nav_path.read_bytes()
            with patch("qdii_ranking.cache.performance._calculate_performance_from_points") as calculate:
                with self.assertRaises(DataError):
                    nav_cache.get(HttpClient(), {"code": "000001", "fund_page_url": URL}, date(2026, 9, 23), object())
                calculate.assert_not_called()
            self.assertEqual(original_nav, nav_path.read_bytes())
            self.assertIsNone(nav_cache.loaded_points("000001"))
            pdf_path = root / "pdf/fixture.pdf"
            pdf_path.parent.mkdir()
            original_pdf = b"%PDF-" + b"existing validated bytes" * 100
            pdf_path.write_bytes(original_pdf)
            doc = LegalDocument("fixture", "summary", date(2026, 9, 23), URL, "product_summary")
            with self.assertRaises(DataError):
                PeriodicReportCache(pdf_path.parent).get_text(HttpClient(), doc, URL, force_refresh=True)
            self.assertEqual(original_pdf, pdf_path.read_bytes())
