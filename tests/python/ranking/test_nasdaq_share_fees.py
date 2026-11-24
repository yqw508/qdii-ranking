import unittest
from datetime import date

from qdii_ranking.errors import DataError
from qdii_ranking.models import AnnouncementRecord, FundAnnouncementSnapshot, LegalDocument
from qdii_ranking.sources.contracts import (
    fetch_latest_legal_documents,
    parse_holding_cost,
    summary_title_share_class,
)

AS_OF = date(2026, 11, 24)
PROSPECTUS = AnnouncementRecord("AN1", "某纳斯达克100指数基金招募说明书(更新)", date(2026, 6, 1))


def snapshot(code, *items):
    return FundAnnouncementSnapshot(code, AS_OF, (PROSPECTUS, *items))


def summary(title, announcement_id="AN9"):
    return LegalDocument(announcement_id, title, date(2026, 6, 26), f"https://x/{announcement_id}", "product_summary")


class SummarySelectionTests(unittest.TestCase):
    def test_title_class_detection(self):
        self.assertEqual("C", summary_title_share_class("天弘纳斯达克100指数型发起式证券投资基金(QDII)(C类份额)基金产品资料概要(更新)"))
        self.assertEqual("F", summary_title_share_class("广发纳斯达克100联接基金(QDII)(广发纳指100ETF联接(QDII)人民币F)基金产品资料概要更新"))
        self.assertIsNone(summary_title_share_class("万家纳斯达克100指数型发起式证券投资基金(QDII)基金产品资料概要更新"))
        real_titles = {
            "博时纳斯达克100交易型开放式指数证券投资基金发起式联接基金(QDII)(博时纳斯达克100ETF发起式联接(QDII)C人民币)基金产品资料概要更新": "C",
            "博时纳斯达克100交易型开放式指数证券投资基金发起式联接基金(QDII)(博时纳斯达克100ETF发起式联接(QDII)I人民币)基金产品资料概要更新": "I",
            "嘉实纳斯达克100交易型开放式指数证券投资基金发起式联接基金(QDII)(I类人民币份额)基金产品资料概要更新(2026年06月10日)": "I",
            "易方达纳斯达克100交易型开放式指数证券投资基金联接基金(LOF)(易方达纳斯达克100ETF联接(QDII-LOF)C(人民币份额))基金产品资料概要更新": "C",
            "华夏纳斯达克100交易型开放式指数证券投资基金发起式联接基金(QDII)(华夏纳斯达克100ETF发起式联接(QDII)C)基金产品资料概要更新(2026-05-29)": "C",
            "宝盈纳斯达克100指数型发起式证券投资基金(QDII)(宝盈纳斯达克100指数发起(QDII)A人民币份额)基金产品资料概要(更新)": "A",
            "汇添富纳斯达克100ETF发起式联接基金(QDII)人民币E类份额更新基金产品资料概要(2026年08月14日更新)": "E",
            "建信纳斯达克100指数型证券投资基金(QDII)(A类份额人民币)基金产品资料概要更新": "A",
        }
        for title, expected in real_titles.items():
            self.assertEqual(expected, summary_title_share_class(title), title)

    def test_a_share_rejects_other_class_summary(self):
        a = AnnouncementRecord("AN5", "博时纳斯达克100联接基金(QDII)(博时纳斯达克100ETF发起式联接(QDII)A人民币)基金产品资料概要更新", date(2026, 6, 26))
        i = AnnouncementRecord("AN6", "博时纳斯达克100联接基金(QDII)(博时纳斯达克100ETF发起式联接(QDII)I人民币)基金产品资料概要更新", date(2026, 6, 26))
        _, chosen = fetch_latest_legal_documents(None, "016055", AS_OF, snapshot("016055", a, i), share_class="A")
        self.assertEqual("AN5", chosen.announcement_id)

    def test_a_share_unlabeled_summary_uses_single_rate(self):
        doc = summary("万家纳斯达克100指数型发起式证券投资基金(QDII)基金产品资料概要更新")
        text = "基金运作综合费率（年化） 0.65% 测算日期为 2026 年 3 月 31 日"
        self.assertEqual(0.65, parse_holding_cost(text, doc, AS_OF, share_class="A")["annualized_pct"])

    def test_class_share_picks_own_summary_over_newer_other_class(self):
        a = AnnouncementRecord("AN202606261", "某基金(QDII)(A类份额)基金产品资料概要(更新)", date(2026, 6, 27))
        c = AnnouncementRecord("AN202606260", "某基金(QDII)(C类份额)基金产品资料概要(更新)", date(2026, 6, 26))
        _, chosen = fetch_latest_legal_documents(None, "018044", AS_OF, snapshot("018044", a, c), share_class="C")
        self.assertEqual("AN202606260", chosen.announcement_id)

    def test_class_share_falls_back_to_combined_summary_only(self):
        a = AnnouncementRecord("AN2", "某基金(QDII)(A类份额)基金产品资料概要(更新)", date(2026, 6, 27))
        combined = AnnouncementRecord("AN3", "某基金(QDII)基金产品资料概要更新", date(2026, 6, 20))
        _, chosen = fetch_latest_legal_documents(None, "019442", AS_OF, snapshot("019442", a, combined), share_class="C")
        self.assertEqual("AN3", chosen.announcement_id)
        _, none = fetch_latest_legal_documents(None, "019442", AS_OF, snapshot("019442", a), share_class="C")
        self.assertIsNone(none)

    def test_default_selection_unchanged_for_main_ranking(self):
        a = AnnouncementRecord("AN2", "某基金(QDII)(A类份额)基金产品资料概要(更新)", date(2026, 6, 20))
        c = AnnouncementRecord("AN4", "某基金(QDII)人民币C基金产品资料概要(更新)", date(2026, 6, 27))
        _, chosen = fetch_latest_legal_documents(None, "018043", AS_OF, snapshot("018043", a, c))
        self.assertEqual("AN2", chosen.announcement_id)


class ClassHoldingCostTests(unittest.TestCase):
    SINGLE = "基金运作综合费率（年化） 0.95% 测算日期为 2026 年 3 月 31 日"
    COMBINED = "基金运作综合费率（年化） A类份额 0.95% C类份额 1.15% 测算日期为 2026 年 3 月 31 日"

    def test_class_specific_summary_uses_single_rate(self):
        doc = summary("某基金(QDII)(C类份额)基金产品资料概要(更新)")
        self.assertEqual(0.95, parse_holding_cost(self.SINGLE, doc, AS_OF, share_class="C")["annualized_pct"])

    def test_combined_summary_reads_target_class_rate(self):
        doc = summary("某基金(QDII)基金产品资料概要更新")
        self.assertEqual(1.15, parse_holding_cost(self.COMBINED, doc, AS_OF, share_class="C")["annualized_pct"])

    def test_combined_summary_without_class_rate_fails_closed(self):
        doc = summary("某基金(QDII)基金产品资料概要更新")
        with self.assertRaises(DataError):
            parse_holding_cost(self.SINGLE, doc, AS_OF, share_class="C")
        with self.assertRaises(DataError):
            parse_holding_cost(self.COMBINED, doc, AS_OF, share_class="D")


if __name__ == "__main__":
    unittest.main()
