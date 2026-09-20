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
                "three_year_boundary_tolerance_days": 7,
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
        with TemporaryDirectory() as directory, patch(
            "qdii_ranking.cli.build_payload", return_value=self.payload()
        ), patch("qdii_ranking.cli.write_json"), patch(
            "qdii_ranking.cli.write_csv"
        ), patch("qdii_ranking.cli.write_markdown"):
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

    def test_parses_stock_index_fund_label_from_wrapped_pdf_table(self):
        text = """
        前十名基金投资明细
        序号 基金名称 基金类型 运作方式 管理人 公允价值 占基金资产净值比例
        1
        景顺长城纳斯达克科技市值加权交易型开放式指数证券投资基金（QDII）
        股票型指数基金
        交易型开放式(ETF)
        景顺长城基金管理有限公司
        4,694,211,198.44 94.15
        7.11 投资组合报告附注
        """
        rows = ranking.parse_fund_investment_rows(text, "017091")
        self.assertEqual(1, len(rows))
        self.assertEqual(94.15, rows[0]["weight_pct"])
        self.assertIn("景顺长城纳斯达克科技市值加权", rows[0]["fund_name"])

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
            self.assertEqual(
                "parsed",
                cache.get_text(client, report, "referer", force_refresh=True),
            )
            (Path(directory) / "report-id.pdf").write_bytes(b"bad")
            self.assertEqual("parsed", cache.get_text(client, report, "referer"))
            self.assertEqual(3, client.calls)
            self.assertEqual(
                {
                    "hits": 1,
                    "downloads": 3,
                    "corrupt_redownloads": 1,
                    "text_extractions": 4,
                },
                cache.stats(),
            )
