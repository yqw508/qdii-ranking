import unittest
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import update_qdii_ranking as ranking


def sample_exchange_premium(run_date="2026-08-20"):
    entries, fingerprint = ranking.load_exchange_premium_catalog(
        ranking.DEFAULT_US_EQUITY_ETF_CATALOG
    )
    records = []
    for index, entry in enumerate(entries):
        premium = round((index - 10) / 10, 2)
        iopv = 1.0
        records.append(
            {
                **entry,
                "market_price_cny": round(iopv * (1 + premium / 100), 4),
                "iopv_cny": iopv,
                "source_discount_pct": -premium,
                "premium_pct": premium,
                "change_pct": round(index / 100, 2),
                "turnover_cny": float(10_000_000 + index),
                "quote_date": run_date,
                "updated_at": f"{run_date}T15:00:00+08:00",
                "quote_source_url": f"https://example.test/quote/{entry['code']}",
                "quote_status": "fresh",
                "holding_cost": {
                    "status": "parsed",
                    "annualized_pct": round(0.6 + index / 100, 2),
                    "measurement_date": None,
                    "source_title": f"{entry['name']}基金产品资料概要更新",
                    "source_published_date": run_date,
                    "source_url": f"https://example.test/fee/{entry['code']}.pdf",
                },
            }
        )
    records.sort(key=ranking.exchange_premium_sort_key)
    return {
        "schema_version": 2,
        "status": "fresh",
        "requested_at": f"{run_date}T07:07:00+08:00",
        "quote_delay_minutes": 15,
        "expected_count": len(entries),
        "fresh_count": len(entries),
        "cache_hit_count": 0,
        "catalog_fingerprint": fingerprint,
        "group_order": list(ranking.ETF_PREMIUM_GROUP_ORDER),
        "source_name": "东方财富ETF行情",
        "source_url": ranking.ETF_QUOTE_PAGE_URL,
        "refresh_url": ranking.exchange_premium_quote_url(entries),
        "records": records,
    }


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


class FundFilterTests(unittest.TestCase):
    def test_default_result_count_and_return_threshold(self):
        args = ranking.parse_args([])
        self.assertEqual(10, args.top)
        self.assertIsNone(args.min_scale)
        self.assertEqual(30.0, args.min_three_year_return_pct)
        self.assertEqual(50.0, args.min_five_year_return_pct)
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


class ContractBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ranking.ContractBenchmarkCatalog(
            ranking.DEFAULT_CONTRACT_BENCHMARK_CATALOG
        )
        self.fund = {
            "code": "000043",
            "name": "嘉实美国成长股票人民币",
            "fund_type": "QDII-普通股票",
            "fund_page_url": "https://example.test/000043",
        }

    def test_index_rmb_a_is_accepted_but_c_and_standalone_etf_are_not(self):
        self.assertTrue(
            ranking.is_otc_share(
                {
                    "name": "建信纳斯达克100指数(QDII)A人民币",
                    "fund_type": "指数型-海外股票",
                }
            )
        )
        self.assertFalse(
            ranking.is_otc_share(
                {
                    "name": "建信纳斯达克100指数(QDII)C人民币",
                    "fund_type": "指数型-海外股票",
                }
            )
        )
        self.assertFalse(
            ranking.is_otc_share(
                {
                    "name": "纳斯达克100ETF人民币A",
                    "fund_type": "指数型-海外股票",
                }
            )
        )

    def test_parses_single_dominant_benchmark_and_table_heading(self):
        text = "业绩比较基准 95%×罗素1000 成长指数收益率+5%×活期存款利率 风险收益特征"
        profile = ranking.parse_contract_benchmark(text, self.fund, self.catalog)
        self.assertEqual("russell-1000-growth", profile["benchmark_id"])
        self.assertEqual(95.0, profile["benchmark_weight_pct"])

    def test_prefers_weighted_benchmark_over_earlier_target_index_description(self):
        text = """
        标的指数为纳斯达克100指数，相关章节介绍指数编制方法。
        业绩比较基准 95%×纳斯达克100指数收益率+5%×活期存款利率 风险收益特征
        """
        profile = ranking.parse_contract_benchmark(text, self.fund, self.catalog)
        self.assertEqual("nasdaq-100", profile["benchmark_id"])
        self.assertEqual(95.0, profile["benchmark_weight_pct"])

    def test_accepts_composite_and_sub_eighty_percent_benchmarks(self):
        composite = ranking.parse_contract_benchmark(
            "本基金的业绩比较基准为80%×纳斯达克100指数收益率+20%×恒生指数收益率。",
            self.fund,
            self.catalog,
        )
        self.assertEqual("composite", composite["status"])
        self.assertEqual(2, len(composite["components"]))
        low_weight = ranking.parse_contract_benchmark(
            "本基金的业绩比较基准为60%×MSCI所有国家世界指数+40%×美国3月政府债券收益率。",
            self.fund,
            self.catalog,
        )
        self.assertEqual("recognized", low_weight["status"])
        self.assertEqual(60.0, low_weight["benchmark_weight_pct"])

    def test_special_product_structures_are_explicit(self):
        self.assertEqual(
            "leveraged",
            ranking.detect_product_structure("纳斯达克100两倍做多", "standard"),
        )
        self.assertEqual(
            "inverse", ranking.detect_product_structure("标普500反向", "standard")
        )
        self.assertEqual(
            "volatility", ranking.detect_product_structure("VIX指数", "standard")
        )
        self.assertEqual(
            "standard",
            ranking.detect_product_structure("风险章节说明基金不得形成杠杆", "standard"),
        )

    @patch.object(ranking, "_announcement_page")
    def test_legal_document_selection_never_uses_future_or_wrong_share(self, page):
        page.return_value = {
            "TotalCount": 4,
            "PageSize": 100,
            "Data": [
                {
                    "TITLE": "某基金更新招募说明书",
                    "PUBLISHDATEDesc": "2026-08-21",
                    "ID": "future",
                },
                {
                    "TITLE": "某基金更新招募说明书",
                    "PUBLISHDATEDesc": "2026-08-19",
                    "ID": "prospectus",
                },
                {
                    "TITLE": "某基金(D类份额人民币)基金产品资料概要更新",
                    "PUBLISHDATEDesc": "2026-08-20",
                    "ID": "wrong-share",
                },
                {
                    "TITLE": "某基金(A类份额)基金产品资料概要更新",
                    "PUBLISHDATEDesc": "2026-08-18",
                    "ID": "summary",
                },
            ],
        }
        prospectus, summary = ranking.fetch_latest_legal_documents(
            object(), "000043", date(2026, 8, 20)
        )
        self.assertEqual("prospectus", prospectus.announcement_id)
        self.assertEqual("summary", summary.announcement_id)

    @patch.object(ranking, "fetch_latest_legal_documents")
    def test_product_summary_conflict_warns_and_does_not_block(self, documents):
        prospectus = ranking.LegalDocument(
            "p", "招募说明书", date(2026, 6, 1), "https://example.test/p.pdf", "prospectus"
        )
        summary = ranking.LegalDocument(
            "s", "产品概要", date(2026, 6, 2), "https://example.test/s.pdf", "product_summary"
        )
        documents.return_value = prospectus, summary

        class Cache:
            def get_text(self, _client, document, _referer):
                if document.document_type == "prospectus":
                    return "本基金的业绩比较基准为95%×纳斯达克100指数收益率+5%×活期存款利率。"
                return "业绩比较基准 95%×德国DAX指数收益率+5%×活期存款利率 风险收益特征"

        profile, holding_cost, warnings = ranking.resolve_contract_benchmark(
            object(), self.fund, date(2026, 8, 20), Cache(), self.catalog
        )
        self.assertEqual("conflict", profile["product_summary_status"])
        self.assertEqual("unavailable", holding_cost["status"])
        self.assertTrue(any("不一致" in warning for warning in warnings))

    @patch.object(ranking, "fetch_latest_legal_documents")
    def test_legal_document_index_failure_is_non_blocking(self, documents):
        documents.side_effect = ranking.DataError("source unavailable")
        profile, holding_cost, warnings = ranking.resolve_contract_benchmark(
            object(), self.fund, date(2026, 8, 20), object(), self.catalog
        )
        self.assertEqual("unreadable", profile["status"])
        self.assertEqual("unreadable", profile["product_summary_status"])
        self.assertEqual("unavailable", holding_cost["status"])
        self.assertEqual(2, len(warnings))

    @patch.object(ranking, "fetch_latest_legal_documents")
    def test_missing_product_summary_warns_about_holding_cost(self, documents):
        prospectus = ranking.LegalDocument(
            "p", "招募说明书", date(2026, 6, 1), "https://example.test/p.pdf", "prospectus"
        )
        documents.return_value = prospectus, None

        class Cache:
            def get_text(self, *_args):
                return "业绩比较基准 95%×纳斯达克100指数收益率+5%×活期存款利率 风险收益特征"

        _profile, holding_cost, warnings = ranking.resolve_contract_benchmark(
            object(), self.fund, date(2026, 8, 20), Cache(), self.catalog
        )
        self.assertEqual("unavailable", holding_cost["status"])
        self.assertTrue(any("没有可用的人民币产品概要" in warning for warning in warnings))

    def test_parses_official_annualized_holding_cost(self):
        summary = ranking.LegalDocument(
            "s", "产品概要", date(2026, 8, 14), "https://example.test/s.pdf", "product_summary"
        )
        result = ranking.parse_holding_cost(
            "基金运作综合费率 （ 年化 ） 0.66% 注：综合费率测算日期为 2026 年 08 月 13 日。",
            summary,
            date(2026, 8, 20),
        )
        self.assertEqual(0.66, result["annualized_pct"])
        self.assertEqual("2026-08-13", result["measurement_date"])

    def test_parses_official_holding_cost_layout_variants(self):
        summary = ranking.LegalDocument(
            "s", "产品概要", date(2026, 8, 14), "https://example.test/s.pdf", "product_summary"
        )
        fixtures = (
            ("基金运作综合费率（年化）基金运作综合费率 0.80%", 0.8),
            ("基金运作综合费率（年化）5 / 7 基金运作综合费率 0.66%", 0.66),
            ("基金运作综合费率（年化）- 0.66%", 0.66),
        )
        for text, expected in fixtures:
            with self.subTest(text=text):
                result = ranking.parse_holding_cost(text, summary, date(2026, 8, 20))
                self.assertEqual(expected, result["annualized_pct"])

    def test_rejects_missing_or_future_holding_cost_data(self):
        summary = ranking.LegalDocument(
            "s", "产品概要", date(2026, 8, 14), "https://example.test/s.pdf", "product_summary"
        )
        with self.assertRaisesRegex(ranking.DataError, "Could not locate"):
            ranking.parse_holding_cost("未披露综合费率", summary, date(2026, 8, 20))
        with self.assertRaisesRegex(ranking.DataError, "future"):
            ranking.parse_holding_cost(
                "基金运作综合费率（年化）1.20% 测算日期为2026年08月21日",
                summary,
                date(2026, 8, 20),
            )
        with self.assertRaisesRegex(ranking.DataError, "outside"):
            ranking.parse_holding_cost(
                "基金运作综合费率（年化）101.00%", summary, date(2026, 8, 20)
            )
        with self.assertRaisesRegex(ranking.DataError, "publication date"):
            ranking.parse_holding_cost(
                "基金运作综合费率（年化）1.00%",
                ranking.LegalDocument(
                    "future",
                    "未来产品概要",
                    date(2026, 8, 21),
                    "https://example.test/future.pdf",
                    "product_summary",
                ),
                date(2026, 8, 20),
            )

    def test_direct_limit_and_return_drawdown_boundaries(self):
        self.assertFalse(
            ranking.direct_limit_qualifies(
                {"status": "limited", "amount_cny": 199}, 200
            )
        )
        self.assertTrue(
            ranking.direct_limit_qualifies(
                {"status": "limited", "amount_cny": 200}, 200
            )
        )
        self.assertTrue(
            ranking.direct_limit_qualifies(
                {"status": "unlimited", "amount_cny": None}, 200
            )
        )
        score, annualized = ranking.calculate_return_drawdown_ratio(
            {
                "code": "test",
                "three_year_performance_start_date": "2023-08-18",
                "three_year_performance_end_date": "2026-08-18",
                "three_year_return_pct": 80.0,
                "three_year_max_drawdown_pct": 0.0,
            }
        )
        self.assertIsNone(score)
        self.assertGreater(annualized, 0)


class AnnouncementCacheTests(unittest.TestCase):
    @patch.object(ranking, "_announcement_page")
    def test_daily_check_seeds_history_once_and_reuses_snapshot(self, page):
        page_one = {
            "TotalCount": 200,
            "PageSize": 100,
            "Data": [
                {"ID": "report", "TITLE": "某基金2026年第2季度报告", "PUBLISHDATEDesc": "2026-07-20"},
                {"ID": "quota", "TITLE": "某基金限制大额申购公告", "PUBLISHDATEDesc": "2026-08-18"},
                {"ID": "future", "TITLE": "某基金更新招募说明书", "PUBLISHDATEDesc": "2026-08-21"},
            ],
        }
        page_two = {
            "TotalCount": 200,
            "PageSize": 100,
            "Data": [
                {"ID": "prospectus", "TITLE": "某基金更新招募说明书", "PUBLISHDATEDesc": "2026-06-01"},
                {"ID": "summary", "TITLE": "某基金(A类份额)基金产品资料概要更新", "PUBLISHDATEDesc": "2026-06-02"},
            ],
        }
        page.side_effect = lambda _client, _code, index: page_one if index == 1 else page_two
        as_of = date(2026, 8, 20)
        with TemporaryDirectory() as directory:
            cache = ranking.AnnouncementIndexCache(Path(directory))
            first = cache.get(object(), "000043", as_of)
            second = cache.get(object(), "000043", as_of)
            self.assertEqual(3, page.call_count)
            self.assertNotIn("future", {item.announcement_id for item in first.items})
            prospectus, summary = ranking.fetch_latest_legal_documents(
                object(), "000043", as_of, snapshot=second
            )
            self.assertEqual("prospectus", prospectus.announcement_id)
            self.assertEqual("summary", summary.announcement_id)
            self.assertEqual(
                "report",
                ranking.fetch_latest_periodic_report(
                    object(), "000043", as_of, snapshot=second
                ).announcement_id,
            )
            self.assertEqual(
                ["quota"],
                [item["id"] for item in ranking.fetch_announcements(
                    object(), "000043", as_of, snapshot=second
                )],
            )
            self.assertEqual(
                {"checks": 2, "pages_fetched": 3, "full_seeds": 1, "cache_loads": 1, "corrupt_rebuilds": 0},
                cache.stats(),
            )

    @patch.object(ranking, "resolve_contract_benchmark")
    def test_contract_result_cache_invalidates_on_document_change(self, resolve):
        profile = {
            "status": "recognized",
            "prospectus_published_date": "2026-06-01",
            "product_summary_published_date": "2026-06-02",
        }
        holding = {
            "status": "parsed",
            "source_published_date": "2026-06-02",
            "measurement_date": "2026-05-31",
        }
        resolve.return_value = (profile, holding, ["warning"])
        fund = {"code": "000043", "name": "嘉实美国成长股票人民币", "fund_type": "QDII-普通股票"}
        as_of = date(2026, 8, 20)
        catalog = ranking.ContractBenchmarkCatalog(ranking.DEFAULT_CONTRACT_BENCHMARK_CATALOG)
        base_items = (
            ranking.AnnouncementRecord("p", "更新招募说明书", date(2026, 6, 1)),
            ranking.AnnouncementRecord("s", "A类基金产品资料概要", date(2026, 6, 2)),
        )
        first = ranking.FundAnnouncementSnapshot("000043", as_of, base_items)
        changed = ranking.FundAnnouncementSnapshot(
            "000043",
            as_of,
            (base_items[0], ranking.AnnouncementRecord("s2", "A类基金产品资料概要更新", date(2026, 8, 1))),
        )
        with TemporaryDirectory() as directory:
            cache = ranking.ContractProfileResultCache(Path(directory))
            self.assertEqual(
                (profile, holding, ["warning"]),
                cache.get(object(), fund, as_of, object(), catalog, first),
            )
            cache.get(object(), fund, as_of, object(), catalog, first)
            cache.get(object(), fund, as_of, object(), catalog, changed)
        self.assertEqual(2, resolve.call_count)
        self.assertEqual(1, cache.stats()["hits"])
        self.assertEqual(2, cache.stats()["misses"])


class PerformanceTests(unittest.TestCase):
    @patch.object(ranking, "fetch_trailing_performance")
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

    @patch.object(ranking, "fetch_safe_usd_cny_history")
    @patch.object(ranking, "fetch_nasdaq100_history")
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

    @patch.object(ranking, "calculate_performance_from_points")
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


class HtmlOutputTests(unittest.TestCase):
    @staticmethod
    def record(
        rank,
        code,
        name,
        direct_amount,
        agency_amount,
        agency_status="limited",
    ):
        source = f"https://example.test/{code}/notice.pdf?kind=quota&channel=all"
        return {
            "rank": rank,
            "ranking_list": "us_main",
            "routing_reason": "confirmed_us_exposure",
            "code": code,
            "name": name,
            "fund_type": "QDII-普通股票",
            "management_style": "active",
            "product_structure_tags": ["主动", "股票", "大盘成长"],
            "contract_benchmark": {
                "status": "recognized",
                "benchmark_id": "russell-1000-growth",
                "benchmark_name": "罗素1000成长指数",
                "benchmark_text": "罗素1000成长指数收益率×95%+活期存款利率×5%",
                "benchmark_weight_pct": 95.0,
                "market_label": "美国",
                "market_scope": "us",
                "asset_class": "equity",
                "style_label": "大盘成长",
                "structure": "standard",
                "management_style": "active",
                "excluded_target": False,
                "components": [
                    {
                        "benchmark_id": "russell-1000-growth",
                        "benchmark_name": "罗素1000成长指数",
                        "weight_pct": 95.0,
                        "market_scope": "us",
                        "market_label": "美国",
                        "asset_class": "equity",
                        "style_label": "大盘成长",
                        "structure": "standard",
                        "excluded_target": False,
                    }
                ],
                "prospectus_published_date": "2026-06-01",
                "source_url": f"https://example.test/{code}/prospectus.pdf",
                "product_summary_source_url": f"https://example.test/{code}/summary.pdf",
            },
            "holding_cost": {
                "status": "parsed",
                "annualized_pct": 1.23,
                "measurement_date": "2026-05-31",
                "source_url": f"https://example.test/{code}/summary.pdf",
            },
            "fund_page_url": f"https://example.test/fund/{code}?a=1&b=2",
            "inception_date": "2018-01-02",
            "institution_holding_ratio_pct": 12.34,
            "scale_billion_cny": 5.67,
            "scale_report_date": "2026-06-30",
            "nav_history_start_date": "2016-08-18",
            "nav_history_end_date": "2026-08-18",
            "one_year_return_pct": 23.45,
            "one_year_max_drawdown_pct": -12.34,
            "one_year_performance_start_date": "2025-08-18",
            "one_year_performance_end_date": "2026-08-18",
            "three_year_return_pct": 78.9,
            "three_year_max_drawdown_pct": -23.45,
            "three_year_performance_start_date": "2023-08-18",
            "three_year_performance_end_date": "2026-08-18",
            "five_year_return_pct": 123.45,
            "five_year_performance_start_date": "2021-08-18",
            "five_year_performance_end_date": "2026-08-18",
            "ten_year_return_pct": 234.56,
            "ten_year_performance_start_date": "2016-08-18",
            "ten_year_performance_end_date": "2026-08-18",
            "nasdaq100_fit": {
                "correlation": 0.9123,
                "beta": 0.8765,
                "tracking_error_pct": 8.76,
                "observations": 154,
                "start_date": "2023-08-18",
                "end_date": "2026-08-18",
            },
            "us_equity_exposure": {
                "confirmed_pct": 65.43,
                "possible_pct": 72.1,
                "unresolved_pct": 6.67,
                "report_date": "2026-06-30",
                "source_url": f"https://example.test/{code}/report.pdf?a=1&b=2",
            },
            "direct_limit": {
                "status": "limited" if direct_amount is not None else "unlimited",
                "amount_cny": direct_amount,
                "source_url": source,
            },
            "agency_limit": {
                "status": agency_status,
                "amount_cny": agency_amount,
                "source_url": source if agency_status == "limited" else None,
            },
            "share_class_rule": "A/C separate",
            "channel_rule": "direct and agency limits differ",
            "quota_source_urls": [source],
        }

    def payload(self):
        return {
            "run_date": "2026-08-20",
            "holder_report_date": "2025-12-31",
            "filters": {
                "min_scale_billion_cny": None,
                "min_age_years": 3,
                "min_three_year_return_pct": 30.0,
                "min_five_year_return_pct_if_available": 50.0,
                "min_ten_year_return_pct_if_available": 100.0,
                "min_us_equity_pct": 50.0,
                "min_direct_limit_cny_inclusive": 200,
                "us_main_exclude_keywords": ["亚洲", "中国", "港"],
                "global_exclude_keywords": [],
                "base_candidates_total": 2,
                "contract_candidates_scanned": 2,
            },
            "records": [
                self.record(1, "539002", "建信新兴市场混合(QDII)A", 100000, 500),
                self.record(
                    2,
                    "000043",
                    "嘉实美国成长股票人民币 <script>alert(1)</script>",
                    100000,
                    None,
                    agency_status="unlimited",
                ),
            ],
            "global_supplement": {"records": []},
            "benchmark": {
                "index_source_url": "https://example.test/xndx?a=1&b=2",
                "fx_source_url": "https://example.test/usd-cny?a=1&b=2",
                "index_latest_date": "2026-08-19",
                "fx_latest_date": "2026-08-19",
            },
            "exchange_premium": sample_exchange_premium(),
            "warnings": ["未知标的 <需要核实>"],
        }

    def test_mobile_html_is_self_contained_escaped_and_preserves_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "latest.html"
            ranking.write_html(path, self.payload())
            document = path.read_text(encoding="utf-8")
        self.assertIn('name="viewport"', document)
        self.assertIn("规模不限", document)
        self.assertIn("三年收益 ≥ 30%", document)
        self.assertIn("五年有数据 ≥ 50%", document)
        self.assertIn("十年有数据 ≥ 100%", document)
        self.assertEqual(2, document.count('<details class="fund-item"'))
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", document)
        self.assertIn("未知标的 &lt;需要核实&gt;", document)
        self.assertIn("10万元", document)
        self.assertIn("500元", document)
        self.assertIn("正常开放", document)
        self.assertIn("91.2% · 0.88", document)
        self.assertIn("8.76%", document)
        self.assertIn("154 周", document)
        self.assertIn("a=1&amp;b=2", document)
        self.assertIn('target="_blank" rel="noopener noreferrer"', document)
        self.assertNotIn("<script src=", document)
        self.assertNotIn("<link rel=", document)
        self.assertIn("场内溢价", document)
        self.assertEqual(25, document.count('class="premium-item premium-row'))
        self.assertEqual(1, document.count('class="premium-table"'))
        self.assertEqual(25, document.count('class="premium-row-toggle"'))
        self.assertEqual(25, document.count('data-label="综合费率"'))
        self.assertIn("<th>综合费率</th>", document)
        self.assertIn("0.60%/年", document)
        self.assertEqual(25, document.count("查看费率来源"))
        self.assertIn("综合费率（年化）", document)
        self.assertIn("不含场内券商佣金", document)
        self.assertIn("按溢价从高到低排列", document)
        self.assertEqual(1, document.count('id="premium-refresh"'))
        self.assertIn("行情约延迟 15 分钟", document)
        self.assertIn("QdiiPremiumRefresh", document)

    def test_regression_channel_limits_are_correct_in_each_fund(self):
        payload = self.payload()
        payload["records"][1]["agency_limit"] = {
            "status": "limited",
            "amount_cny": 100,
            "source_url": "https://example.test/000043/notice.pdf",
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "latest.html"
            ranking.write_html(path, payload)
            document = path.read_text(encoding="utf-8")
        ccb = document.split('data-code="539002"', 1)[1].split("</details>", 1)[0]
        harvest = document.split('data-code="000043"', 1)[1].split("</details>", 1)[0]
        self.assertIn("10万元", ccb)
        self.assertIn("500元", ccb)
        self.assertIn("10万元", harvest)
        self.assertIn("100元", harvest)

    def test_main_writes_latest_html(self):
        with TemporaryDirectory() as directory, patch.object(
            ranking, "build_payload", return_value=self.payload()
        ), patch.object(ranking, "write_json"), patch.object(
            ranking, "write_csv"
        ), patch.object(ranking, "write_markdown"):
            output_dir = Path(directory) / "output"
            publish_dir = Path(directory) / "public"
            result = ranking.main(
                [
                    "--output-dir",
                    str(output_dir),
                    "--publish-dir",
                    str(publish_dir),
                ]
            )
            path = output_dir / "latest.html"
            published_path = publish_dir / "index.html"
            self.assertEqual(0, result)
            self.assertTrue(path.exists())
            self.assertTrue(published_path.exists())
            self.assertEqual(path.read_bytes(), published_path.read_bytes())
            self.assertIn("QDII 榜单与场内溢价", path.read_text(encoding="utf-8"))


class PeriodicReportTests(unittest.TestCase):
    def test_parses_numeric_and_chinese_periodic_report_titles(self):
        self.assertEqual(
            date(2026, 6, 30),
            ranking.parse_periodic_report_date("某基金2026年第2季度报告"),
        )
        self.assertEqual(
            date(2026, 6, 30),
            ranking.parse_periodic_report_date("某基金二〇二六年半年度报告"),
        )
        self.assertEqual(
            date(2025, 12, 31),
            ranking.parse_periodic_report_date("某基金2025年年度报告"),
        )
        self.assertIsNone(
            ranking.parse_periodic_report_date("某基金2026年第2季度报告摘要")
        )
        self.assertIsNone(
            ranking.parse_periodic_report_date("某基金2026年年度报告提示性公告")
        )

    def test_selects_latest_report_without_lookahead(self):
        class Client:
            def get_json(self, *_args, **_kwargs):
                return {
                    "Data": [
                        {
                            "TITLE": "某基金2026年第2季度报告",
                            "PUBLISHDATEDesc": "2026-07-20",
                            "ID": "future",
                        },
                        {
                            "TITLE": "某基金2026年第1季度报告",
                            "PUBLISHDATEDesc": "2026-04-20",
                            "ID": "current",
                        },
                        {
                            "TITLE": "某基金2025年年度报告摘要",
                            "PUBLISHDATEDesc": "2026-03-20",
                            "ID": "summary",
                        },
                    ]
                }

        report = ranking.fetch_latest_periodic_report(
            Client(), "000001", date(2026, 6, 30)
        )
        self.assertEqual("current", report.announcement_id)
        self.assertEqual(date(2026, 3, 31), report.report_date)

    def test_parses_direct_holdings_fof_and_unreported_residual(self):
        report_text = """
        4.期末基金资产净值 100.00
        5.期末基金份额净值 1.00
        5.1 报告期末基金资产组合情况
        1 权益投资 20.00 20.00
        2 基金投资 60.00 60.00
        9 合计 100.00 100.00
        5.2 报告期末在各个国家（地区）证券市场的股票及存托凭证投资分 布
        国家 公允价值 占基金资产净值比例
        美国 10.00 10.00
        合计 20.00 20.00
        5.3 行业分类
        5.9 报告期末按公允价值排序的前十名基金投资明 细
        序号 基金名称 基金类型 运作方式 管理人 公允价值 占基金资产净值比例
        1 TEST US ETF 指数基 金 开放式 Test Manager 40.00 40.00
        2 TEST OTHER ETF ETF 交易型开放式 Test Manager 10.00 10.00
        3 TEST STOCK ETF 股 票 型 交易型开放式 Test Manager 5.00 5.00
        4 TEST COMMODITY ETF 商 品 型 开放式 Test Manager 5.00 5.00
        5 TEST GOLD ETF ETF 契约型开放式 Test Manager 5.00 5.00
        6 TEST EQUITY ETF 权 益 类 交易型开放式 Test Manager 5.00 5.00
        5.10 投资组合报告附注
        """
        parsed = ranking.parse_us_equity_report(report_text, "fixture")
        self.assertEqual(10.0, parsed["direct_us_pct"])
        self.assertEqual(60.0, parsed["fund_investment_pct"])
        self.assertEqual(40.0, parsed["fund_holdings"][0]["weight_pct"])
        self.assertEqual("commodity", parsed["fund_holdings"][3]["reported_category"])

    def test_parses_explicit_no_us_and_no_fund_holdings(self):
        report_text = """
        5.1 报告期末基金资产组合情况
        1 权益投资 20.00 20.00
        2 基金投资 --
        9 合计 100.00 100.00
        5.2 报告期末在各个国家（地区）证券市场的股票及存托凭证投资分布
        日本 20.00 20.00
        合计 20.00 20.00
        5.3 行业分类
        """
        parsed = ranking.parse_us_equity_report(report_text, "fixture")
        self.assertEqual(0.0, parsed["direct_us_pct"])
        self.assertEqual(0.0, parsed["fund_investment_pct"])
        self.assertEqual([], parsed["fund_holdings"])

    def test_parses_dash_percentage_and_skips_placeholder_rows(self):
        report_text = """
        前十名基金投资明细
        序号 基金名称 基金类型 运作方式 管理人 公允价值 占基金资产净值比例（%）
        1 SAMPLE ETF ETF 交易型开放式 Test Manager 1,000.00 1.00
        2 ZERO ETF ETF 交易型开放式 Test Manager 990.00 -
        3 - - - - - -
        4 - - - - - - 华夏测试基金 90
        投资组合报告附注
        """
        rows = ranking.parse_fund_investment_rows(report_text, "fixture")
        self.assertEqual(2, len(rows))
        self.assertEqual(1.0, rows[0]["weight_pct"])
        self.assertEqual(0.0, rows[1]["weight_pct"])

    def test_parses_midyear_report_asset_heading(self):
        report_text = """
        7.1 期末基金资产组合情况 43
        7.2 期末在各个国家（地区）证券市场的权益投资分布 44
        7.10 期末按公允价值排序的前十名基金投资明细 52
        7.11 投资组合报告附注 53
        6.1 资产负债表
        净资产合计 1,077,948,242.67 842,733,049.63
        6.2 利润表
        7.1 期末基金资产组合情况
        1 权益投资 20.00 20.00
        2 基金投资 7,387,734.86 0.65
        9 合计 100.00 100.00
        7.2 期末在各个国家（地区）证券市场的权益投资分布
        英国 20.00 20.00
        合计 20.00 20.00
        7.3 期末按行业分类的权益投资组合
        7.10 期末按公允价值排序的前十名基金投资明细
        1 SCOTTISH MORTGAGE 权益类 封闭式 Baillie Gifford 7,387,734.86 0.69
        7.11 投资组合报告附注
        """
        parsed = ranking.parse_us_equity_report(report_text, "539003")
        self.assertEqual(0.0, parsed["direct_us_pct"])
        self.assertAlmostEqual(0.6854, parsed["fund_investment_pct"], places=4)
        self.assertEqual(0.69, parsed["fund_holdings"][0]["weight_pct"])

    def test_parses_wrapped_depository_receipt_heading(self):
        report_text = """
        5.1 报告期末基金资产组合情况
        1 权益投资 306,976,360.04 89.48
        2 基金投资 - -
        9 合计 343,048,323.73 100.00
        5.2 报告期末在各个国家（地区）证券市场的股票及存托 凭证投资分布
        国家（地区） 公允价值（人民币元） 占基金资产净值比例（%）
        美国 288,684,697.04 88.57
        中国香港 18,291,663.00 5.61
        合计 306,976,360.04 94.18
        5.3 报告期末按行业分类
        """
        parsed = ranking.parse_us_equity_report(report_text, "017144")
        self.assertEqual(88.57, parsed["direct_us_pct"])
        self.assertEqual(0.0, parsed["fund_investment_pct"])

    def test_sums_wrapped_multi_share_net_assets(self):
        report_text = """
        4.期末基金资产
        净值
        人民币A类 1,000,000,00
        0.00
        人民币C类 500,000,00
        0.00
        5.期末基金份额
        净值 1.00
        5.1 报告期末基金资产组合情况
        1 权益投资 --
        2 基金投资 1,350,000,000.00 90.00
        9 合计 1,500,000,000.00 100.00
        5.2 报告期末在各个国家（地区）证券市场的股票及存托凭证投资分布
        合计 --
        5.3 行业分类
        5.9 报告期末按公允价值排序的前十名基金投资明细
        1 纳指 ETF 汇添富 ETF基金 交易型开放式 汇添富基金 1,350,000,000.00 90.00
        5.10 投资组合报告附注
        """
        parsed = ranking.parse_us_equity_report(report_text, "018966")
        self.assertEqual(90.0, parsed["fund_investment_pct"])
        self.assertEqual(90.0, parsed["fund_holdings"][0]["weight_pct"])

    def test_parses_etf_label_without_space_from_wrapped_pdf_table(self):
        text = """
        前十名基金投资明细
        序号 基金名称 基金类型 运作方式 管理人 公允价值 占基金资产净值比例
        1
        CSOP SK Hynix Dai
        ly 2x Leveraged Product
        ETF基金 交易型开放式 CSOP Asset Management Ltd 433,536,732.50 7.44
        2
        TEST US EQUITY ETF
        ETF基金 交易型开放式 Test Manager 87,289,275.00 1.50
        5.10 投资组合报告附注
        """
        rows = ranking.parse_fund_investment_rows(text, "016664")
        self.assertEqual(2, len(rows))
        self.assertEqual(7.44, rows[0]["weight_pct"])
        self.assertIn("CSOP SK Hynix", rows[0]["fund_name"])

    def test_parses_qdii_label_from_domestic_target_etf_row(self):
        text = """
        前十名基金投资明细
        序号 基金名称 基金类型 运作方式 管理人 公允价值 占基金资产净值比例
        1
        大成纳斯
        达克
        100ETF
        （QDII）
        QDII 交易型开
        放式
        大成基金
        管理有限
        公司
        5,416,495,992.39 90.32
        5.10 投资组合报告附注
        """
        rows = ranking.parse_fund_investment_rows(text, "000834")
        self.assertEqual(1, len(rows))
        self.assertEqual("大成纳斯 达克 100ETF （QDII）", rows[0]["fund_name"])
        self.assertEqual(90.32, rows[0]["weight_pct"])

    def test_repairs_money_values_split_across_pdf_lines(self):
        cleaned = ranking.clean_report_text(
            "18,353,859,95\n6.59 next 6,792,717,042\n.48 end"
        )
        self.assertIn("18,353,859,956.59", cleaned)
        self.assertIn("6,792,717,042.48", cleaned)

    def test_pdf_cache_hits_and_redownloads_corruption(self):
        class Client:
            calls = 0

            def get_bytes(self, *_args, **_kwargs):
                self.calls += 1
                return b"%PDF-" + b"x" * 1000

        report = ranking.PeriodicReport(
            "report-id",
            "report",
            date(2026, 6, 30),
            date(2026, 7, 20),
            "https://example.test/report.pdf",
        )
        client = Client()

        def validate(value):
            if value.startswith(b"%PDF-") and len(value) >= 1000:
                return "parsed"
            raise ranking.DataError("corrupt")

        with TemporaryDirectory() as directory, patch.object(
            ranking.PeriodicReportCache, "_validate", side_effect=validate
        ):
            cache = ranking.PeriodicReportCache(Path(directory))
            self.assertEqual("parsed", cache.get_text(client, report, "referer"))
            self.assertEqual("parsed", cache.get_text(client, report, "referer"))
            (Path(directory) / "report-id.pdf").write_bytes(b"bad")
            self.assertEqual("parsed", cache.get_text(client, report, "referer"))
            self.assertEqual(2, client.calls)
            self.assertEqual(
                {
                    "hits": 1,
                    "downloads": 2,
                    "corrupt_redownloads": 1,
                    "text_extractions": 3,
                },
                cache.stats(),
            )


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

    @patch.object(ranking, "calculate_us_equity_exposure_base")
    @patch.object(ranking, "parse_us_equity_report")
    @patch.object(ranking, "fetch_latest_periodic_report")
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

    @patch.object(ranking, "fetch_latest_periodic_report")
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

    @patch.object(ranking, "fetch_us_equity_exposure")
    @patch.object(ranking, "fetch_trailing_performance")
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

    @patch.object(ranking, "fetch_us_equity_exposure")
    @patch.object(ranking, "fetch_trailing_performance")
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

    @patch.object(ranking, "fetch_us_equity_exposure")
    @patch.object(ranking, "fetch_trailing_performance")
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


class QuotaNoticeTests(unittest.TestCase):
    def test_business_cap_title_is_selected(self):
        title = "关于调整某基金人民币销售申购、定期定额申购业务上限的公告"
        self.assertIsNotNone(ranking.NOTICE_TITLE_RE.search(title))

    def test_rmb_sales_business_cap_applies_to_all_channels(self):
        text = """
        自2026年4月13日起调整人民币销售的申购、定期定额申购业务上限，
        即单个投资者单日累计申购（含定期定额申购）申请华夏全球股票
        （QDII）（人民币）（000041）的金额应不超过人民币1万元。
        """
        base = ranking.parse_quota_notice(
            text, date(2026, 4, 13), "https://example.test/chinaamc.pdf"
        )[0]
        self.assertEqual(10000, base["global_amount_cny"])
        self.assertIsNone(base["direct_amount_cny"])
        self.assertIsNone(base["agency_amount_cny"])

    def test_table_limit_allows_space_before_unit(self):
        text = "限制申购金额 （单位：元） 1,000.00"
        base = ranking.parse_quota_notice(
            text, date(2026, 6, 5), "https://example.test/southern.pdf"
        )[0]
        self.assertEqual(1000, base["global_amount_cny"])

    def test_limit_with_currency_after_amount_and_pdf_line_breaks(self):
        text = """
        暂停大额 申购起始 日 2026 年 07 月 17 日
        自 2026 年 07 月 17 日起，人民币 A 调整大额申购业务，单日单个基金账户
        单笔或多笔累计申购、定期定额投资的金额不应超过 10 人民币元（含 10 人民币元）。
        """
        base = ranking.parse_quota_notice(
            text, date(2026, 7, 17), "https://example.test/htffund.pdf"
        )[0]
        self.assertEqual(date(2026, 7, 17), base["effective_date"])
        self.assertEqual(10, base["global_amount_cny"])

    def test_cumulative_amount_limit_wording(self):
        text = "单日申购、定期定额投资及转换转入金额累计限额为10.00元"
        base = ranking.parse_quota_notice(
            text, date(2026, 7, 27), "https://example.test/yinhua.pdf"
        )[0]
        self.assertEqual(10, base["global_amount_cny"])

    def test_split_chinese_and_numeric_pdf_runs_are_rejoined(self):
        text = "业\n务\n限\n额\n为\n5\n,\n000\n.\n00\n元"
        base = ranking.parse_quota_notice(
            text, date(2026, 2, 4), "https://example.test/gf.pdf"
        )[0]
        self.assertEqual(5000, base["global_amount_cny"])

    @patch.object(ranking, "fetch_announcements")
    def test_newer_unparsed_notice_invalidates_older_limit(self, announcements):
        announcements.return_value = [
            {
                "id": "old-notice",
                "title": "调整大额申购限制金额的公告",
                "published": date(2026, 7, 10),
                "url": "https://example.test/old-notice.pdf",
            },
            {
                "id": "new-notice",
                "title": "调整大额申购限制金额的公告",
                "published": date(2026, 7, 17),
                "url": "https://example.test/new-notice.pdf",
            },
        ]

        class Cache:
            def get_text(self, _client, document, _referer):
                if document.announcement_id == "old-notice":
                    return "申购业务限额为1,000.00元"
                return "公告正文未能提取额度"

        quota, warnings = ranking.resolve_quota(
            object(),
            {
                "code": "018966",
                "purchase_status": "limited",
                "page_agency_limit_cny": 10,
                "fund_page_url": "https://example.test/018966",
            },
            date(2026, 8, 21),
            Cache(),
        )
        self.assertEqual("unknown", quota["direct_limit"]["status"])
        self.assertEqual("unknown", quota["agency_limit"]["status"])
        self.assertTrue(any("produced no effective limit transition" in w for w in warnings))

    def test_pdf_split_table_label_and_direct_channel_override(self):
        text = """
        暂停大额 申购起始 日 2026 年 8 月 18 日
        下属分级基金的限制申购 金额（单 位： 人民币 元 ） 500.00 500.00
        自2026 年8 月18 日起，对投资者单日单个基金账户累计高于500 元的申购进行限制
        （不同份额分别计算）。针对在建信基金直销渠道投资的情况，单日单个基金账户
        累计申购金额高于10 万元，本基金管理人有权拒绝高于10 万元的部分金额。
        """
        base = ranking.parse_quota_notice(
            text, date(2026, 8, 17), "https://example.test/ccbfund.pdf"
        )[0]
        self.assertEqual(date(2026, 8, 18), base["effective_date"])
        self.assertEqual(500, base["global_amount_cny"])
        self.assertEqual(100000, base["direct_amount_cny"])
        self.assertEqual("A/C separate", base["share_aggregation"])

    def test_direct_and_non_direct_sales_are_not_swapped(self):
        text = """
        限制申购金额 100 元人民币。
        投资者通过直销销售机构单个开放日每个基金账户累计申购金额不得超过10万元人民币；
        投资者通过非直销销售机构单个开放日每个基金账户累计申购金额不得超过100元人民币。
        """
        base = ranking.parse_quota_notice(
            text, date(2025, 11, 4), "https://example.test/harvest.pdf"
        )[0]
        self.assertEqual(100000, base["direct_amount_cny"])
        self.assertEqual(100, base["agency_amount_cny"])
        self.assertEqual(100, base["global_amount_cny"])

    def test_future_restore_notice_is_not_treated_as_unlimited(self):
        text = "本基金恢复大额申购、定投业务的具体时间将另行公告。"
        self.assertEqual(
            [],
            ranking.parse_quota_notice(
                text, date(2026, 6, 5), "https://example.test/ccbfund.pdf"
            ),
        )

    def test_channel_specific_limits(self):
        text = """
        调整大额申购起始日 2026年7月17日
        自2026年7月17日起调整直销机构的大额申购业务。单日每个基金账户
        累计申购A类基金份额、C类基金份额的合计金额超过10万元有权拒绝。
        继续暂停办理代销机构1000元以上的大额申购业务。
        """
        transitions = ranking.parse_quota_notice(
            text, date(2026, 7, 17), "https://example.test/yinhua.pdf"
        )
        base = transitions[0]
        self.assertEqual(100000, base["direct_amount_cny"])
        self.assertEqual(1000, base["agency_amount_cny"])
        self.assertEqual("A/C combined", base["share_aggregation"])

    def test_channel_limits_with_amount_above_wording(self):
        text = """
        银华基金管理股份有限公司关于旗下部分基金暂停及恢复申购业务的公告。
        自2026年9月8日起银华海外数字经济量化选股混合型发起式证券投资基金（QDII）
        继续暂停直销机构10万元以上及代销机构1000元以上的大额申购（含定期定额投资）业务。
        """
        transitions = ranking.parse_quota_notice(
            text, date(2026, 9, 3), "https://example.test/yinhua-20260903.pdf"
        )
        self.assertEqual(1, len(transitions))
        base = transitions[0]
        self.assertEqual(date(2026, 9, 8), base["effective_date"])
        self.assertEqual(100000, base["direct_amount_cny"])
        self.assertEqual(1000, base["agency_amount_cny"])

    def test_all_channel_limit(self):
        text = """
        暂停大额申购起始日 2026年8月18日
        限制申购金额（单位：人民币元） 50,000.00
        自2026年8月18日起，单日单个基金账户在全部销售机构累计申购
        本基金A类人民币份额或C类人民币份额的金额不超过5万元，分别计算。
        """
        base = ranking.parse_quota_notice(
            text, date(2026, 8, 18), "https://example.test/efunds.pdf"
        )[0]
        self.assertEqual(50000, base["global_amount_cny"])
        self.assertTrue(base["all_channels_combined"])
        self.assertEqual("A/C separate", base["share_aggregation"])

    def test_separate_wording_variants(self):
        text = """
        暂停大额申购起始日 2026年8月14日
        限制申购金额（单位：人民币元）200.00
        人民币A类份额和人民币C类份额分开计算进行限制。
        """
        base = ranking.parse_quota_notice(
            text, date(2026, 8, 13), "https://example.test/ftsfund.pdf"
        )[0]
        self.assertEqual("A/C separate", base["share_aggregation"])

    def test_business_limit_and_multiple_channel_wording(self):
        text = """
        自2026年8月11日起，本基金A类人民币份额、C类人民币份额个人投资者
        单日单个基金账户申购业务限额为2,000.00元。
        个人投资者通过多家销售渠道的多笔申购申请将累计计算，
        不同份额的申请将单独计算限额。
        """
        base = ranking.parse_quota_notice(
            text, date(2026, 8, 10), "https://example.test/gf.pdf"
        )[0]
        self.assertEqual(2000, base["global_amount_cny"])
        self.assertTrue(base["all_channels_combined"])
        self.assertEqual("A/C separate", base["share_aggregation"])

    def test_automatic_restore_transition(self):
        text = """
        暂停大额申购及定期定额投资起始日 2026年4月30日
        限制大额申购及定期定额投资金额（单位：元） 100
        自2026年5月6日起，本基金将恢复暂停接受单日单个基金账户单笔或累计
        超过500万元的申购申请。
        """
        transitions = ranking.parse_quota_notice(
            text, date(2026, 4, 30), "https://example.test/bocim.pdf"
        )
        amounts = {
            item["effective_date"].isoformat(): item["global_amount_cny"] for item in transitions
        }
        self.assertEqual(100, amounts["2026-04-30"])
        self.assertEqual(5000000, amounts["2026-05-06"])


class QuotaParseCacheTests(unittest.TestCase):
    def test_reuses_success_and_failure_by_announcement_id(self):
        class Documents:
            def __init__(self):
                self.calls = 0

            def get_text(self, *_args):
                self.calls += 1
                return "notice text"

        fund = {"code": "000001", "fund_page_url": "https://example.test/fund"}
        notice = {
            "id": "notice-1",
            "title": "限制大额申购公告",
            "published": date(2026, 8, 18),
            "url": "https://example.test/notice-1.pdf",
        }
        transition = {
            "effective_date": date(2026, 8, 18),
            "direct_amount_cny": 200,
            "agency_amount_cny": None,
            "global_amount_cny": None,
            "global_status": "limited",
            "source_url": notice["url"],
            "published_date": "2026-08-18",
            "share_aggregation": None,
            "all_channels_combined": False,
            "confidence": "high",
        }
        documents = Documents()
        with TemporaryDirectory() as directory:
            cache = ranking.QuotaNoticeParseCache(Path(directory))
            with patch.object(ranking, "parse_quota_notice", return_value=[transition]) as parse:
                self.assertEqual([transition], cache.get(object(), fund, notice, documents))
                self.assertEqual([transition], cache.get(object(), fund, notice, documents))
                self.assertEqual(1, parse.call_count)
            failed = {**notice, "id": "notice-2", "url": "https://example.test/notice-2.pdf"}
            with patch.object(ranking, "parse_quota_notice", return_value=[]) as parse:
                with self.assertRaisesRegex(ranking.DataError, "no effective"):
                    cache.get(object(), fund, failed, documents)
                with self.assertRaisesRegex(ranking.DataError, "no effective"):
                    cache.get(object(), fund, failed, documents)
                self.assertEqual(1, parse.call_count)
        self.assertEqual(2, documents.calls)
        self.assertEqual(2, cache.stats()["hits"])
        self.assertEqual(2, cache.stats()["misses"])
        self.assertEqual(1, cache.stats()["failures"])


if __name__ == "__main__":
    unittest.main()
