import copy
import unittest
from html.parser import HTMLParser
from pathlib import Path

from qdii_ranking.report_comparison_details import (
    holding_changes,
    load_comparison_details,
    render_comparison_detail,
    validate_comparison_details,
)
from qdii_ranking.report_comparisons import ReportComparisonError, load_report_comparison_index


ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "references/report-comparisons/2026-q2-vs-q3.json"
CODES = ["017436", "501226", "270023", "539002", "006555", "017144", "164212", "012920"]


class TopTenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_table = False
        self.in_body = False
        self.rows = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table = dict(attrs).get("id") == "top-ten"
        if tag == "tbody" and self.in_table:
            self.in_body = True
        if tag == "tr" and self.in_table and self.in_body:
            self.rows += 1

    def handle_endtag(self, tag):
        if tag == "tbody":
            self.in_body = False
        if tag == "table":
            self.in_table = False


class ReportComparisonDetailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_comparison_details(DATA)

    def test_all_eight_reports_are_paired_and_pages_reproduce_curated_evidence(self):
        self.assertEqual(CODES, list(self.data["funds"]))
        index = load_report_comparison_index(ROOT / "public/report-comparisons/index.json")
        for code in CODES:
            with self.subTest(code=code):
                page = render_comparison_detail(self.data, code)
                self.assertEqual(page, (ROOT / "public/report-comparisons" / code / "2026-q2-vs-q3.html").read_text(encoding="utf-8"))
                parser = TopTenParser()
                parser.feed(page)
                self.assertEqual(10, parser.rows)
                for period in self.data["funds"][code]["periods"]:
                    self.assertIn(period["source_url"], page)
                    self.assertIn("#page=" + str(period["pages"]["holdings"]), page)
                self.assertEqual(" ".join(self.data["funds"][code]["insights"]), index["funds"][code]["comparisons"][0]["summary"])
                self.assertNotIn("见报告组合表", page)

    def test_corrected_returns_and_industry_evidence(self):
        fund = self.data["funds"]["501226"]
        self.assertEqual("-12.69", fund["periods"][1]["classes"][0]["return_pct"])
        self.assertEqual("-11.59", fund["periods"][1]["benchmark_pct"])
        self.assertEqual("70.17", fund["periods"][1]["sectors"]["信息科技"])
        fund = self.data["funds"]["270023"]
        self.assertEqual(["66.89", "56.75"], [p["sectors"]["信息技术"] for p in fund["periods"]])
        self.assertIn("2026-09-21", render_comparison_detail(self.data, "539002"))
        self.assertIn("2026-06-01", render_comparison_detail(self.data, "012920"))

    def test_changes_match_company_identity_and_do_not_impute_undisclosed_weights(self):
        rows = holding_changes(self.data["funds"]["017436"])
        meta = next(r for r in rows if r[0] == "Meta Platforms")
        self.assertEqual("+3.51 个百分点", meta[3])
        self.assertEqual("第 8 → 1 名", meta[4])
        sandisk = next(r for r in rows if "Sandisk" in r[0])
        self.assertEqual("未进入该期前十大", sandisk[1])
        self.assertIn("不可比", sandisk[3])
        zhongji = next(r for r in holding_changes(self.data["funds"]["012920"]) if "中际旭创" in r[0])
        self.assertEqual("-0.23 个百分点", zhongji[3])

    def test_rejects_missing_quarter_bad_rank_duplicate_identity_and_nonfinite_value(self):
        for mutation in ("quarter", "rank", "identity", "number", "nav", "coverage"):
            with self.subTest(mutation=mutation):
                data = copy.deepcopy(self.data)
                fund = data["funds"]["017436"]
                if mutation == "quarter":
                    fund["periods"].pop()
                elif mutation == "rank":
                    fund["periods"][0]["holdings"][-1]["rank"] = 9
                elif mutation == "identity":
                    fund["periods"][0]["holdings"][-1]["issuer_id"] = fund["periods"][0]["holdings"][0]["issuer_id"]
                elif mutation == "number":
                    fund["periods"][0]["assets"]["权益投资"] = "NaN"
                elif mutation == "nav":
                    fund["periods"][0]["classes"][0]["nav"] = "-1"
                else:
                    fund["periods"][0]["assets"]["权益投资"] = "1"
                with self.assertRaises(ReportComparisonError):
                    validate_comparison_details(data)

    def test_html_escapes_names_discussion_and_source_urls(self):
        data = copy.deepcopy(self.data)
        fund = data["funds"]["017436"]
        fund["name"] = '基金 <script> & "名称"'
        fund["periods"][0]["holdings"][0]["name"] = '持仓 <b> & "名称"'
        fund["periods"][0]["strategy"] = ['说明 <script> & "内容"']
        fund["periods"][0]["source_url"] += '?a=1&b="value"'
        page = render_comparison_detail(data, "017436")
        self.assertNotIn("<script>", page)
        self.assertIn('基金 &lt;script&gt; &amp; &quot;名称&quot;', page)
        self.assertIn('持仓 &lt;b&gt; &amp; &quot;名称&quot;', page)
        self.assertIn('?a=1&amp;b=&quot;value&quot;', page)


class IndexEnhancedComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_comparison_details(ROOT / "references/report-comparisons/2026-q2-vs-q3-519981.json")

    def test_rmb_primary_share_and_usd_benchmark_are_explicit(self):
        fund = self.data["funds"]["519981"]
        self.assertEqual("人民币", fund["primary_share_label"])
        self.assertEqual(["14.30", "-1.44"], [p["classes"][0]["return_pct"] for p in fund["periods"]])
        self.assertEqual(["12.55", "0.39"], [p["benchmark_pct"] for p in fund["periods"]])
        page = render_comparison_detail(self.data, "519981")
        self.assertIn("人民币份额单季收益", page)
        self.assertNotIn("A 类", page)
        self.assertIn("美元计价", page)
        self.assertIn("不是剔除汇率影响后的增强收益", page)
        self.assertIn("全基金并表资产净值（亿元）", page)
        self.assertIn("17.1402", page)
        self.assertIn("25.9431", page)

    def test_index_top_ten_and_active_top_five_are_kept_separate(self):
        fund = self.data["funds"]["519981"]
        p2, p3 = fund["periods"]
        self.assertEqual(10, len(p2["holdings"]))
        self.assertEqual(5, len(p2["active_portfolio"]["holdings"]))
        self.assertEqual("AMAT", p2["holdings"][0]["issuer_id"])
        self.assertEqual("TMO", p3["holdings"][0]["issuer_id"])
        self.assertEqual("MRVL", p2["active_portfolio"]["holdings"][0]["issuer_id"])
        self.assertEqual("META", p3["active_portfolio"]["holdings"][0]["issuer_id"])
        self.assertEqual(["76.22", "80.85"], [p["sectors_total_pct"] for p in fund["periods"]])
        self.assertEqual(["9.74", "8.21"], [p["active_portfolio"]["sectors_total_pct"] for p in fund["periods"]])
        # Intel exists in both index-quarter lists but only Q3's disclosed active list.
        index_intel = next(r for r in holding_changes(fund) if "(INTC)" in r[0])
        active_intel = next(r for r in holding_changes(fund, active=True) if "(INTC)" in r[0])
        self.assertEqual("-0.04 个百分点", index_intel[3])
        self.assertEqual("未进入该期前五大", active_intel[1])
        self.assertIn("不可比", active_intel[3])
        page = render_comparison_detail(self.data, "519981")
        self.assertIn("不是全基金合并前十大", page)
        self.assertIn('id="active-top-five"', page)
        self.assertIn("§5.4.1", page)
        self.assertIn("§5.4.2", page)
        parser = TopTenParser()
        parser.feed(page)
        self.assertEqual(10, parser.rows)
        self.assertEqual(page, (ROOT / "public/report-comparisons/519981/2026-q2-vs-q3.html").read_text(encoding="utf-8"))

    def test_incomplete_or_inconsistent_sleeve_evidence_is_rejected(self):
        for mutation in ("one_period", "five_ranks", "industry_total", "equity_total", "locator", "scope"):
            with self.subTest(mutation=mutation):
                data = copy.deepcopy(self.data)
                fund = data["funds"]["519981"]
                p = fund["periods"][0]
                if mutation == "one_period":
                    del p["active_portfolio"]
                elif mutation == "five_ranks":
                    p["active_portfolio"]["holdings"].pop()
                elif mutation == "industry_total":
                    p["active_portfolio"]["sectors_total_pct"] = "20.00"
                elif mutation == "equity_total":
                    p["regions"]["美国"] = "95.95"
                elif mutation == "locator":
                    del p["pages"]["active_holdings"]
                else:
                    del fund["holdings_scope_note"]
                with self.assertRaises(ReportComparisonError):
                    validate_comparison_details(data)


if __name__ == "__main__":
    unittest.main()
