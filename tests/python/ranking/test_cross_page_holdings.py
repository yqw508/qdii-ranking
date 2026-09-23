import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from qdii_ranking.errors import DataError
from qdii_ranking.models import PeriodicReport
from qdii_ranking.config import DEFAULT_US_EQUITY_CATALOG
from qdii_ranking.sources.exposure import (
    LookthroughResolver, calculate_us_equity_exposure, clean_report_text,
    parse_fund_investment_rows,
)


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/holdings/019454-cross-page.txt"
TEXT = FIXTURE.read_text(encoding="utf-8")
NAME = "华泰柏瑞中证韩交所中韩半导体交易型开放式指数证券投资基金（QDII）"
HEADER = "华泰柏瑞中韩半导体 ETF 发起式联接（QDII）2026 年中期报告\n第 46页 共 56页"
TAIL = "型开放式\n指数证券\n投资基金\n（QDII）"


def parse(text=TEXT):
    return parse_fund_investment_rows(clean_report_text(text), "019454")


class CrossPageHoldingTests(unittest.TestCase):
    def test_real_report_preserves_full_name_and_only_one_holding(self):
        self.assertEqual([
            {"rank": 1, "fund_name": NAME, "weight_pct": 94.82, "reported_category": None}
        ], parse())

    def test_wrapped_type_and_operation(self):
        for label in ("指数型", "指\n数\n型"):
            with self.subTest(label=label):
                rows = parse(TEXT.replace("指数型", label).replace("交易型开\n放式", "交\n易\n型\n开\n放\n式"))
                self.assertEqual(NAME, rows[0]["fund_name"])

    def test_same_page_name_and_following_row_are_kept_separate(self):
        text = TEXT.replace("华泰柏瑞\n中证韩交\n所中韩半\n导体交易", NAME)
        text = text.replace(HEADER + "\n" + TAIL, "")
        second = "2\n另一指数ETF联接基金\n指数型 普通开放式\n另一基金管理有限公司\n1,000.00 1.23\n"
        text = text.replace("7.11 投资组合报告附注", second + "7.11 投资组合报告附注")
        rows = parse(text)
        self.assertEqual([NAME, "另一指数ETF联接基金"], [r["fund_name"] for r in rows])
        self.assertEqual([94.82, 1.23], [r["weight_pct"] for r in rows])

    def test_cross_page_continuation_does_not_consume_next_row(self):
        second = "2\nOTHER ETF ETF 交易型开放式 Manager 2,000.00 2.00\n"
        rows = parse(TEXT.replace("7.11 投资组合报告附注", second + "7.11 投资组合报告附注"))
        self.assertEqual(2, len(rows))
        self.assertEqual(NAME, rows[0]["fund_name"])
        self.assertEqual("OTHER ETF", rows[1]["fund_name"])

    def test_ambiguous_or_missing_continuation_blocks(self):
        for text in (
            TEXT.replace(HEADER, ""),
            TEXT.replace(TAIL, ""),
            TEXT.replace(TAIL, "序号 基金名称 基金类型\n" + TAIL),
            TEXT.replace(TAIL, "注：" + TAIL),
            TEXT.replace(TAIL, "2\n" + TAIL),
            TEXT.replace(TAIL, TAIL + "\n另一基金管理有限公司"),
            TEXT.replace(TAIL, TAIL + "\n88.00"),
            TEXT.replace(TAIL, "另一交易型开放式指数证券投资基金（QDII）"),
            TEXT.replace(HEADER, "第 46页 共 56页"),
        ):
            with self.subTest(text=text[-300:]), self.assertRaises(DataError):
                parse(text)

    def test_missing_percentage_cannot_use_note_or_section_number(self):
        with self.assertRaises(DataError):
            parse(TEXT.replace(" 94.82", ""))
        with self.assertRaises(DataError):
            parse(TEXT.replace(" 94.82", "").replace(TAIL, TAIL + "\n注：94.82"))

    def test_zero_weight_and_empty_page_tail_for_complete_name(self):
        text = TEXT.replace("华泰柏瑞\n中证韩交\n所中韩半\n导体交易", NAME).replace(TAIL, "")
        self.assertEqual(0.0, parse(text.replace("94.82", "-"))[0]["weight_pct"])

    def test_unknown_holding_remains_only_possible_us_exposure(self):
        report = PeriodicReport(
            "AN202608291828681989", "2026年中期报告", date(2026, 6, 30), date(2026, 8, 29),
            "https://pdf.dfcfw.com/pdf/H2_AN202608291828681989_1.pdf",
        )
        with TemporaryDirectory() as directory:
            resolver = LookthroughResolver(DEFAULT_US_EQUITY_CATALOG, Path(directory) / "underlying.json")
            exposure, warnings = calculate_us_equity_exposure(
                {"direct_us_pct": 0.0, "fund_investment_pct": 94.82, "fund_holdings": parse()},
                report, resolver, 50,
            )
        self.assertEqual((0.0, 94.82), (exposure["confirmed_pct"], exposure["possible_pct"]))
        self.assertEqual("ambiguous", exposure["status"])
        self.assertTrue(any(NAME in w and "94.82%" in w for w in warnings))
