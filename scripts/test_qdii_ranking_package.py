import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from io import BytesIO

from qdii_ranking.atomic import atomic_write_text
from qdii_ranking.cache.base import JsonFileCache
from qdii_ranking.config import RANKING_SCHEMA_VERSION
from qdii_ranking.contracts import ensure_schema
from qdii_ranking.pipeline import (
    RunMemo,
    apply_quota_gate,
    evaluate_batch,
    rank_candidates,
    route_candidates,
)
from qdii_ranking.renderers.json_renderer import render_json
from qdii_ranking.sources.candidates import is_rmb_a_share
from qdii_ranking.sources.holder import extract_page_count
from qdii_ranking.transport import HttpTransport, TransportError


class PackageContractTests(unittest.TestCase):
    def test_schema_contract_is_shared_by_renderer(self):
        payload = {"schema_version": RANKING_SCHEMA_VERSION, "records": []}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "latest.json"
            render_json(path, payload)
            self.assertEqual(payload, json.loads(path.read_text(encoding="utf-8")))

    def test_renderer_rejects_other_schema(self):
        with self.assertRaises(ValueError):
            ensure_schema({"schema_version": RANKING_SCHEMA_VERSION - 1})

    def test_atomic_writer_replaces_complete_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.txt"
            atomic_write_text(path, "complete")
            self.assertEqual("complete", path.read_text(encoding="utf-8"))
            self.assertFalse(path.with_suffix(".txt.tmp").exists())

    def test_run_memo_is_scoped_to_date_and_code(self):
        memo = RunMemo(date(2026, 9, 20))
        result = ({"two_year_return_pct": 1.0}, [])
        self.assertIs(memo.put_performance("000001", result), result)
        self.assertIs(memo.get_performance("000001"), result)
        self.assertIsNone(memo.get_performance("000002"))

    def test_evaluate_batch_preserves_stage_input_order(self):
        self.assertEqual(
            [1, 4, 9, 16],
            evaluate_batch([1, 2, 3, 4], lambda value: value * value, max_workers=2),
        )

    def test_routing_stage_returns_separate_immutable_pools(self):
        candidates = [
            {
                "code": "1",
                "name": "US",
                "nasdaq100_fit": {"correlation": 1.0},
                "_document_result": {
                    "exposure": {"confirmed_us_equity_pct": 60.0},
                    "exposure_warnings": ["interval"],
                },
            },
            {
                "code": "2",
                "name": "Global",
                "_document_result": {
                    "exposure": {"confirmed_us_equity_pct": 10.0},
                    "exposure_warnings": [],
                },
            },
        ]
        routed = route_candidates(
            candidates,
            50.0,
            [],
            lambda name, exposure, threshold, keywords: (
                ("us_main", "confirmed")
                if exposure["confirmed_us_equity_pct"] >= threshold
                else ("global_supplement", "below")
            ),
        )
        self.assertEqual(["1"], [item["code"] for item in routed.us_main])
        self.assertEqual(["2"], [item["code"] for item in routed.global_supplement])
        self.assertEqual(("1 interval",), routed.warnings)

    def test_quota_and_ranking_stages_preserve_missing_and_cap_reasons(self):
        unresolved = {
            "code": "2",
            "_document_result": {
                "quota": None,
                "quota_warnings": [],
                "quota_error": "missing",
            },
        }
        eligible = {
            "code": "1",
            "three_year_return_pct": 30.0,
            "three_year_max_drawdown_pct": -10.0,
            "_document_result": {
                "quota": {"direct_limit": {"status": "limited", "amount_cny": 200}},
                "quota_warnings": [],
                "quota_error": None,
            },
        }
        quota = apply_quota_gate(
            [unresolved, eligible],
            200,
            lambda limit, minimum: limit["amount_cny"] >= minimum,
        )
        self.assertEqual(["1"], [item["code"] for item in quota.qualified])
        self.assertEqual("quota_unresolved", quota.exclusions[0][0])
        ranked = rank_candidates(
            [eligible, {**eligible, "code": "3"}],
            [eligible],
            1,
            lambda item: item["code"],
            lambda item: item["code"],
            lambda item: (3.0, 9.0),
        )
        self.assertEqual(["1"], [item["code"] for item in ranked.us_ranked])
        self.assertEqual("ranking_cap", ranked.exclusions[0][0])
        self.assertNotIn("_return_drawdown_ratio", eligible)

    def test_candidate_share_rule_is_available_from_package(self):
        self.assertFalse(
            is_rmb_a_share(
                {
                    "name": "纳斯达克100ETF联接(QDII-LOF)C(人民币)",
                    "fund_type": "指数型-海外股票",
                }
            )
        )

    def test_holder_page_count_parser_is_self_contained(self):
        self.assertEqual(3, extract_page_count('pages:"3"'))

    def test_json_cache_distinguishes_missing_and_corrupt_entries(self):
        with TemporaryDirectory() as directory:
            cache = JsonFileCache(Path(directory))
            self.assertIsNone(cache.load("missing"))
            Path(directory, "broken.json").write_text("{", encoding="utf-8")
            self.assertIsNone(cache.load("broken"))
            cache.save("valid", {"value": 1})
            self.assertEqual({"value": 1}, cache.load("valid"))
            self.assertEqual(2, cache.stats.misses)
            self.assertEqual(1, cache.stats.corrupt_rebuilds)
            self.assertEqual(1, cache.stats.hits)

    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_transport_retries_and_exposes_conditional_metrics(self, urlopen):
        response = type("Response", (), {
            "status": 200,
            "headers": {"Last-Modified": "today"},
            "read": lambda self: b"ok",
            "__enter__": lambda self: self,
            "__exit__": lambda self, *args: None,
        })()
        urlopen.side_effect = [URLError("temporary"), response]
        transport = HttpTransport(retries=2, timeout=1, user_agent="test")
        self.assertEqual(b"ok", transport.get_bytes("https://example.invalid"))
        metrics = transport.metrics_snapshot()["other"]
        self.assertEqual(1, metrics["calls"])
        self.assertEqual(2, metrics["attempts"])
        self.assertEqual(1, metrics["retries"])

    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_transport_handles_http_304(self, urlopen):
        headers = {"Last-Modified": "today"}
        error = HTTPError("https://example.invalid", 304, "not modified", headers, BytesIO())
        urlopen.side_effect = [error]
        transport = HttpTransport(retries=1, timeout=1, user_agent="test")
        status, body, last_modified = transport.get_conditional_text(
            "https://example.invalid", last_modified="yesterday"
        )
        self.assertEqual((304, None, "today"), (status, body, last_modified))
        self.assertEqual(1, transport.metrics_snapshot()["other"]["not_modified"])

    @patch("qdii_ranking.transport.urllib.request.urlopen")
    def test_transport_retries_invalid_post_json(self, urlopen):
        def response(body):
            return type("Response", (), {
                "status": 200,
                "headers": {},
                "read": lambda self: body,
                "__enter__": lambda self: self,
                "__exit__": lambda self, *args: None,
            })()

        urlopen.side_effect = [response(b"{"), response(b'{"ok": true}')]
        transport = HttpTransport(retries=2, timeout=1, user_agent="test")
        self.assertEqual(
            {"ok": True},
            transport.post_form_json("https://example.invalid", {"page": "1"}),
        )
        self.assertEqual(2, transport.metrics_snapshot()["other"]["attempts"])


if __name__ == "__main__":
    unittest.main()
