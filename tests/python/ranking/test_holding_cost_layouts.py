import unittest
from datetime import date

from qdii_ranking.errors import DataError
from qdii_ranking.models import LegalDocument
from qdii_ranking.sources.contracts import parse_holding_cost
from qdii_ranking.sources.fee_components import parse_fee_components

AS_OF = date(2026, 11, 24)

# Fee sections as extracted by CI from the real product summaries.
WANJIA = (
    "（三） 基金运作综合费用测算 若投资者申购本基金份额，在持有期间，投资者需支出的运作费率如下表： "
    "万家纳斯达克 100 指数发起式(QDII)A 基金运作综合费率（年化） 持有期间 0.68% "
    "万家纳斯达克 100 指数发起式(QDII)C 基金运作综合费率（年化） 持有期间 0.88% "
    "注：基金管理费率、托管费率、销售服务费率（若有）为基金现行费率"
)
BAOYING_A = (
    "若投资者认购/申购本基金份额，在持有期间，投资者需支出的运作费率如下表： "
    "宝盈纳斯达克100 指数发起（QDII）A 人民币 基金运作综合费率（年化） 持有期间 0.66% 四、 风险揭示"
)
BAOYING_C = (
    "宝盈纳斯达克100 指数发起（QDII）C 人民币 基金运作综合费率（年化） 持有期间 0.91% 宝盈纳斯达克 100"
)
TIANHONG_A = "运作费率如下表 ： 基金运作综合费率（年化） 0.60% 注:基金管理费率"


def doc(title):
    return LegalDocument("AN1", title, date(2026, 10, 20), "https://pdf.dfcfw.com/pdf/H2_AN1_1.pdf", "product_summary")


class HoldingCostLayoutTests(unittest.TestCase):
    def rate(self, text, title, share_class=None):
        return parse_holding_cost(text, doc(title), AS_OF, share_class=share_class)["annualized_pct"]

    def test_combined_summary_with_class_before_label(self):
        title = "万家纳斯达克100指数型发起式证券投资基金(QDII)基金产品资料概要更新"
        self.assertEqual(0.68, self.rate(WANJIA, title, "A"))
        self.assertEqual(0.88, self.rate(WANJIA, title, "C"))
        self.assertEqual(0.68, self.rate(WANJIA, title))
        with self.assertRaises(DataError):
            self.rate(WANJIA, title, "D")

    def test_holding_period_between_label_and_rate(self):
        self.assertEqual(0.66, self.rate(BAOYING_A, "宝盈纳斯达克100(宝盈纳斯达克100指数发起(QDII)A人民币份额)基金产品资料概要(更新)", "A"))
        self.assertEqual(0.91, self.rate(BAOYING_C, "宝盈纳斯达克100(宝盈纳斯达克100指数发起(QDII)C人民币份额)基金产品资料概要(更新)", "C"))

    def test_existing_single_rate_layout_unchanged(self):
        self.assertEqual(0.6, self.rate(TIANHONG_A, "天弘纳斯达克100(QDII)(A类份额)基金产品资料概要(更新)", "A"))
        self.assertEqual(0.6, self.rate(TIANHONG_A, "天弘纳斯达克100(QDII)基金产品资料概要(更新)"))


class FeeComponentTests(unittest.TestCase):
    PAGE = (
        "<tr><th>管理费率</th><td>0.50%（每年）</td><th>托管费率</th><td>0.15%（每年）</td></tr>"
        "<tr><th>销售服务费率</th><td>0.20%（每年）</td><th>最高认购费率</th><td>0.00%（前端）</td></tr>"
    )

    def test_sums_three_annual_fees(self):
        cost = parse_fee_components(self.PAGE, "019442", AS_OF)
        self.assertEqual("fee_sum", cost["status"])
        self.assertEqual(0.85, cost["annualized_pct"])
        self.assertEqual(0.2, cost["components"]["sales_service_pct"])
        self.assertEqual("https://fundf10.eastmoney.com/jbgk_019442.html", cost["source_url"])

    def test_dashes_mean_zero(self):
        # 519981 shows "---（每年）" for the sales-service fee; 1.10 + 0.20 = 1.30.
        page = (
            "<tr><th>管理费率</th><td>1.10%（每年）</td><th>托管费率</th><td>0.20%（每年）</td></tr>"
            "<tr><th>销售服务费率</th><td>---（每年）</td><th>最高认购费率</th><td>1.20%（前端）</td></tr>"
        )
        cost = parse_fee_components(page, "519981", AS_OF)
        self.assertEqual(1.3, cost["annualized_pct"])
        self.assertEqual(0.0, cost["components"]["sales_service_pct"])

    def test_missing_or_non_numeric_rate_fails_closed(self):
        for page in (
            self.PAGE.replace("销售服务费率", "其他"),
            self.PAGE.replace("0.50%（每年）", "暂无（每年）"),
            "",
        ):
            with self.subTest(page=page[:30]), self.assertRaises(DataError):
                parse_fee_components(page, "019442", AS_OF)


class ApplyFeeSumTests(unittest.TestCase):
    def test_every_tab_gets_fee_sum_and_keeps_summary_reference(self):
        from unittest.mock import Mock

        from qdii_ranking.sources.fee_components import apply_fee_sums

        summary = {"status": "parsed", "annualized_pct": 1.68}
        payload = {
            "records": [{"code": "519981", "holding_cost": dict(summary)}],
            "global_supplement": {"records": [{"code": "000001", "holding_cost": dict(summary)}]},
            "nasdaq100_otc": {"records": [{"code": "019442", "holding_cost": dict(summary)}]},
            "exchange_premium": {"records": [{"code": "513100", "holding_cost": dict(summary)}]},
        }
        client = Mock()
        client.get_text.side_effect = lambda url, referer=None: (
            "" if "000001" in url else FeeComponentTests.PAGE
        )
        warnings = apply_fee_sums(payload, client, AS_OF)
        nasdaq = payload["nasdaq100_otc"]["records"][0]
        self.assertEqual("fee_sum", nasdaq["holding_cost"]["status"])
        self.assertEqual(0.85, nasdaq["holding_cost"]["annualized_pct"])
        self.assertEqual(summary, nasdaq["summary_holding_cost"])
        failed = payload["global_supplement"]["records"][0]
        self.assertEqual("unavailable", failed["holding_cost"]["status"])
        self.assertEqual(1, len(warnings))
        self.assertTrue(warnings[0].startswith("持有费率告警 000001"))
        self.assertEqual(4, client.get_text.call_count)


if __name__ == "__main__":
    unittest.main()
