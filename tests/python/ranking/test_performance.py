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


class PerformanceTests(unittest.TestCase):
    def test_full_scan_reuses_run_scoped_performance_result(self):
        result = (
            {
                "three_year_return_pct": 60.0,
                "five_year_return_pct": None,
                "ten_year_return_pct": None,
            },
            [],
        )
        performance_cache = Mock()
        performance_cache.get.return_value = result
        run_cache = {}

        selected, warnings, scanned, rejected = ranking.evaluate_performance_full_scan(
            object(),
            [{"code": "000001"}],
            date(2026, 8, 19),
            50.0,
            performance_cache,
            object(),
            run_cache=run_cache,
        )
        self.assertEqual(1, len(selected))
        self.assertEqual([], warnings)
        self.assertEqual(1, scanned)
        self.assertEqual({}, rejected)
        self.assertIs(run_cache["000001"], result)
        performance_cache.get.assert_called_once()

    @patch("qdii_ranking.ranking.fetch_trailing_performance")
    def test_three_year_threshold_full_scan_includes_exact_match(self, fetch):
        fetch.side_effect = [
            ({"three_year_return_pct": 29.99}, []),
            ({"three_year_return_pct": 30.0}, []),
            ({"three_year_return_pct": 80.0}, []),
        ]
        candidates = [{"code": str(index)} for index in range(3)]
        selected, warnings, scanned = ranking.filter_performance_full_scan(
            object(), candidates, date(2026, 8, 19), 30.0, top=2
        )
        self.assertEqual(["1", "2"], [item["code"] for item in selected])
        self.assertEqual([], warnings)
        self.assertEqual(3, scanned)

    def test_calculates_trailing_return_and_max_drawdown(self):
        points = [
            {"date": date(2025, 8, 18), "nav": 1.00, "equity_return_pct": 0, "unit_money": ""},
            {"date": date(2025, 12, 1), "nav": 1.20, "equity_return_pct": 20, "unit_money": ""},
            {"date": date(2026, 3, 1), "nav": 0.90, "equity_return_pct": -25, "unit_money": ""},
            {"date": date(2026, 8, 18), "nav": 1.08, "equity_return_pct": 20, "unit_money": ""},
        ]
        result = ranking.calculate_trailing_performance(
            points, "example", date(2026, 8, 19), years=1
        )
        self.assertEqual(8.0, result["return_pct"])
        self.assertEqual(-25.0, result["max_drawdown_pct"])
        self.assertEqual("2025-08-18", result["start_date"])
        self.assertEqual("2026-08-18", result["end_date"])

    def test_calculates_three_year_performance(self):
        points = [
            {"date": date(2023, 8, 18), "nav": 1.00, "equity_return_pct": 0, "unit_money": ""},
            {"date": date(2024, 8, 18), "nav": 1.50, "equity_return_pct": 50, "unit_money": ""},
            {"date": date(2025, 8, 18), "nav": 1.20, "equity_return_pct": -20, "unit_money": ""},
            {"date": date(2026, 8, 18), "nav": 2.00, "equity_return_pct": 66.67, "unit_money": ""},
        ]
        result = ranking.calculate_trailing_performance(
            points, "example", date(2026, 8, 19), years=3
        )
        self.assertEqual(100.0, result["return_pct"])
        self.assertEqual(-20.0, result["max_drawdown_pct"])

    def test_calculates_complete_five_and_ten_year_returns(self):
        points = [
            {"date": date(2016, 8, 18), "nav": 1.0, "equity_return_pct": 0, "unit_money": ""},
            {"date": date(2021, 8, 18), "nav": 2.0, "equity_return_pct": 100, "unit_money": ""},
            {"date": date(2026, 8, 18), "nav": 3.0, "equity_return_pct": 50, "unit_money": ""},
        ]
        five = ranking.calculate_trailing_performance(points, "example", date(2026, 8, 20), 5)
        ten = ranking.calculate_trailing_performance(points, "example", date(2026, 8, 20), 10)
        self.assertEqual(50.0, five["return_pct"])
        self.assertEqual(200.0, ten["return_pct"])
        self.assertIsNone(
            ranking.calculate_trailing_performance(points[1:], "example", date(2026, 8, 20), 10)
        )

    def test_conditional_long_return_thresholds_and_missing_history(self):
        self.assertEqual(
            [],
            ranking.performance_threshold_failures(
                {
                    "three_year_return_pct": 30.0,
                    "five_year_return_pct": None,
                    "ten_year_return_pct": None,
                },
                30,
                50,
                100,
            ),
        )
        self.assertEqual(
            [],
            ranking.performance_threshold_failures(
                {
                    "three_year_return_pct": 30.0,
                    "five_year_return_pct": 50.0,
                    "ten_year_return_pct": 100.0,
                },
                30,
                50,
                100,
            ),
        )
        reasons = {
            reason
            for reason, _label in ranking.performance_threshold_failures(
                {
                    "three_year_return_pct": 30.0,
                    "five_year_return_pct": 49.99,
                    "ten_year_return_pct": 99.99,
                },
                30,
                50,
                100,
            )
        }
        self.assertEqual(
            {
                "five_year_return_below_threshold",
                "ten_year_return_below_threshold",
            },
            reasons,
        )

    def test_incomplete_display_only_history_does_not_emit_warning(self):
        trend = []
        for observed, nav in (
            (date(2023, 8, 18), 1.0),
            (date(2025, 8, 18), 1.5),
            (date(2026, 8, 18), 2.0),
        ):
            timestamp = int(
                datetime.combine(observed, datetime.min.time(), ranking.SHANGHAI_TZ).timestamp()
                * 1000
            )
            trend.append(
                {"x": timestamp, "y": nav, "equityReturn": 0, "unitMoney": ""}
            )

        class Client:
            def get_text(self, *_args, **_kwargs):
                return "var Data_netWorthTrend = " + json.dumps(trend) + ";"

        performance, warnings = ranking.fetch_trailing_performance(
            Client(),
            {"code": "000001", "fund_page_url": "https://example.test/fund"},
            date(2026, 8, 19),
        )
        self.assertEqual([], warnings)
        self.assertIsNone(performance["five_year_return_pct"])
        self.assertIsNone(performance["ten_year_return_pct"])

    def test_dividend_is_included_in_adjusted_return(self):
        previous = {
            "date": date(2025, 8, 18),
            "nav": 1.00,
            "equity_return_pct": 0,
            "unit_money": "",
        }
        current = {
            "date": date(2026, 1, 1),
            "nav": 0.90,
            "equity_return_pct": 10,
            "unit_money": "分红：每份派现金0.20元",
        }
        self.assertAlmostEqual(
            1.10, ranking.adjusted_daily_factor(previous, current, "example")
        )

    def test_parses_shanghai_dates_from_trend_data(self):
        timestamp = int(
            datetime(2026, 8, 18, tzinfo=ranking.SHANGHAI_TZ).timestamp() * 1000
        )
        payload = (
            'var Data_netWorthTrend = '
            f'[{{"x":{timestamp},"y":1.2,"equityReturn":2.0,"unitMoney":""}}];'
        )
        points = ranking.parse_performance_page(payload, "example")
        self.assertEqual(date(2026, 8, 18), points[0]["date"])


class ThreeYearBoundaryTests(unittest.TestCase):
    @staticmethod
    def points(days=1, end=date(2026, 9, 4), final_nav=1.6):
        start = ranking.years_ago(end, 3) + timedelta(days=days)
        return [
            {"date": observed, "nav": nav, "equity_return_pct": None, "unit_money": ""}
            for observed, nav in (
                (start, 1.0), (end - timedelta(days=730), 1.5),
                (end - timedelta(days=365), 1.2), (end, final_nav),
            )
        ]

    def test_full_window_and_inclusive_tolerance_limits(self):
        for days in (0, 1, 7, 8):
            with self.subTest(days=days):
                points = self.points(days)
                result = ranking.calculate_trailing_performance(
                    points, "000001", date(2026, 9, 15), 3,
                    inception_date=points[0]["date"].isoformat(),
                )
                if days == 8:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(days, result["boundary_shortfall_days"])
                    self.assertEqual(60.0, result["return_pct"])
                    self.assertEqual(-20.0, result["max_drawdown_pct"])
                    self.assertEqual(str(points[0]["date"]), result["start_date"])

    def test_requires_inception_match_and_strict_age(self):
        points = self.points()
        for inception, as_of in (
            (None, date(2026, 9, 8)),
            ("2023-09-04", date(2026, 9, 8)),
            ("2023-09-05", date(2026, 9, 5)),
            ("2023-09-05", date(2026, 9, 4)),
        ):
            with self.subTest(inception=inception, as_of=as_of):
                self.assertIsNone(ranking.calculate_trailing_performance(
                    points, "000001", as_of, 3, inception_date=inception,
                ))

    def test_complete_window_takes_precedence(self):
        points = self.points()
        points.insert(0, {**points[0], "date": date(2023, 9, 1), "nav": 0.8})
        result = ranking.calculate_trailing_performance(
            points, "000001", date(2026, 9, 8), 3, inception_date="2023-09-01",
        )
        self.assertEqual(0, result["boundary_shortfall_days"])
        self.assertEqual("2023-09-01", result["start_date"])
        self.assertEqual(100.0, result["return_pct"])

    def test_leap_day_and_future_points_use_historical_endpoint(self):
        points = self.points(1, end=date(2024, 2, 29))
        points.append({**points[-1], "date": date(2024, 3, 10), "nav": 2.0})
        result = ranking.calculate_trailing_performance(
            points, "000001", date(2024, 3, 2), 3, inception_date="2021-03-01",
        )
        self.assertEqual("2024-02-29", result["end_date"])
        self.assertEqual(1, result["boundary_shortfall_days"])
        self.assertEqual(60.0, result["return_pct"])

    def test_other_windows_never_use_tolerance(self):
        for years in (1, 5, 10):
            points = self.points()
            points = [
                {**points[0], "date": date(2026 - years, 9, 5)}, points[-1],
            ]
            with self.subTest(years=years):
                self.assertIsNone(ranking.calculate_trailing_performance(
                    points, "000001", date(2026, 9, 8), years,
                    inception_date=str(points[0]["date"]),
                ))

    def test_affected_funds_keep_real_returns_and_rejected_warning(self):
        for code, nav, qualifies in (
            ("018851", 1.2027, False), ("019155", 1.6207, True), ("019156", 1.5963, True),
        ):
            with self.subTest(code=code):
                performance, warnings = ranking.calculate_performance_from_points(
                    self.points(final_nav=nav), code, date(2026, 9, 8), "https://example.test/nav",
                    inception_date="2023-09-05",
                )
                self.assertEqual(round((nav - 1) * 100, 2), performance["three_year_return_pct"])
                self.assertEqual(1, performance["three_year_boundary_shortfall_days"])
                self.assertEqual(1, len(warnings))
                self.assertIn("距完整三年少 1 天", warnings[0])
                with patch(
                    "qdii_ranking.ranking.fetch_trailing_performance",
                    return_value=(performance, warnings),
                ):
                    selected, retained, scanned = ranking.filter_performance_full_scan(
                        object(), [{"code": code}], date(2026, 9, 8), 30.0, top=10,
                    )
                self.assertEqual(qualifies, bool(selected))
                self.assertEqual(warnings, retained)
                self.assertEqual(1, scanned)

    def test_plain_fetch_cache_304_and_forced_fetch_agree(self):
        points = self.points()
        trend = [
            {"x": int(datetime.combine(p["date"], datetime.min.time(), ranking.SHANGHAI_TZ).timestamp() * 1000),
             "y": p["nav"], "unitMoney": ""}
            for p in points
        ]
        source = "var Data_netWorthTrend = " + json.dumps(trend) + ";"
        fund = {"code": "000001", "inception_date": "2023-09-05",
                "fund_page_url": "https://example.test/fund", "latest_nav_date": "2026-09-04",
                "latest_nav_value": 1.6}
        benchmark = ranking.Nasdaq100Benchmark({}, {})
        as_of = date(2026, 9, 8)

        class Client:
            def __init__(self):
                self.responses = [(200, source, "modified"), (304, None, "modified"),
                                  (304, None, "modified"), (200, source, "modified")]
                self.validators = []

            def get_text(self, *_args, **_kwargs):
                return source

            def get_conditional_text(self, _url, referer=None, last_modified=None):
                self.validators.append(last_modified)
                return self.responses.pop(0)

        client = Client()
        expected = ranking.fetch_trailing_performance(client, fund, as_of, benchmark)
        with TemporaryDirectory() as directory:
            cache = ranking.PerformanceResultCache(Path(directory))
            self.assertEqual(expected, cache.get(client, fund, as_of, benchmark))
            self.assertEqual(expected, cache.get(client, fund, as_of, benchmark))
            performance, warnings = cache.get(
                client, {**fund, "latest_nav_date": "2026-09-07", "latest_nav_value": 1.7}, as_of, benchmark,
            )
            self.assertEqual(expected[0], performance)
            self.assertEqual(expected[1], warnings[:-1])
            self.assertIn("强制重新验证", warnings[-1])
            with patch.object(client, "get_conditional_text", side_effect=ranking.DataError("offline")):
                with self.assertRaisesRegex(ranking.DataError, "offline"):
                    cache.get(client, fund, as_of, benchmark)
            with patch.object(client, "get_text", side_effect=ranking.DataError("offline")):
                with self.assertRaisesRegex(ranking.DataError, "offline"):
                    ranking.fetch_trailing_performance(client, fund, as_of, benchmark)
        self.assertEqual([None, "modified", "modified", None], client.validators)
        self.assertEqual(1, cache.stats()["not_modified"])


class Nasdaq100FitTests(unittest.TestCase):
    def test_parses_official_benchmark_sources(self):
        timestamp = int(datetime(2026, 8, 18, tzinfo=timezone.utc).timestamp() * 1000)
        xndx = ranking.parse_nasdaq100_history(
            [{"x": timestamp, "y": 32123.45, "FPSymbol": "XNDX"}],
            date(2026, 8, 19),
        )
        safe = ranking.parse_safe_usd_cny_history(
            "<table><tr><td>2026-08-18</td><td>678.54</td></tr></table>",
            date(2026, 8, 19),
        )
        self.assertEqual(32123.45, xndx[date(2026, 8, 18)])
        self.assertAlmostEqual(6.7854, safe[date(2026, 8, 18)])

    @staticmethod
    def synthetic_series(weeks=160):
        start = date(2023, 7, 3)
        nav = 1.0
        index = 1000.0
        points = []
        xndx = {}
        fx = {}
        benchmark_returns = (0.01, -0.005, 0.02, -0.012)
        for offset in range(weeks):
            observed = start + timedelta(days=offset * 7)
            if offset:
                benchmark_return = benchmark_returns[offset % len(benchmark_returns)]
                index *= 1 + benchmark_return
                nav *= 1 + 2 * benchmark_return
            points.append(
                {
                    "date": observed,
                    "nav": nav,
                    "equity_return_pct": None,
                    "unit_money": "",
                }
            )
            xndx[observed] = index
            fx[observed] = 1.0
        return points, ranking.Nasdaq100Benchmark(xndx, fx)

    def test_calculates_weekly_correlation_beta_and_tracking_error(self):
        points, benchmark = self.synthetic_series()
        result = ranking.calculate_nasdaq100_fit(
            points, "example", points[-1]["date"], benchmark
        )
        self.assertEqual(1.0, result["correlation"])
        self.assertAlmostEqual(2.0, result["beta"], places=4)
        self.assertGreater(result["tracking_error_pct"], 0)
        self.assertGreaterEqual(result["observations"], 140)
        self.assertGreaterEqual(
            (date.fromisoformat(result["end_date"]) - date.fromisoformat(result["start_date"])).days,
            1000,
        )

    def test_rejects_insufficient_weekly_observations(self):
        points, benchmark = self.synthetic_series(20)
        with self.assertRaisesRegex(ranking.DataError, "only 19 valid"):
            ranking.calculate_nasdaq100_fit(
                points, "example", points[-1]["date"], benchmark
            )

    def test_does_not_use_future_or_stale_benchmark_values(self):
        observed = date(2026, 8, 20)
        series = {
            observed - timedelta(days=8): 1.0,
            observed + timedelta(days=1): 2.0,
        }
        self.assertIsNone(
            ranking.latest_series_value(series, sorted(series), observed)
        )


class Nasdaq100BenchmarkCacheTests(unittest.TestCase):
    @staticmethod
    def source_points(as_of):
        start = ranking.years_ago(as_of, 3) - timedelta(days=21)
        dates = [start + timedelta(days=offset) for offset in range((as_of - start).days + 1)]
        return (
            {observed: 1000.0 + index for index, observed in enumerate(dates)},
            {observed: 6.8 for observed in dates},
        )

    @patch("qdii_ranking.cache.benchmark.fetch_safe_usd_cny_history")
    @patch("qdii_ranking.cache.benchmark.fetch_nasdaq100_history")
    def test_populates_cache_and_uses_complete_cache_on_source_failure(
        self, fetch_xndx, fetch_fx
    ):
        as_of = date(2026, 8, 20)
        xndx, fx = self.source_points(as_of)
        fetch_xndx.return_value = xndx
        fetch_fx.return_value = fx
        with TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark.json"
            first_cache = ranking.Nasdaq100BenchmarkCache(path)
            benchmark, warnings = first_cache.get(object(), as_of)
            self.assertEqual([], warnings)
            self.assertEqual(as_of, max(benchmark.xndx_levels))
            self.assertTrue(path.is_file())

            fetch_xndx.side_effect = ranking.DataError("offline")
            fetch_fx.side_effect = ranking.DataError("offline")
            second_cache = ranking.Nasdaq100BenchmarkCache(path)
            cached, warnings = second_cache.get(object(), as_of)
            self.assertEqual(2, len(warnings))
            self.assertEqual(2, second_cache.stats()["fallbacks"])
            self.assertEqual(benchmark.xndx_levels, cached.xndx_levels)


class PerformanceCacheTests(unittest.TestCase):
    @staticmethod
    def result():
        return {
            "performance_source_url": "https://example.test/performance.js",
            "nav_history_start_date": "2016-08-19",
            "nav_history_end_date": "2026-08-19",
            "one_year_return_pct": 20.0,
            "one_year_max_drawdown_pct": -10.0,
            "one_year_performance_start_date": "2025-08-19",
            "one_year_performance_end_date": "2026-08-19",
            "three_year_return_pct": 60.0,
            "three_year_boundary_shortfall_days": 0,
            "three_year_max_drawdown_pct": -20.0,
            "three_year_performance_start_date": "2023-08-19",
            "three_year_performance_end_date": "2026-08-19",
            "five_year_return_pct": 100.0,
            "five_year_max_drawdown_pct": -30.0,
            "five_year_performance_start_date": "2021-08-19",
            "five_year_performance_end_date": "2026-08-19",
            "ten_year_return_pct": 200.0,
            "ten_year_max_drawdown_pct": -40.0,
            "ten_year_performance_start_date": "2016-08-19",
            "ten_year_performance_end_date": "2026-08-19",
            "nasdaq100_fit": {
                "correlation": 0.95,
                "beta": 1.01,
                "tracking_error_pct": 5.0,
                "observations": 154,
                "start_date": "2023-08-19",
                "end_date": "2026-08-19",
            },
            "nasdaq100_fit_error": None,
        }

    @patch("qdii_ranking.cache.performance.calculate_performance_from_points")
    def test_cache_revalidates_with_304_and_rebuilds_corruption(self, calculate):
        calculate.return_value = (self.result(), ["cached warning"])
        observed = date(2026, 8, 19)
        timestamp = int(
            ranking.datetime.combine(
                observed, ranking.datetime.min.time(), ranking.SHANGHAI_TZ
            ).timestamp()
            * 1000
        )
        source = (
            "var Data_netWorthTrend = "
            + json.dumps(
                [
                    {"x": timestamp - 86400000, "y": 1.0, "equityReturn": 0},
                    {"x": timestamp, "y": 1.1, "equityReturn": 10},
                ]
            )
            + ";"
        )

        class Client:
            def __init__(self):
                self.responses = [
                    (200, source, "Wed, 19 Aug 2026 01:00:00 GMT"),
                    (304, None, "Wed, 19 Aug 2026 01:00:00 GMT"),
                    (200, source, "Wed, 19 Aug 2026 01:00:00 GMT"),
                ]
                self.last_modified = []

            def get_conditional_text(self, _url, referer=None, last_modified=None):
                self.last_modified.append(last_modified)
                return self.responses.pop(0)

        client = Client()
        fund = {
            "code": "000001",
            "fund_page_url": "https://example.test/fund",
            "latest_nav_date": observed.isoformat(),
            "latest_nav_value": 1.1,
        }

        as_of = date(2026, 8, 19)
        benchmark = ranking.Nasdaq100Benchmark(
            {date(2023, 8, 1): 100.0, as_of: 200.0},
            {date(2023, 8, 1): 7.0, as_of: 6.8},
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ranking.PerformanceResultCache(root)
            self.assertEqual(self.result(), cache.get(client, fund, as_of, benchmark)[0])
            self.assertEqual(self.result(), cache.get(client, fund, as_of, benchmark)[0])
            path = root / "nav-history" / "000001.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = 0
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(self.result(), cache.get(client, fund, as_of, benchmark)[0])
        self.assertEqual([None, "Wed, 19 Aug 2026 01:00:00 GMT", None], client.last_modified)
        self.assertEqual(
            {
                "hits": 1,
                "misses": 2,
                "corrupt_rebuilds": 1,
                "conditional_requests": 1,
                "not_modified": 1,
                "updates": 2,
            },
            cache.stats(),
        )

    def test_page_lead_warns_only_after_full_history_revalidation(self):
        points = [
            {
                "date": date(2026, 8, 19),
                "nav": 1.5,
                "equity_return_pct": 1.0,
                "unit_money": "",
            }
        ]
        fund = {
            "code": "000001",
            "latest_nav_date": "2026-08-20",
            "latest_nav_value": 1.6,
        }
        with self.assertRaisesRegex(ranking.DataError, "does not contain"):
            ranking.PerformanceResultCache._validate_page_snapshot(
                points, fund, date(2026, 8, 20)
            )
        warning = ranking.PerformanceResultCache._validate_page_snapshot(
            points, fund, date(2026, 8, 20), allow_page_lead=True
        )
        self.assertIn("强制重新验证", warning)
        with self.assertRaisesRegex(ranking.DataError, "does not contain"):
            ranking.PerformanceResultCache._validate_page_snapshot(
                points,
                {**fund, "latest_nav_date": "2026-08-28"},
                date(2026, 8, 28),
                allow_page_lead=True,
            )
