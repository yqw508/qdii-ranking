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


class HolderPeriodTests(unittest.TestCase):
    def test_rejects_partial_new_period(self):
        periods = [
            ranking.HolderPeriod("2026-06-30", 1, "2026_2"),
            ranking.HolderPeriod("2025-12-31", 24419, "2025_4"),
            ranking.HolderPeriod("2025-06-30", 23094, "2025_2"),
        ]
        selected, warnings = ranking.select_holder_period(periods)
        self.assertEqual("2025-12-31", selected.report_date)
        self.assertEqual(1, len(warnings))

    def test_partial_period_can_be_forced(self):
        periods = [
            ranking.HolderPeriod("2026-06-30", 1, "2026_2"),
            ranking.HolderPeriod("2025-12-31", 24419, "2025_4"),
        ]
        selected, _ = ranking.select_holder_period(periods, allow_partial=True)
        self.assertEqual("2026-06-30", selected.report_date)

    def test_detects_previous_ranked_fund_missing_from_unchanged_holder_period(self):
        metadata = {
            "016701": {
                "code": "016701",
                "name": "银华海外数字经济量化选股混合发起式(QDII)A",
                "fund_type": "QDII-混合偏股",
            }
        }
        previous = {
            "holder_report_date": "2026-06-30",
            "records": [{"code": "016701"}],
            "global_supplement": {"records": []},
        }
        self.assertEqual(
            ["016701"],
            ranking.disappeared_ranked_candidates(
                previous, [], metadata, "2026-06-30"
            ),
        )

    def test_allows_report_period_change_and_ineligible_metadata_change(self):
        previous = {
            "holder_report_date": "2026-06-30",
            "records": [{"code": "016701"}],
            "global_supplement": {"records": []},
        }
        eligible_metadata = {
            "016701": {
                "code": "016701",
                "name": "银华海外数字经济量化选股混合发起式(QDII)A",
                "fund_type": "QDII-混合偏股",
            }
        }
        self.assertEqual(
            [],
            ranking.disappeared_ranked_candidates(
                previous, [], eligible_metadata, "2026-12-31"
            ),
        )
        ineligible_metadata = {
            **eligible_metadata,
            "016701": {
                **eligible_metadata["016701"],
                "name": "银华海外数字经济量化选股混合发起式(QDII)C",
            },
        }
        self.assertEqual(
            [],
            ranking.disappeared_ranked_candidates(
                previous, [], ineligible_metadata, "2026-06-30"
            ),
        )


class FundFilterTests(unittest.TestCase):
    def test_default_result_count_and_return_threshold(self):
        args = ranking.parse_args([])
        self.assertEqual(10, args.top)
        self.assertIsNone(args.min_scale)
        self.assertEqual(30.0, args.min_three_year_return_pct)
        self.assertEqual(40.0, args.min_five_year_return_pct)
        self.assertEqual(100.0, args.min_ten_year_return_pct)
        self.assertEqual(50.0, args.min_us_equity_pct)
        self.assertEqual(200, args.min_direct_limit_cny)
        self.assertEqual(["亚洲", "中国", "港"], args.us_main_exclude_keywords)
        self.assertEqual("public", args.publish_dir.name)


    def test_legacy_exclude_keywords_option_maps_to_us_main_only(self):
        args = ranking.parse_args(["--exclude-keywords", "亚洲", "港"])
        self.assertEqual(["亚洲", "港"], args.us_main_exclude_keywords)

    def test_recognizes_rmb_a_share(self):
        self.assertTrue(
            ranking.is_rmb_a_share(
                {"name": "某全球企业混合(QDII)A", "fund_type": "QDII-混合偏股"}
            )
        )
        self.assertTrue(
            ranking.is_rmb_a_share(
                {"name": "某全球配置(QDII)人民币A", "fund_type": "QDII-混合"}
            )
        )
        self.assertFalse(
            ranking.is_rmb_a_share(
                {"name": "某全球配置(QDII)美元A", "fund_type": "QDII-混合"}
            )
        )
        self.assertFalse(
            ranking.is_rmb_a_share(
                {"name": "某全球配置(QDII)C", "fund_type": "QDII-混合"}
            )
        )

    def test_recognizes_plain_rmb_primary_share_but_not_c_d_or_foreign_currency(self):
        self.assertTrue(
            ranking.is_rmb_a_share(
                {"name": "嘉实美国成长股票人民币", "fund_type": "QDII-股票"}
            )
        )
        for name in (
            "嘉实美国成长股票人民币C",
            "嘉实美国成长股票人民币D",
            "易方达纳斯达克100ETF联接(QDII-LOF)C(人民币)",
            "嘉实纳斯达克100ETF发起联接(QDII)I人民币",
            "广发纳斯达克100ETF联接(QDII)人民币F",
            "嘉实美国成长股票美元现汇",
            "嘉实美国成长股票港币",
        ):
            self.assertFalse(
                ranking.is_rmb_a_share({"name": name, "fund_type": "QDII-股票"}),
                name,
            )

    def test_candidate_scope_keeps_qdii_and_overseas_passive_only(self):
        self.assertTrue(
            ranking.is_rmb_a_share(
                {"name": "华夏全球股票(QDII)人民币A", "fund_type": "QDII-普通股票"}
            )
        )
        self.assertTrue(
            ranking.is_rmb_a_share(
                {"name": "华夏纳斯达克100联接人民币A", "fund_type": "指数型-海外股票"}
            )
        )
        self.assertFalse(
            ranking.is_rmb_a_share(
                {"name": "普通境内指数人民币A", "fund_type": "指数型-股票"}
            )
        )

    def test_standalone_etf_is_excluded_but_feeder_and_lof_are_kept(self):
        self.assertFalse(
            ranking.is_otc_share(
                {"name": "纳斯达克100ETF人民币A", "fund_type": "指数型-海外股票"}
            )
        )
        self.assertTrue(
            ranking.is_otc_share(
                {"name": "纳斯达克100ETF联接人民币A", "fund_type": "指数型-海外股票"}
            )
        )
        self.assertTrue(
            ranking.is_otc_share(
                {"name": "美国成长LOF人民币A", "fund_type": "指数型-海外股票"}
            )
        )

    def test_filters_keywords_and_ranks_strict_scale(self):
        metadata = {
            "1": {"code": "1", "name": "全球科技(QDII)A", "fund_type": "QDII-股票"},
            "2": {"code": "2", "name": "亚洲科技(QDII)A", "fund_type": "QDII-股票"},
            "3": {"code": "3", "name": "全球医疗(QDII)A", "fund_type": "QDII-股票"},
            "4": {"code": "4", "name": "新兴市场(QDII)A", "fund_type": "QDII-纯债"},
        }
        rows = [
            ["1", "", "40", "60", "0", "1"],
            ["2", "", "90", "10", "0", "1"],
            ["3", "", "30", "70", "0", "1"],
            ["4", "", "95", "5", "0", "1"],
        ]
        candidates = ranking.build_holder_candidates(rows, metadata, ["亚洲"])
        candidates[0].update(scale_billion_cny=4, purchase_status="open")
        candidates[1].update(scale_billion_cny=3, purchase_status="open")
        result = ranking.filter_and_rank(candidates, min_scale=3, top=10)
        self.assertEqual(["1"], [item["code"] for item in result])

    def test_default_scale_filter_keeps_small_funds(self):
        candidates = [
            {
                "code": "1",
                "name": "全球小规模基金(QDII)A",
                "institution_holding_ratio_pct": 10,
                "scale_billion_cny": 0.01,
                "purchase_status": "open",
                "inception_date": "2020-01-01",
            }
        ]
        result = ranking.filter_and_rank(candidates, min_scale=None, top=10)
        self.assertEqual(["1"], [item["code"] for item in result])

    def test_excludes_bond_and_commodity_types_but_keeps_fof_and_reit(self):
        metadata = {
            "1": {"code": "1", "name": "全球债券人民币A", "fund_type": "QDII-纯债"},
            "2": {"code": "2", "name": "全球混合债人民币A", "fund_type": "QDII-混合债"},
            "3": {"code": "3", "name": "全球商品人民币A", "fund_type": "QDII-商品"},
            "4": {"code": "4", "name": "全球配置人民币A", "fund_type": "QDII-FOF"},
            "5": {"code": "5", "name": "全球REIT人民币A", "fund_type": "QDII-REITs"},
        }
        rows = [[code, "", "10", "90", "0", "1"] for code in metadata]
        candidates = ranking.build_holder_candidates(rows, metadata, [])
        self.assertEqual(["4", "5"], sorted(item["code"] for item in candidates))

    def test_geographic_exclusions_backfill_to_ten(self):
        names = [
            ("457001", "国富亚洲机会股票(QDII)A", 56.87, 11.58),
            ("008253", "华宝致远混合(QDII)A", 43.95, 11.01),
            ("501226", "长城全球新能源车股票(QDII)A", 42.30, 12.68),
            ("016701", "银华海外数字经济量化混合(QDII)A", 42.11, 25.85),
            ("011583", "大成港股精选混合(QDII)A", 41.51, 4.50),
            ("007729", "招商普盛全球配置(QDII)人民币A", 38.56, 4.88),
            ("018229", "易方达全球优质企业混合(QDII)A", 38.15, 44.20),
            ("100061", "富国中国中小盘混合(QDII)人民币A", 37.15, 30.23),
            ("163813", "中银全球策略(QDII-FOF)A", 24.74, 5.73),
            ("006373", "国富全球科技互联混合(QDII)人民币A", 18.09, 58.83),
            ("020001", "全球消费精选(QDII)A", 17.50, 8.00),
            ("020002", "全球医疗精选(QDII)A", 16.50, 9.00),
            ("020003", "全球价值精选(QDII)A", 15.50, 10.00),
        ]
        ranked_pool = [
            {
                "code": code,
                "name": name,
                "institution_holding_ratio_pct": ratio,
                "scale_billion_cny": scale,
                "purchase_status": "open",
                "inception_date": "2020-01-01",
            }
            for code, name, ratio, scale in names
        ]
        result = ranking.filter_and_rank(
            ranked_pool,
            min_scale=3,
            top=10,
            exclude_keywords=["亚洲", "中国", "港"],
            as_of=date(2026, 8, 19),
            min_age_years=3,
        )
        self.assertEqual(
            [
                "008253",
                "501226",
                "016701",
                "007729",
                "018229",
                "163813",
                "006373",
                "020001",
                "020002",
                "020003",
            ],
            [item["code"] for item in result],
        )

    def test_requires_fund_to_be_strictly_older_than_three_years(self):
        candidates = [
            {
                "code": "old",
                "name": "全球老基金(QDII)A",
                "institution_holding_ratio_pct": 10,
                "scale_billion_cny": 5,
                "purchase_status": "open",
                "inception_date": "2023-08-18",
            },
            {
                "code": "exact",
                "name": "全球三年基金(QDII)A",
                "institution_holding_ratio_pct": 20,
                "scale_billion_cny": 5,
                "purchase_status": "open",
                "inception_date": "2023-08-19",
            },
        ]
        result = ranking.filter_and_rank(
            candidates,
            min_scale=3,
            top=10,
            as_of=date(2026, 8, 19),
            min_age_years=3,
        )
        self.assertEqual(["old"], [item["code"] for item in result])


class Nasdaq100OTCTests(unittest.TestCase):
    def test_name_match_accepts_supported_variants_only(self):
        self.assertTrue(ranking.is_nasdaq100_otc_name("华夏纳斯达克100ETF联接人民币A"))
        self.assertTrue(ranking.is_nasdaq100_otc_name("某纳指 100 联接(QDII)A"))
        self.assertTrue(ranking.is_nasdaq100_otc_name("Nasdaq-100 feeder RMB A"))
        self.assertFalse(ranking.is_nasdaq100_otc_name("纳斯达克生物科技人民币A"))
        self.assertFalse(ranking.is_nasdaq100_otc_name("标普500指数人民币A"))

    def test_candidates_use_full_metadata_and_keep_missing_holder_rows(self):
        metadata = {
            "000001": {"code": "000001", "name": "纳斯达克100联接人民币A", "fund_type": "指数型-海外股票"},
            "000002": {"code": "000002", "name": "纳斯达克100ETF人民币A", "fund_type": "指数型-海外股票"},
            "000003": {"code": "000003", "name": "Nasdaq 100人民币C", "fund_type": "指数型-海外股票"},
            "000004": {"code": "000004", "name": "纳斯达克100联接人民币A", "fund_type": "QDII-纯债"},
            "000005": {"code": "000005", "name": "Nasdaq-100人民币A", "fund_type": "QDII-股票"},
        }
        rows = [["000001", "", "12.5", "1", "", "2"]]
        candidates = ranking.build_nasdaq100_otc_candidates(metadata, rows)
        self.assertEqual(["000001", "000005"], [item["code"] for item in candidates])
        self.assertEqual(12.5, candidates[0]["institution_holding_ratio_pct"])
        self.assertIsNone(candidates[1]["institution_holding_ratio_pct"])

    def test_two_year_window_does_not_apply_three_year_tolerance(self):
        points = [
            {"date": date(2024, 1, 3), "nav": 1.0, "equity_return_pct": None, "unit_money": "1"},
            {"date": date(2026, 1, 2), "nav": 1.5, "equity_return_pct": None, "unit_money": "1"},
        ]
        self.assertIsNone(
            ranking.calculate_trailing_performance(
                points, "000001", date(2026, 1, 2), 2, inception_date="2024-01-03"
            )
        )

    def test_sort_places_missing_values_last(self):
        def item(code, ret, fee, te, drawdown, scale):
            return {
                "code": code,
                "common_period_return_pct": ret,
                "common_period_max_drawdown_pct": drawdown,
                "scale_billion_cny": scale,
                "holding_cost": {"annualized_pct": fee},
                "nasdaq100_fit_common_period": (
                    None if te is None else {"tracking_error_pct": te}
                ),
            }
        records = [
            item("000003", None, 0.4, 1.0, 50, 2),
            item("000002", 50, None, 1.0, 50, 2),
            item("000001", 50, 0.6, 0.8, 50, 2),
        ]
        records.sort(key=ranking.nasdaq100_otc_sort_key)
        self.assertEqual(["000001", "000002", "000003"], [item["code"] for item in records])

    def test_sort_uses_all_six_common_window_levels(self):
        def item(code, ret, fee, te, drawdown, scale):
            return {
                "code": code,
                "common_period_return_pct": ret,
                "common_period_max_drawdown_pct": drawdown,
                "scale_billion_cny": scale,
                "holding_cost": {"annualized_pct": fee},
                "nasdaq100_fit_common_period": {"tracking_error_pct": te},
            }

        records = [
            item("000009", 51, 9.0, 9.0, -90, 1),
            item("000008", 50, 0.3, 2.0, -30, 1),
            item("000007", 50, 0.4, 0.8, -30, 1),
            item("000006", 50, 0.4, 0.7, -20, 1),
            item("000005", 50, 0.4, 0.7, -10, 2),
            item("000004", 50, 0.4, 0.7, -10, 3),
            item("000003", 50, 0.4, 0.7, -10, 3),
        ]
        records.sort(key=ranking.nasdaq100_otc_sort_key)
        self.assertEqual(
            ["000009", "000008", "000003", "000004", "000005", "000006", "000007"],
            [record["code"] for record in records],
        )
