import math
import unittest
from datetime import date, timedelta

from qdii_ranking.errors import DataError
from qdii_ranking.models import Nasdaq100Benchmark
from qdii_ranking.services.nasdaq100 import apply_common_window
from qdii_ranking.sources.performance import calculate_nasdaq100_fit_for_period


def make_points(start: date, end: date, multiplier: float = 1.0):
    points = []
    observed = start
    index = 0
    while observed <= end:
        nav = 1 + multiplier * (0.0007 * index + 0.025 * math.sin(index / 19))
        points.append(
            {
                "date": observed,
                "nav": nav,
                "equity_return_pct": None,
                "unit_money": "",
            }
        )
        observed += timedelta(days=1)
        index += 1
    return points


def make_record(code: str, inception: str):
    return {
        "code": code,
        "name": f"测试纳斯达克100基金{code}",
        "inception_date": inception,
    }


def make_benchmark(start: date, end: date):
    points = make_points(start, end)
    return Nasdaq100Benchmark(
        xndx_levels={item["date"]: item["nav"] * 1000 for item in points},
        usd_cny_rates={item["date"]: 7.0 for item in points},
    )


class NasdaqCommonWindowTests(unittest.TestCase):
    def setUp(self):
        self.as_of = date(2026, 4, 1)
        self.end = date(2026, 3, 31)
        self.history_start = date(2023, 1, 1)
        self.benchmark = make_benchmark(self.history_start, self.end)

    def test_trailing_two_year_window_excludes_funds_younger_than_cutoff(self):
        # as_of 2026-04-01, so target_start is 2024-04-01; inception 2024-06-01 is too young.
        records = [
            make_record("000001", "2023-01-01"),
            make_record("000002", "2024-06-01"),
        ]
        points = {
            "000001": make_points(self.history_start, self.end, 1.0),
            "000002": make_points(date(2024, 6, 1), self.end, 0.98),
        }
        window, warnings = apply_common_window(
            records, points, self.as_of, self.benchmark
        )
        self.assertEqual([], warnings)
        self.assertEqual("2024-04-01", window["start_date"])
        self.assertEqual("2024-04-01", window["target_start_date"])
        self.assertEqual(2, window["window_years"])
        self.assertEqual(2, window["minimum_anchor_age_years"])
        self.assertIsNone(window["anchor_inception_date"])
        self.assertEqual([], window["anchor_funds"])
        self.assertEqual("2026-03-31", window["end_date"])
        self.assertIsNotNone(records[0]["common_period_return_pct"])
        self.assertIsNone(records[1]["common_period_return_pct"])
        self.assertIn("未满两年", records[1]["common_period_error"])

    def test_start_can_move_within_seven_days_from_target(self):
        # Funds exist at target but have no NAV until a few days later.
        target = date(2024, 4, 1)
        shared_start = target + timedelta(days=3)
        records = [
            make_record("000001", "2023-01-01"),
            make_record("000002", target.isoformat()),
        ]
        points = {
            "000001": [
                point
                for point in make_points(self.history_start, self.end)
                if point["date"] < target or point["date"] >= shared_start
            ],
            "000002": make_points(shared_start, self.end),
        }
        window, _ = apply_common_window(records, points, self.as_of, self.benchmark)
        self.assertEqual(shared_start.isoformat(), window["start_date"])
        self.assertEqual(2, window["comparable_count"])

    def test_misaligned_fresh_end_only_excludes_outlier(self):
        records = [
            make_record("000001", "2023-01-01"),
            make_record("000002", "2023-01-01"),
            make_record("000003", "2022-01-01"),
        ]
        healthy_end = self.end - timedelta(days=1)
        healthy = make_points(self.history_start, healthy_end)
        outlier = [
            *make_points(self.history_start, self.end - timedelta(days=15)),
            {
                "date": self.end,
                "nav": 2.0,
                "equity_return_pct": None,
                "unit_money": "",
            },
        ]
        window, warnings = apply_common_window(
            records,
            {"000001": outlier, "000002": healthy, "000003": healthy},
            self.as_of,
            self.benchmark,
        )
        self.assertEqual(healthy_end.isoformat(), window["end_date"])
        self.assertEqual(2, window["comparable_count"])
        self.assertTrue(any("000001" in warning for warning in warnings))

    def test_stale_record_does_not_freeze_healthy_records(self):
        # One fund has stale NAV, so it's excluded; the other still computes its metrics.
        stale_end = self.as_of - timedelta(days=15)
        records = [
            make_record("000001", "2023-01-01"),
            make_record("000002", "2023-01-01"),
        ]
        points = {
            "000001": make_points(self.history_start, stale_end),
            "000002": make_points(self.history_start, self.end),
        }
        window, warnings = apply_common_window(
            records, points, self.as_of, self.benchmark
        )
        self.assertEqual("2024-04-01", window["start_date"])
        self.assertEqual(1, window["comparable_count"])
        self.assertTrue(any("000001" in warning for warning in warnings))

    def test_fit_coverage_uses_requested_common_start(self):
        requested_start = date(2024, 3, 22)
        late_start = requested_start + timedelta(days=120)
        points = make_points(late_start, self.end)
        with self.assertRaisesRegex(DataError, "covers only"):
            calculate_nasdaq100_fit_for_period(
                points,
                "000001",
                self.benchmark,
                requested_start,
                self.end,
                min_observations=45,
                min_span_days=330,
                min_coverage_ratio=0.85,
            )


if __name__ == "__main__":
    unittest.main()
