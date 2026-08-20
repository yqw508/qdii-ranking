import unittest
import json
import threading
import time
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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


class FundFilterTests(unittest.TestCase):
    def test_default_result_count_and_return_threshold(self):
        args = ranking.parse_args([])
        self.assertEqual(10, args.top)
        self.assertEqual(50.0, args.min_three_year_return_pct)
        self.assertEqual(50.0, args.min_us_equity_pct)
        self.assertEqual("public", args.publish_dir.name)

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


class PerformanceTests(unittest.TestCase):
    @patch.object(ranking, "fetch_trailing_performance")
    def test_three_year_threshold_full_scan_includes_exact_match(self, fetch):
        fetch.side_effect = [
            ({"three_year_return_pct": 49.99}, []),
            ({"three_year_return_pct": 50.0}, []),
            ({"three_year_return_pct": 80.0}, []),
        ]
        candidates = [{"code": str(index)} for index in range(3)]
        selected, warnings, scanned = ranking.filter_performance_full_scan(
            object(), candidates, date(2026, 8, 19), 50.0, top=2
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


class PerformanceCacheTests(unittest.TestCase):
    @staticmethod
    def result():
        return {
            "performance_source_url": "https://example.test/performance.js",
            "one_year_return_pct": 20.0,
            "one_year_max_drawdown_pct": -10.0,
            "one_year_performance_start_date": "2025-08-19",
            "one_year_performance_end_date": "2026-08-19",
            "three_year_return_pct": 60.0,
            "three_year_max_drawdown_pct": -20.0,
            "three_year_performance_start_date": "2023-08-19",
            "three_year_performance_end_date": "2026-08-19",
        }

    @patch.object(ranking, "fetch_trailing_performance")
    def test_cache_hits_and_rebuilds_future_or_corrupt_data(self, fetch):
        fetch.return_value = (self.result(), ["cached warning"])
        fund = {"code": "000001", "fund_page_url": "https://example.test/fund"}
        as_of = date(2026, 8, 19)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = ranking.PerformanceResultCache(root)
            self.assertEqual(self.result(), cache.get(object(), fund, as_of)[0])
            self.assertEqual(self.result(), cache.get(object(), fund, as_of)[0])
            path = root / as_of.isoformat() / "000001.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["performance"]["three_year_performance_end_date"] = "2026-08-20"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(self.result(), cache.get(object(), fund, as_of)[0])
        self.assertEqual(2, fetch.call_count)
        self.assertEqual(
            {"hits": 1, "misses": 2, "corrupt_rebuilds": 1}, cache.stats()
        )


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
            "code": code,
            "name": name,
            "fund_page_url": f"https://example.test/fund/{code}?a=1&b=2",
            "inception_date": "2018-01-02",
            "institution_holding_ratio_pct": 12.34,
            "scale_billion_cny": 5.67,
            "scale_report_date": "2026-06-30",
            "one_year_return_pct": 23.45,
            "one_year_max_drawdown_pct": -12.34,
            "one_year_performance_start_date": "2025-08-18",
            "one_year_performance_end_date": "2026-08-18",
            "three_year_return_pct": 78.9,
            "three_year_max_drawdown_pct": -23.45,
            "three_year_performance_start_date": "2023-08-18",
            "three_year_performance_end_date": "2026-08-18",
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
        }

    def payload(self):
        return {
            "run_date": "2026-08-20",
            "holder_report_date": "2025-12-31",
            "filters": {
                "min_scale_billion_cny": 3.0,
                "min_age_years": 3,
                "min_three_year_return_pct": 50.0,
                "min_us_equity_pct": 50.0,
                "base_candidates_total": 2,
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
            "warnings": ["未知标的 <需要核实>"],
        }

    def test_mobile_html_is_self_contained_escaped_and_preserves_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "latest.html"
            ranking.write_html(path, self.payload())
            document = path.read_text(encoding="utf-8")
        self.assertIn('name="viewport"', document)
        self.assertEqual(2, document.count('<details class="fund-item"'))
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", document)
        self.assertIn("未知标的 &lt;需要核实&gt;", document)
        self.assertIn("10万元", document)
        self.assertIn("500元", document)
        self.assertIn("正常开放", document)
        self.assertIn("a=1&amp;b=2", document)
        self.assertIn('target="_blank" rel="noopener noreferrer"', document)
        self.assertNotIn("<script src=", document)
        self.assertNotIn("<link rel=", document)

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
            self.assertIn("QDII 美股含量榜单", path.read_text(encoding="utf-8"))


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
                {"hits": 1, "downloads": 2, "corrupt_redownloads": 1},
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
    def test_full_scan_does_not_stop_at_top_and_ranks_by_exposure(
        self, performance, exposure
    ):
        concurrency = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        def performance_result(_client, fund, _as_of):
            code = int(fund["code"])
            with lock:
                concurrency["active"] += 1
                concurrency["maximum"] = max(
                    concurrency["maximum"], concurrency["active"]
                )
            time.sleep(0.02)
            with lock:
                concurrency["active"] -= 1
            return {"three_year_return_pct": 60.0 + code}, [f"performance {code}"]

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
        self.assertEqual(6, ranking.PERFORMANCE_WORKERS)
        self.assertEqual(6, concurrency["maximum"])
        self.assertEqual(
            [f"performance {index}" for index in range(12)], warnings
        )

    @patch.object(ranking, "fetch_us_equity_exposure")
    @patch.object(ranking, "fetch_trailing_performance")
    def test_us_exposure_tie_breakers_are_deterministic(self, performance, exposure):
        performance_by_code = {"001": 100.0, "002": 90.0, "003": 90.0, "004": 80.0}
        exposure_by_code = {"001": 71.0, "002": 70.0, "003": 70.0, "004": 70.0}
        institution_by_code = {"001": 10.0, "002": 20.0, "003": 20.0, "004": 20.0}
        performance.side_effect = lambda _client, fund, _as_of: (
            {"three_year_return_pct": performance_by_code[fund["code"]]},
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
        self.assertEqual(["001", "002", "003", "004"], [item["code"] for item in selected])


class QuotaNoticeTests(unittest.TestCase):
    def test_table_limit_allows_space_before_unit(self):
        text = "限制申购金额 （单位：元） 1,000.00"
        base = ranking.parse_quota_notice(
            text, date(2026, 6, 5), "https://example.test/southern.pdf"
        )[0]
        self.assertEqual(1000, base["global_amount_cny"])

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


if __name__ == "__main__":
    unittest.main()
