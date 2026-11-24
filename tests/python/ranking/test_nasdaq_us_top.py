import unittest
from datetime import date
from unittest.mock import Mock, patch

from qdii_ranking.assemblers import build_nasdaq100_otc_records
from qdii_ranking.sources.contracts import unavailable_holding_cost, unreadable_contract_benchmark

AS_OF = date(2026, 11, 24)
NASDAQ_A = {"code": "018043", "name": "天弘纳斯达克100指数发起(QDII)A", "fund_type": "指数型-海外股票"}
NASDAQ_YOUNG = {"code": "023422", "name": "建信纳斯达克100指数(QDII)D人民币", "fund_type": "指数型-海外股票"}
US_FIRST = {"code": "000043", "name": "嘉实美国成长股票人民币", "fund_type": "QDII-股票"}
US_SECOND = {"code": "016701", "name": "银华海外数字经济量化选股混合发起式(QDII)A", "fund_type": "QDII-混合偏股"}
US_FOURTH = {"code": "001668", "name": "汇添富全球移动互联混合(QDII)人民币A", "fund_type": "QDII-混合偏股"}
METADATA = {item["code"]: item for item in (NASDAQ_A, NASDAQ_YOUNG, US_FIRST, US_SECOND, US_FOURTH)}


def run(us_main_top, young_codes=("023422",)):
    performance, contracts = Mock(), Mock()
    performance.get.return_value = ({}, [])
    contracts.get.side_effect = lambda _c, fund, *_a: (
        {**unreadable_contract_benchmark(fund), "management_style": "passive"},
        unavailable_holding_cost(None), [],
    )
    page = {"inception_date": "2020-01-01", "purchase_status": "open", "purchase_status_text": "开放申购",
            "fund_page_url": "https://fund.eastmoney.com/x.html"}

    def window(items, *_args):
        for index, item in enumerate(items):
            if item["code"] in young_codes:
                item["common_period_error"] = "成立未满两年，暂不参与公共区间排名。"
            else:
                item["common_period_return_pct"] = 20.0 - index
        return {"status": "available", "comparable_count": 0}, []

    with patch("qdii_ranking.assemblers.parse_fund_page", return_value=page), patch(
        "qdii_ranking.assemblers.apply_common_window", side_effect=window
    ):
        client = Mock()
        client.get_text.return_value = ""  # archive page without fee rows
        return build_nasdaq100_otc_records(
            client, METADATA, [], [], AS_OF, "2026-09-30", Mock(), performance, Mock(), contracts,
            Mock(), Mock(), us_main_top=us_main_top,
        )


class UsMainTopTests(unittest.TestCase):
    def test_top_three_join_and_unlisted_funds_are_excluded(self):
        records, _, _, window, excluded = run([("000043", 1), ("016701", 2)])
        by_code = {item["code"]: item for item in records}
        self.assertEqual({"018043", "000043", "016701"}, set(by_code))
        self.assertNotIn("001668", by_code)
        self.assertEqual(1, by_code["000043"]["us_main_rank"])
        self.assertEqual("us_main_top", by_code["000043"]["routing_reason"])
        self.assertEqual("name_match", by_code["018043"]["routing_reason"])
        self.assertIsNone(by_code["018043"]["us_main_rank"])
        self.assertEqual([1, 2, 3], [item["rank"] for item in records])
        self.assertEqual(3, window["comparable_count"])
        self.assertEqual(["023422"], [item["code"] for item in excluded])
        self.assertIn("未满两年", excluded[0]["reason"])

    def test_name_matched_us_top_fund_is_not_duplicated(self):
        records, *_ = run([("018043", 1)])
        matches = [item for item in records if item["code"] == "018043"]
        self.assertEqual(1, len(matches))
        self.assertEqual(1, matches[0]["us_main_rank"])
        self.assertEqual("name_match", matches[0]["routing_reason"])

    def test_us_top_fund_without_return_is_excluded_not_listed(self):
        records, _, _, _, excluded = run([("000043", 1)], young_codes=("023422", "000043"))
        self.assertNotIn("000043", {item["code"] for item in records})
        self.assertIn("000043", {item["code"] for item in excluded})


if __name__ == "__main__":
    unittest.main()
