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


class UsEquityExposureTests(unittest.TestCase):
    def resolver(self, directory):
        return ranking.LookthroughResolver(
            ranking.DEFAULT_US_EQUITY_CATALOG, Path(directory) / "lookthrough.json"
        )

    def report(self):
        return ranking.PeriodicReport(
            "fixture",
            "fixture",
            date(2026, 6, 30),
            date(2026, 7, 21),
            "https://example.test/report.pdf",
        )

    def test_routes_only_confirmed_fifty_percent_to_us_main(self):
        self.assertEqual(
            "us_main",
            ranking.ranking_list_for_exposure(
                {"confirmed_pct": 50.0, "possible_pct": 50.0}, 50
            ),
        )
        self.assertEqual(
            "global_supplement",
            ranking.ranking_list_for_exposure(
                {"confirmed_pct": 49.99, "possible_pct": 80.0}, 50
            ),
        )

    def test_routing_reason_uses_geography_override_before_exposure(self):
        keywords = ["亚洲", "中国", "港"]
        self.assertEqual(
            ("us_main", "confirmed_us_exposure"),
            ranking.ranking_route(
                "全球科技精选(QDII)A", {"confirmed_pct": 50.0}, 50, keywords
            ),
        )
        self.assertEqual(
            ("global_supplement", "us_exposure_below_threshold"),
            ranking.ranking_route(
                "全球科技精选(QDII)A", {"confirmed_pct": 49.99}, 50, keywords
            ),
        )
        for name in (
            "国富亚洲机会股票(QDII)A",
            "富国中国精选混合(QDII)A",
            "大成港股精选混合(QDII)A",
        ):
            for confirmed in (49.0, 50.0, 80.0):
                with self.subTest(name=name, confirmed=confirmed):
                    self.assertEqual(
                        ("global_supplement", "us_main_name_geography_override"),
                        ranking.ranking_route(
                            name, {"confirmed_pct": confirmed}, 50, keywords
                        ),
                    )

    def test_chinese_instrument_names_are_preserved_for_catalog_matching(self):
        self.assertEqual(
            "华安纳斯达克100交易型开放式指数证券投资",
            ranking.normalize_instrument_name(
                "华安纳斯 达克100 交易型开 放式指数 证券投资"
            ),
        )
        self.assertNotEqual(
            ranking.normalize_instrument_name("华安纳斯达克100ETF"),
            ranking.normalize_instrument_name("大成纳斯达克100ETF"),
        )
        with TemporaryDirectory() as directory:
            resolver = self.resolver(directory)
            resolved = resolver.resolve(
                "华安纳斯 达克100 交易型开 放式指数 证券投资",
                date(2026, 6, 30),
            )
            gf_resolved = resolver.resolve("广发纳指 100ETF", date(2026, 6, 30))
            biotech_resolved = resolver.resolve(
                "纳指生物 科技 ETF 汇添富", date(2026, 6, 30)
            )
        self.assertIsNotNone(resolved)
        self.assertEqual(100.0, resolved["us_equity_pct"])
        self.assertIsNotNone(gf_resolved)
        self.assertEqual(100.0, gf_resolved["us_equity_pct"])
        self.assertIsNotNone(biotech_resolved)
        self.assertEqual(100.0, biotech_resolved["us_equity_pct"])

    def test_163813_conservative_lower_bound_exceeds_fifty(self):
        holdings = [
            ("INVESCO QQQ TRUST SERIES 1", 12.09),
            ("INVESCO SEMICONDUCTORS ETF", 11.82),
            ("PWR S&P 500 EQ WGT TECH", 9.04),
            ("INVESCO NASDAQ 100 ETF", 6.73),
            ("VANECK SEMICONDUCTOR ETF", 6.50),
            ("FIRST TRUST NASDQ 100 TECH I", 6.41),
            ("SPDR BBG BARC 1-3 MONTH TBIL", 5.01),
            ("VANGUARD TOT WORLD STK ETF", 3.76),
            ("TECHNOLOGY SELECT SECT SPDR", 3.26),
            ("FRK FTSE KOREA UCITS ETF", 2.29),
        ]
        parsed = {
            "direct_us_pct": 13.96,
            "fund_investment_pct": 72.4139,
            "fund_holdings": [
                {"rank": index, "fund_name": name, "weight_pct": weight}
                for index, (name, weight) in enumerate(holdings, start=1)
            ],
        }
        with TemporaryDirectory() as directory:
            exposure, warnings = ranking.calculate_us_equity_exposure(
                parsed, self.report(), self.resolver(directory), 50
            )
        self.assertEqual(69.81, exposure["confirmed_pct"])
        self.assertEqual("qualified", exposure["status"])
        self.assertGreater(exposure["possible_pct"], exposure["confirmed_pct"])
        self.assertTrue(warnings)

    def test_threshold_boundaries_and_ambiguous_interval(self):
        class UnknownResolver:
            def resolve(self, *_args):
                return None

        cases = [
            ({"direct_us_pct": 50.0, "fund_investment_pct": 0, "fund_holdings": []}, "qualified"),
            ({"direct_us_pct": 49.99, "fund_investment_pct": 0, "fund_holdings": []}, "excluded"),
            (
                {
                    "direct_us_pct": 49.99,
                    "fund_investment_pct": 1.0,
                    "fund_holdings": [
                        {"rank": 1, "fund_name": "UNKNOWN", "weight_pct": 1.0}
                    ],
                },
                "ambiguous",
            ),
        ]
        for parsed, expected in cases:
            exposure, _ = ranking.calculate_us_equity_exposure(
                parsed, self.report(), UnknownResolver(), 50
            )
            self.assertEqual(expected, exposure["status"])

    def test_global_numeric_allocation_must_be_historical_and_within_120_days(self):
        with TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "aliases": ["GLOBAL OLD ETF"],
                                "category": "global_equity",
                                "us_equity_pct": 62,
                                "data_date": "2026-03-01",
                                "source_url": "https://example.test/issuer",
                            },
                            {
                                "aliases": ["GLOBAL FRESH ETF"],
                                "category": "global_equity",
                                "us_equity_pct": 63,
                                "data_date": "2026-03-02",
                                "source_url": "https://example.test/issuer",
                            },
                            {
                                "aliases": ["GLOBAL FUTURE ETF"],
                                "category": "global_equity",
                                "us_equity_pct": 64,
                                "data_date": "2026-07-01",
                                "source_url": "https://example.test/issuer",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            resolver = ranking.LookthroughResolver(
                catalog_path, Path(directory) / "cache.json"
            )
            self.assertIsNone(resolver.resolve("GLOBAL OLD ETF", date(2026, 6, 30)))
            self.assertEqual(
                63,
                resolver.resolve("GLOBAL FRESH ETF", date(2026, 6, 30))["us_equity_pct"],
            )
            self.assertIsNone(
                resolver.resolve("GLOBAL FUTURE ETF", date(2026, 6, 30))
            )
            cached = ranking.LookthroughResolver(
                catalog_path, Path(directory) / "cache.json"
            )
            cached.resolve("GLOBAL FRESH ETF", date(2026, 6, 30))
            self.assertEqual({"hits": 1, "misses": 0}, cached.stats())

    @patch("qdii_ranking.cache.exposure.calculate_us_equity_exposure_base")
    @patch("qdii_ranking.cache.exposure.parse_us_equity_report")
    @patch("qdii_ranking.cache.exposure.fetch_latest_periodic_report")
    def test_fund_exposure_cache_reuses_base_result_and_invalidates_catalog(
        self, fetch_report, parse_report, calculate_base
    ):
        class ReportCache:
            calls = 0

            def get_text(self, *_args):
                self.calls += 1
                return "report text"

        report = self.report()
        fetch_report.return_value = report
        parse_report.return_value = {"parsed": True}
        base_exposure = {
            "confirmed_pct": 60.0,
            "possible_pct": 65.0,
            "direct_us_pct": 20.0,
            "lookthrough_confirmed_pct": 40.0,
            "unresolved_pct": 5.0,
            "report_date": report.report_date.isoformat(),
            "published_date": report.published_date.isoformat(),
            "source_url": report.source_url,
            "components": [],
        }
        calculate_base.return_value = (base_exposure, ["base warning"])
        fund = {"code": "000001", "fund_page_url": "https://example.test/fund"}
        as_of = date(2026, 8, 19)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            catalog = {"entries": []}
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            resolver = ranking.LookthroughResolver(catalog_path, root / "lookthrough.json")
            report_cache = ReportCache()
            cache = ranking.FundExposureResultCache(root / "fund-exposure")

            first, first_warnings = cache.get(
                object(), fund, as_of, report_cache, resolver, 50
            )
            second, second_warnings = cache.get(
                object(), fund, as_of, report_cache, resolver, 70
            )
            self.assertEqual("qualified", first["status"])
            self.assertEqual("excluded", second["status"])
            self.assertEqual(["base warning"], first_warnings)
            self.assertEqual(["base warning"], second_warnings)

            exposure_path = (
                root / "fund-exposure" / "000001" / f"{report.announcement_id}.json"
            )
            exposure_path.write_text("{broken", encoding="utf-8")
            cache.get(object(), fund, as_of, report_cache, resolver, 50)

            catalog["entries"].append(
                {
                    "aliases": ["NEW ETF"],
                    "category": "us_equity",
                    "us_equity_pct": 100,
                    "source_url": "https://example.test/issuer",
                }
            )
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            changed_resolver = ranking.LookthroughResolver(
                catalog_path, root / "lookthrough-changed.json"
            )
            cache.get(object(), fund, as_of, report_cache, changed_resolver, 50)

        self.assertEqual(3, report_cache.calls)
        self.assertEqual(3, calculate_base.call_count)
        self.assertEqual(
            {"hits": 1, "misses": 3, "corrupt_rebuilds": 2}, cache.stats()
        )

    @patch("qdii_ranking.cache.exposure.fetch_latest_periodic_report")
    def test_fund_exposure_cache_rejects_future_report(self, fetch_report):
        fetch_report.return_value = ranking.PeriodicReport(
            "future",
            "future",
            date(2026, 9, 30),
            date(2026, 10, 20),
            "https://example.test/future.pdf",
        )
        with TemporaryDirectory() as directory:
            resolver = self.resolver(directory)
            cache = ranking.FundExposureResultCache(Path(directory) / "fund-exposure")
            with self.assertRaises(ranking.DataError):
                cache.get(
                    object(),
                    {"code": "000001", "fund_page_url": "https://example.test/fund"},
                    date(2026, 8, 19),
                    object(),
                    resolver,
                    50,
                )

    @patch("qdii_ranking.ranking.fetch_us_equity_exposure")
    @patch("qdii_ranking.ranking.fetch_trailing_performance")
    def test_full_scan_does_not_stop_at_top_and_ranks_by_correlation(
        self, performance, exposure
    ):
        concurrency = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        def performance_result(_client, fund, _as_of, _benchmark):
            code = int(fund["code"])
            with lock:
                concurrency["active"] += 1
                concurrency["maximum"] = max(
                    concurrency["maximum"], concurrency["active"]
                )
            time.sleep(0.02)
            with lock:
                concurrency["active"] -= 1
            return {
                "three_year_return_pct": 60.0 + code,
                "nasdaq100_fit": {
                    "correlation": 0.80 + code / 1000,
                    "beta": 1.0,
                },
                "nasdaq100_fit_error": None,
            }, [f"performance {code}"]

        def exposure_result(_client, fund, *_args):
            code = int(fund["code"])
            confirmed = 99.0 if code == 11 else 50.0 + code
            return {
                "status": "qualified",
                "confirmed_pct": confirmed,
            }, []

        performance.side_effect = performance_result
        exposure.side_effect = exposure_result
        candidates = [
            {
                "code": str(index).zfill(2),
                "institution_holding_ratio_pct": 100 - index,
            }
            for index in range(12)
        ]
        (
            selected,
            warnings,
            performance_scanned,
            performance_qualified,
            exposure_scanned,
            exposure_qualified,
        ) = ranking.filter_performance_and_us_exposure_full_scan(
            object(),
            candidates,
            date(2026, 8, 19),
            50,
            50,
            10,
            object(),
            object(),
        )
        self.assertEqual("11", selected[0]["code"])
        self.assertEqual(10, len(selected))
        self.assertEqual(12, performance_scanned)
        self.assertEqual(12, performance_qualified)
        self.assertEqual(12, exposure_scanned)
        self.assertEqual(12, exposure_qualified)
        self.assertEqual(10, ranking.PERFORMANCE_WORKERS)
        self.assertEqual(10, concurrency["maximum"])
        self.assertEqual(
            [f"performance {index}" for index in range(12)], warnings
        )

    @patch("qdii_ranking.ranking.fetch_us_equity_exposure")
    @patch("qdii_ranking.ranking.fetch_trailing_performance")
    def test_qualified_fund_without_nasdaq_fit_blocks_ranking(
        self, performance, exposure
    ):
        performance.return_value = (
            {
                "three_year_return_pct": 60.0,
                "nasdaq100_fit": None,
                "nasdaq100_fit_error": "insufficient observations",
            },
            [],
        )
        exposure.return_value = (
            {"status": "qualified", "confirmed_pct": 80.0},
            [],
        )
        with self.assertRaisesRegex(ranking.DataError, "qualified fund 000001"):
            ranking.filter_performance_and_us_exposure_full_scan(
                object(),
                [{"code": "000001", "institution_holding_ratio_pct": 1.0}],
                date(2026, 8, 19),
                50,
                50,
                10,
                object(),
                object(),
            )

    @patch("qdii_ranking.ranking.fetch_us_equity_exposure")
    @patch("qdii_ranking.ranking.fetch_trailing_performance")
    def test_us_exposure_tie_breakers_are_deterministic(self, performance, exposure):
        performance_by_code = {"001": 100.0, "002": 90.0, "003": 90.0, "004": 80.0}
        exposure_by_code = {"001": 71.0, "002": 70.0, "003": 70.0, "004": 70.0}
        institution_by_code = {"001": 10.0, "002": 20.0, "003": 20.0, "004": 20.0}
        fit_by_code = {
            "001": {"correlation": 0.96, "beta": 1.25},
            "002": {"correlation": 0.95, "beta": 1.25},
            "003": {"correlation": 0.95, "beta": 1.0},
            "004": {"correlation": 0.95, "beta": 1.5},
        }
        performance.side_effect = lambda _client, fund, _as_of, _benchmark: (
            {
                "three_year_return_pct": performance_by_code[fund["code"]],
                "nasdaq100_fit": fit_by_code[fund["code"]],
                "nasdaq100_fit_error": None,
            },
            [],
        )
        exposure.side_effect = lambda _client, fund, *_args: (
            {
                "status": "qualified",
                "confirmed_pct": exposure_by_code[fund["code"]],
            },
            [],
        )
        candidates = [
            {
                "code": code,
                "institution_holding_ratio_pct": institution_by_code[code],
            }
            for code in ("004", "003", "002", "001")
        ]
        selected, *_ = ranking.filter_performance_and_us_exposure_full_scan(
            object(),
            candidates,
            date(2026, 8, 19),
            50,
            50,
            10,
            object(),
            object(),
        )
        self.assertEqual(["001", "003", "002", "004"], [item["code"] for item in selected])
