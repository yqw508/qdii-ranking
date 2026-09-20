import unittest
import json
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import update_qdii_ranking as ranking

from tests.python.support.ranking import sample_exchange_premium

class ExchangePremiumTests(unittest.TestCase):
    @staticmethod
    def entry():
        return {
            "code": "513500",
            "name": "标普500ETF博时",
            "exchange": "SSE",
            "market_id": 1,
            "category": "broad_market",
            "benchmark_group": "标普500",
            "source_url": "https://example.test",
        }

    @staticmethod
    def holding_cost(value=0.8):
        return {
            "status": "parsed",
            "annualized_pct": value,
            "measurement_date": None,
            "source_title": "标普500ETF博时基金产品资料概要更新",
            "source_published_date": "2026-08-18",
            "source_url": "https://example.test/summary.pdf",
        }

    @staticmethod
    def announcement_cache(summary_id="summary"):
        record = ranking.AnnouncementRecord(
            summary_id,
            "标普500ETF博时基金产品资料概要更新",
            date(2026, 8, 18),
        )

        class Cache:
            def get(self, _client, code, as_of):
                return ranking.FundAnnouncementSnapshot(code, as_of, (record,))

        return Cache()

    def test_catalog_contains_the_expected_twenty_five_unique_etfs(self):
        entries, fingerprint = ranking.load_exchange_premium_catalog(
            ranking.DEFAULT_US_EQUITY_ETF_CATALOG
        )
        self.assertEqual(25, len(entries))
        self.assertEqual(25, len({entry["code"] for entry in entries}))
        self.assertEqual(64, len(fingerprint))
        self.assertEqual(
            set(ranking.ETF_PREMIUM_GROUP_ORDER),
            {entry["benchmark_group"] for entry in entries},
        )

    def test_dynamic_catalog_paginates_and_filters_to_qdii_scope(self):
        rows = [
            {"f12": "159100", "f13": 0, "f14": "巴西ETF华夏"},
            {"f12": "510300", "f13": 1, "f14": "沪深300ETF"},
            {"f12": "160125", "f13": 0, "f14": "南方香港LOF"},
        ]

        class Client:
            def __init__(self):
                self.pages = []

            def get_json(self, url, **_kwargs):
                page = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["pn"][0])
                self.pages.append(page)
                start = (page - 1) * 2
                return {"data": {"total": len(rows), "diff": rows[start : start + 2]}}

        metadata = {
            "159100": {"code": "159100", "name": "巴西ETF华夏", "fund_type": "指数型-海外股票"},
            "510300": {"code": "510300", "name": "沪深300ETF", "fund_type": "指数型-股票"},
            "160125": {"code": "160125", "name": "南方香港LOF", "fund_type": "QDII-普通股票"},
        }
        client = Client()
        with patch("qdii_ranking.sources.premium.ETF_MARKET_LIST_PAGE_SIZE", 2):
            entries, quote_rows, fingerprint = ranking.load_qdii_exchange_premium_catalog(
                client, metadata
            )
        self.assertEqual([1, 2], client.pages)
        self.assertEqual(["159100", "160125"], [item["code"] for item in entries])
        self.assertEqual(set(quote_rows), {"159100", "160125"})
        self.assertEqual(64, len(fingerprint))
        self.assertTrue(all(item["category"] == "qdii" for item in entries))
        self.assertEqual(
            "exchange_premium",
            ranking.HttpClient._category(ranking.exchange_premium_market_url()),
        )

    def test_dynamic_snapshot_filters_records_without_quotes(self):
        base_entry = {
            "name": "场内QDII",
            "exchange": "SSE",
            "market_id": 1,
            "category": "qdii",
            "benchmark_group": "指数型-海外股票",
            "fund_type": "指数型-海外股票",
            "source_url": "https://example.test",
        }
        entries = [
            {**base_entry, "code": "513500"},
            {**base_entry, "code": "161125", "name": "标普500LOF"},
            {**base_entry, "code": "513501"},
        ]
        valid_quote = {
            "f2": 2.672,
            "f3": -0.11,
            "f6": 228746612,
            "f12": "513500",
            "f14": "场内QDII",
            "f124": 1787299916,
            "f297": 20260821,
            "f402": -9.09,
            "f441": 2.4493,
        }
        unavailable_quote = {
            **valid_quote,
            "f12": "513501",
            "f2": "-",
            "f402": "-",
            "f441": "-",
        }

        lof_quote = {
            **valid_quote,
            "f2": 3.252,
            "f12": "161125",
            "f14": "标普500LOF",
            "f402": -3.25,
            "f441": "-",
        }

        class Client:
            def get_json(self, url, **_kwargs):
                self.url = url
                return {
                    "Data": {
                        "LSJZList": [{"FSRQ": "2026-08-20", "DWJZ": "3.1496"}]
                    }
                }

        client = Client()
        with TemporaryDirectory() as directory:
            section, warnings = ranking.build_exchange_premium_snapshot(
                client,
                ranking.DEFAULT_US_EQUITY_ETF_CATALOG,
                Path(directory) / "exchange-premium.json",
                date(2026, 8, 21),
                catalog_entries=entries,
                quote_rows={
                    "513500": valid_quote,
                    "161125": lof_quote,
                    "513501": unavailable_quote,
                },
                catalog_fingerprint="a" * 64,
            )
        self.assertEqual([], warnings)
        self.assertEqual(3, section["discovered_count"])
        self.assertEqual(1, section["filtered_unavailable_count"])
        self.assertEqual(2, section["expected_count"])
        self.assertEqual("fresh", section["status"])
        records = {record["code"]: record for record in section["records"]}
        self.assertEqual({"513500", "161125"}, set(records))
        self.assertEqual("nav", records["161125"]["reference_value_type"])
        self.assertEqual(3.1496, records["161125"]["reference_value_cny"])
        self.assertEqual(3.25, records["161125"]["premium_pct"])
        self.assertIn("fundCode=161125", client.url)

    def test_premium_sort_is_descending_with_missing_quotes_last(self):
        records = [
            {"code": "000003", "premium_pct": None},
            {"code": "000002", "premium_pct": 2.0},
            {"code": "000001", "premium_pct": 8.0},
        ]
        records.sort(key=ranking.exchange_premium_sort_key)
        self.assertEqual(["000001", "000002", "000003"], [item["code"] for item in records])

    def test_normalizes_discount_as_premium_and_checks_price_iopv(self):
        entry = self.entry()
        raw = {
            "f2": 2.672,
            "f3": -0.11,
            "f6": 228746612,
            "f12": "513500",
            "f14": "标普500ETF博时",
            "f124": 1787299916,
            "f297": 20260821,
            "f402": -9.09,
            "f441": 2.4493,
        }
        quote = ranking.normalize_exchange_premium_quote(
            raw, entry, date(2026, 8, 21)
        )
        self.assertEqual(9.09, quote["premium_pct"])
        with self.assertRaisesRegex(ValueError, "future data"):
            ranking.normalize_exchange_premium_quote(raw, entry, date(2026, 8, 20))
        with self.assertRaisesRegex(ValueError, "differs from price/IOPV"):
            ranking.normalize_exchange_premium_quote(
                {**raw, "f402": -1}, entry, date(2026, 8, 21)
            )

    def test_parses_and_uses_latest_lof_nav_reference(self):
        reference = ranking.parse_exchange_premium_lof_nav(
            {
                "Data": {
                    "LSJZList": [{"FSRQ": "2026-08-20", "DWJZ": "3.1496"}]
                }
            },
            "161125",
            date(2026, 8, 21),
        )
        self.assertEqual(
            {
                "reference_value_type": "nav",
                "reference_value_cny": 3.1496,
                "reference_value_date": "2026-08-20",
                "reference_value_source_url": ranking.exchange_premium_lof_nav_url("161125"),
            },
            reference,
        )
        raw = {
            "f2": 3.252,
            "f3": 0.4,
            "f6": 1234567,
            "f12": "161125",
            "f14": "标普500LOF",
            "f124": 1787299916,
            "f297": 20260821,
            "f402": -3.25,
            "f441": "-",
        }
        quote = ranking.normalize_exchange_premium_quote(
            raw,
            {
                **self.entry(),
                "code": "161125",
                "name": "标普500LOF",
                "category": "qdii",
                "benchmark_group": "QDII-指数",
            },
            date(2026, 8, 21),
            reference,
        )
        self.assertIsNone(quote["iopv_cny"])
        self.assertEqual("nav", quote["reference_value_type"])
        self.assertEqual(3.1496, quote["reference_value_cny"])
        self.assertEqual(3.25, quote["premium_pct"])

    def test_rejects_future_or_invalid_lof_nav_reference(self):
        for row in (
            {"FSRQ": "2026-08-22", "DWJZ": "3.1496"},
            {"FSRQ": "2026-08-20", "DWJZ": "0"},
        ):
            with self.subTest(row=row), self.assertRaises(ranking.DataError):
                ranking.parse_exchange_premium_lof_nav(
                    {"Data": {"LSJZList": [row]}},
                    "161125",
                    date(2026, 8, 21),
                )

    def test_holding_cost_cache_hit_avoids_reparsing_unchanged_summary(self):
        class DocumentCache:
            def get_text(self, *_args):
                raise AssertionError("unchanged summary should use the parsed cache")

        with TemporaryDirectory() as directory:
            result_cache = ranking.ExchangePremiumHoldingCostCache(Path(directory))
            result_cache.save("513500", "summary", self.holding_cost())
            cost, warnings = ranking.resolve_exchange_premium_holding_cost(
                object(),
                self.entry(),
                date(2026, 8, 20),
                self.announcement_cache(),
                DocumentCache(),
                result_cache,
            )
        self.assertEqual("parsed", cost["status"])
        self.assertEqual(0.8, cost["annualized_pct"])
        self.assertEqual([], warnings)
        self.assertEqual(1, result_cache.stats()["hits"])

    def test_new_unparseable_summary_does_not_fall_back_to_old_fee(self):
        class DocumentCache:
            def get_text(self, *_args):
                return "最新概要未披露可解析的综合费率"

        with TemporaryDirectory() as directory:
            result_cache = ranking.ExchangePremiumHoldingCostCache(Path(directory))
            result_cache.save("513500", "old-summary", self.holding_cost())
            cost, warnings = ranking.resolve_exchange_premium_holding_cost(
                object(),
                self.entry(),
                date(2026, 8, 20),
                self.announcement_cache("new-summary"),
                DocumentCache(),
                result_cache,
            )
            cached = result_cache.load("513500", date(2026, 8, 20))
        self.assertEqual("unavailable", cost["status"])
        self.assertIsNone(cost["annualized_pct"])
        self.assertTrue(any("最新产品概要无法解析" in warning for warning in warnings))
        self.assertEqual("new-summary", cached[0])
        self.assertEqual("unavailable", cached[1]["status"])

    def test_announcement_outage_uses_only_a_valid_last_fee(self):
        class FailingAnnouncementCache:
            def get(self, *_args):
                raise ranking.DataError("temporary outage")

        with TemporaryDirectory() as directory:
            result_cache = ranking.ExchangePremiumHoldingCostCache(Path(directory))
            result_cache.save("513500", "summary", self.holding_cost())
            stale, stale_warnings = ranking.resolve_exchange_premium_holding_cost(
                object(),
                self.entry(),
                date(2026, 8, 20),
                FailingAnnouncementCache(),
                object(),
                result_cache,
            )
            empty_cache = ranking.ExchangePremiumHoldingCostCache(
                Path(directory) / "empty"
            )
            unavailable, unavailable_warnings = ranking.resolve_exchange_premium_holding_cost(
                object(),
                self.entry(),
                date(2026, 8, 20),
                FailingAnnouncementCache(),
                object(),
                empty_cache,
            )
        self.assertEqual("stale", stale["status"])
        self.assertEqual(0.8, stale["annualized_pct"])
        self.assertTrue(any("使用上次费率" in warning for warning in stale_warnings))
        self.assertEqual("unavailable", unavailable["status"])
        self.assertTrue(any("没有可用旧值" in warning for warning in unavailable_warnings))

    def test_source_failure_uses_valid_cache_without_blocking(self):
        class FailingClient:
            def get_json(self, *_args, **_kwargs):
                raise ranking.DataError("temporary quote outage")

        sample = sample_exchange_premium("2026-08-20")
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "exchange-premium.json"
            ranking.write_json(
                cache_path,
                {
                    "schema_version": ranking.ETF_PREMIUM_CACHE_SCHEMA_VERSION,
                    "records": sample["records"],
                },
            )
            section, warnings = ranking.build_exchange_premium_snapshot(
                FailingClient(),
                ranking.DEFAULT_US_EQUITY_ETF_CATALOG,
                cache_path,
                date(2026, 8, 20),
            )
        self.assertEqual("stale", section["status"])
        self.assertEqual(25, section["cache_hit_count"])
        self.assertTrue(all(item["quote_status"] == "stale" for item in section["records"]))
        self.assertTrue(warnings[0].startswith("场内溢价告警："))

    def test_cold_source_failure_keeps_catalog_with_empty_quotes(self):
        class FailingClient:
            def get_json(self, *_args, **_kwargs):
                raise ranking.DataError("temporary quote outage")

        with TemporaryDirectory() as directory:
            section, _warnings = ranking.build_exchange_premium_snapshot(
                FailingClient(),
                ranking.DEFAULT_US_EQUITY_ETF_CATALOG,
                Path(directory) / "exchange-premium.json",
                date(2026, 8, 20),
            )
        self.assertEqual("unavailable", section["status"])
        self.assertEqual(25, len(section["records"]))
        self.assertTrue(all(item["premium_pct"] is None for item in section["records"]))
