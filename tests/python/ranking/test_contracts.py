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

    @patch("qdii_ranking.sources.contracts._announcement_page")
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

    @patch("qdii_ranking.sources.contracts.fetch_latest_legal_documents")
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

    @patch("qdii_ranking.sources.contracts.fetch_latest_legal_documents")
    def test_legal_document_index_failure_is_non_blocking(self, documents):
        documents.side_effect = ranking.DataError("source unavailable")
        profile, holding_cost, warnings = ranking.resolve_contract_benchmark(
            object(), self.fund, date(2026, 8, 20), object(), self.catalog
        )
        self.assertEqual("unreadable", profile["status"])
        self.assertEqual("unreadable", profile["product_summary_status"])
        self.assertEqual("unavailable", holding_cost["status"])
        self.assertEqual(2, len(warnings))

    @patch("qdii_ranking.sources.contracts.fetch_latest_legal_documents")
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
    @patch("qdii_ranking.cache.announcements._announcement_page")
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

    @patch("qdii_ranking.cache.contracts.resolve_contract_benchmark")
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
