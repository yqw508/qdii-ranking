import json
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from qdii_ranking.assemblers import build_nasdaq100_otc_records
from qdii_ranking.errors import DataError
from qdii_ranking.models import AnnouncementRecord, FundAnnouncementSnapshot, LegalDocument
from qdii_ranking.ranking import build_holder_candidates
from qdii_ranking.services.otc_shares import discover_nasdaq100_otc_candidates
from qdii_ranking.sources.candidates import (
    build_nasdaq100_otc_candidates, is_otc_share, is_rmb_a_share,
)
from qdii_ranking.sources.contracts import unreadable_contract_benchmark, unavailable_holding_cost
from qdii_ranking.sources.otc_shares import needs_primary_share_verification, parse_primary_share_evidence


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "otc-shares"
TEXT = (FIXTURES / "160213-product-overview.txt").read_text(encoding="utf-8")
META = {"code": "160213", "name": "国泰纳斯达克100指数", "fund_type": "指数型-海外股票"}
AS_OF = date(2026, 9, 22)
ANNOUNCEMENT = AnnouncementRecord(
    "AN202609211829696259", "国泰纳斯达克100指数证券投资基金基金产品资料概要更新", date(2026, 9, 21)
)
SUMMARY = LegalDocument(
    ANNOUNCEMENT.announcement_id, ANNOUNCEMENT.title, ANNOUNCEMENT.published_date,
    ANNOUNCEMENT.source_url, "product_summary",
)


def evidence():
    return parse_primary_share_evidence(TEXT, META, SUMMARY, AS_OF)


def caches(items=(ANNOUNCEMENT,)):
    announcements, documents = Mock(), Mock()
    announcements.get.return_value = FundAnnouncementSnapshot("160213", AS_OF, items)
    documents.get_text.return_value = TEXT
    return announcements, documents


class PrimaryShareParserTests(unittest.TestCase):
    def test_real_overview_is_missed_by_original_filter_and_verified_locally(self):
        self.assertFalse(is_rmb_a_share(META))
        self.assertFalse(is_otc_share(META))
        self.assertEqual([], build_nasdaq100_otc_candidates({"160213": META}, []))
        result = evidence()
        self.assertEqual(META["name"], result["name"])
        self.assertEqual("160213", result["code"])
        self.assertEqual("CNY", result["currency"])
        self.assertEqual("普通开放式", result["operation_mode"])
        self.assertEqual(ANNOUNCEMENT.source_url, result["source_url"])
        # The real investment section contains USD and ETF references, neither is a share gate.
        self.assertIn("美元", TEXT)
        self.assertIn("ETF", TEXT)

    def test_wrapped_chinese_labels_and_values(self):
        wrapped = TEXT
        for text in ("产品概况", "基金代码", "基金简称", "交易币种", "人民币", "运作方式", "普通开放式"):
            wrapped = wrapped.replace(text, "\n".join(text))
        self.assertEqual(evidence(), parse_primary_share_evidence(wrapped, META, SUMMARY, AS_OF))

    def test_rejects_missing_conflicting_or_ambiguous_overview(self):
        changes = [
            ("160213", "160214"),
            ("基金代码 160213", "基金代码 160213 160214"),
            ("基金代码 160213", "基金代码 160213 下属基金份额简称 A类 C类"),
            ("交易币种 人民币", "交易币种 美元"),
            ("交易币种 人民币", "交易币种 港币"),
            ("交易币种 人民币", "交易币种 人民币/美元"),
            ("交易币种 人民币", "交易币种 人民币 交易币种 美元"),
            ("交易币种 人民币", ""),
            ("普通开放式", "交易型开放式"),
            ("基金简称 国泰", "基金简称 其他"),
            ("（QDII） 基金代码", "C（QDII） 基金代码"),
            ("一、产品概况", "产品资料"),
            ("二、基金投资", "基金投资"),
        ]
        for before, after in changes:
            with self.subTest(after=after), self.assertRaises(DataError):
                parse_primary_share_evidence(TEXT.replace(before, after), META, SUMMARY, AS_OF)

    def test_explicit_nontarget_shares_never_reach_verification(self):
        for suffix in (
            "美元", "USD", "港币", "HKD", "后端", "A", "人民币", "ETF",
            "C", "D", "E", "F", "I", "C类", "D份额", "(E)", "F(QDII)", "(QDII)I",
        ):
            with self.subTest(suffix=suffix):
                meta = {**META, "name": META["name"] + suffix}
                self.assertFalse(needs_primary_share_verification(meta))
                with self.assertRaises(DataError):
                    parse_primary_share_evidence(TEXT, meta, SUMMARY, AS_OF)
        for kind in ("指数型-股票", "股票型", "QDII-纯债", "QDII-商品"):
            self.assertFalse(needs_primary_share_verification({**META, "fund_type": kind}))
        self.assertFalse(needs_primary_share_verification({**META, "name": "国泰标普500指数"}))

    def test_future_document_is_rejected_even_when_passed_directly(self):
        with self.assertRaises(DataError):
            parse_primary_share_evidence(TEXT, META, SUMMARY, date(2026, 9, 20))

    def test_verification_is_not_specific_to_a_fund_code_or_structure_acronym(self):
        meta = {**META, "code": "123456", "name": "某纳斯达克100指数"}
        text = TEXT.replace("160213", "123456").replace("国泰", "某")
        self.assertEqual("123456", parse_primary_share_evidence(text, meta, SUMMARY, AS_OF)["code"])
        for name in ("某纳斯达克100指数LOF", "某纳斯达克100指数(QDII)", "NASDAQ 100 INDEX"):
            self.assertTrue(needs_primary_share_verification({**meta, "name": name}), name)


class PrimaryShareDiscoveryTests(unittest.TestCase):
    def test_fixed_universe_grows_16_to_17_with_no_shared_filter_change(self):
        metadata = {m["code"]: m for m in json.loads((FIXTURES / "candidates-20260922.json").read_text(encoding="utf-8"))}
        original = deepcopy(metadata)
        rows = [[code, "", "20", "80", "", "1"] for code in metadata]
        old_main = build_holder_candidates(rows, metadata, [])
        old_otc = build_nasdaq100_otc_candidates(metadata, rows)
        announcements, documents = caches()
        candidates, snapshots, warnings = discover_nasdaq100_otc_candidates(
            Mock(), metadata, rows, AS_OF, announcements, documents,
        )
        self.assertEqual(16, len(old_otc))
        self.assertEqual(17, len(candidates))
        self.assertEqual(1, sum(item["code"] == "160213" for item in candidates))
        added = next(item for item in candidates if item["code"] == "160213")
        self.assertEqual(META["name"], added["name"])
        self.assertEqual(20, added["institution_holding_ratio_pct"])
        self.assertEqual(evidence(), added["share_class_evidence"])
        self.assertEqual([], warnings)
        self.assertEqual(old_main, build_holder_candidates(rows, metadata, []))
        self.assertEqual(old_otc, build_nasdaq100_otc_candidates(metadata, rows))
        self.assertEqual(original, metadata)
        self.assertIs(snapshots["160213"], announcements.get.return_value)
        announcements.get.assert_called_once()

    def test_missing_future_unreadable_and_invalid_summaries_fail_closed(self):
        future = AnnouncementRecord("AN202609231234567890", ANNOUNCEMENT.title, date(2026, 9, 23))
        for items, text in (((), TEXT), ((future,), TEXT), ((ANNOUNCEMENT,), "broken"), ((ANNOUNCEMENT,), DataError("bad PDF"))):
            with self.subTest(items=items, text=str(text)[:10]):
                announcements, documents = caches(items)
                if isinstance(text, Exception):
                    documents.get_text.side_effect = text
                else:
                    documents.get_text.return_value = text
                candidates, _, warnings = discover_nasdaq100_otc_candidates(
                    Mock(), {"160213": META}, [], AS_OF, announcements, documents,
                )
                self.assertEqual([], candidates)
                self.assertEqual(1, len(warnings))
                self.assertIn("160213", warnings[0])
                self.assertIn("未纳入", warnings[0])

    def test_latest_visible_summary_is_checked_without_falling_back_after_parse_error(self):
        older = AnnouncementRecord("AN202608011234567890", ANNOUNCEMENT.title, date(2026, 8, 1))
        future = AnnouncementRecord("AN202609231234567890", ANNOUNCEMENT.title, date(2026, 9, 23))
        announcements, documents = caches((future, older, ANNOUNCEMENT))
        documents.get_text.return_value = "broken latest"
        result, _, _ = discover_nasdaq100_otc_candidates(Mock(), {"160213": META}, [], AS_OF, announcements, documents)
        self.assertEqual([], result)
        self.assertEqual(ANNOUNCEMENT.announcement_id, documents.get_text.call_args.args[1].announcement_id)
        documents.get_text.assert_called_once()
        # A fresh run must reverify the overview; no successful-eligibility cache is kept.
        documents.get_text.return_value = TEXT
        result, _, _ = discover_nasdaq100_otc_candidates(Mock(), {"160213": META}, [], AS_OF, announcements, documents)
        self.assertEqual(1, len(result))
        self.assertEqual(2, announcements.get.call_count)
        self.assertEqual(2, documents.get_text.call_count)

    def test_assembler_reuses_snapshot_and_ignores_holder_quota_and_performance_gates(self):
        for status in ("suspended", "limited"):
            with self.subTest(status=status):
                announcements, documents = caches()
                performance, contracts = Mock(), Mock()
                performance.get.return_value = ({"three_year_return_pct": -10}, [])
                contracts.get.return_value = (
                    {**unreadable_contract_benchmark(META), "management_style": "passive"},
                    unavailable_holding_cost(None), [],
                )
                page = {
                    "inception_date": "2010-04-29", "purchase_status": status,
                    "purchase_status_text": "暂停申购" if status == "suspended" else "限购100元",
                    "fund_page_url": "https://fund.eastmoney.com/160213.html",
                }
                with patch("qdii_ranking.assemblers.parse_fund_page", return_value=page), patch(
                    "qdii_ranking.assemblers.apply_common_window", return_value=({}, [])
                ) as common:
                    records, _, _, _ = build_nasdaq100_otc_records(
                        Mock(), {"160213": META}, [], [], AS_OF, "2026-06-30", Mock(),
                        performance, announcements, contracts, documents, Mock(),
                    )
                self.assertEqual(1, len(records))
                self.assertEqual(status, records[0]["purchase_status"])
                self.assertEqual("not_evaluated", records[0]["quota_status"])
                self.assertIsNone(records[0]["institution_holding_ratio_pct"])
                self.assertEqual(evidence(), records[0]["share_class_evidence"])
                announcements.get.assert_called_once()
                self.assertIs(announcements.get.return_value, contracts.get.call_args.args[-1])
                self.assertEqual("160213", common.call_args.args[0][0]["code"])
