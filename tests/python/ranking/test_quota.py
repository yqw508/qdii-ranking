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

    @patch("qdii_ranking.sources.quota.fetch_announcements")
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
    def test_reuses_success_across_runs_and_failure_within_run(self):
        class Documents:
            def __init__(self):
                self.calls = 0

            def get_text(self, *_args, **kwargs):
                self.calls += 1
                if kwargs.get("force_refresh") is not True:
                    raise AssertionError("quota cache misses must refresh the PDF")
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
            with patch(
                "qdii_ranking.cache.quota.parse_quota_notice",
                return_value=[transition],
            ) as parse:
                self.assertEqual([transition], cache.get(object(), fund, notice, documents))
                self.assertEqual([transition], cache.get(object(), fund, notice, documents))
                self.assertEqual(1, parse.call_count)
            reloaded_cache = ranking.QuotaNoticeParseCache(Path(directory))
            with patch("qdii_ranking.cache.quota.parse_quota_notice") as parse:
                self.assertEqual(
                    [transition], reloaded_cache.get(object(), fund, notice, documents)
                )
                parse.assert_not_called()
            failed = {**notice, "id": "notice-2", "url": "https://example.test/notice-2.pdf"}
            with patch(
                "qdii_ranking.cache.quota.parse_quota_notice", return_value=[]
            ) as parse:
                with self.assertRaisesRegex(ranking.DataError, "no effective"):
                    cache.get(object(), fund, failed, documents)
                with self.assertRaisesRegex(ranking.DataError, "no effective"):
                    cache.get(
                        object(),
                        {**fund, "code": "000002"},
                        failed,
                        documents,
                    )
                self.assertEqual(1, parse.call_count)
            self.assertFalse((Path(directory) / "notice-2.json").exists())
        self.assertEqual(2, documents.calls)
        self.assertEqual(2, cache.stats()["hits"])
        self.assertEqual(2, cache.stats()["misses"])
        self.assertEqual(1, cache.stats()["failures"])

    def test_retries_failure_in_a_new_run_and_persists_recovery(self):
        class Documents:
            def __init__(self):
                self.calls = 0

            def get_text(self, *_args, **kwargs):
                self.calls += 1
                if kwargs.get("force_refresh") is not True:
                    raise AssertionError("quota cache misses must refresh the PDF")
                return "notice text"

        fund = {"code": "016701", "fund_page_url": "https://example.test/016701"}
        notice = {
            "id": "notice-recovery",
            "title": "调整大额申购限制金额的公告",
            "published": date(2026, 9, 7),
            "url": "https://example.test/notice-recovery.pdf",
        }
        transition = {
            "effective_date": date(2026, 9, 8),
            "direct_amount_cny": 2000000,
            "agency_amount_cny": 1000,
            "global_amount_cny": 2000000,
            "global_status": "limited",
            "source_url": notice["url"],
            "published_date": "2026-09-07",
            "share_aggregation": "A/C combined",
            "all_channels_combined": False,
            "confidence": "high",
        }
        documents = Documents()
        with TemporaryDirectory() as directory:
            first_run = ranking.QuotaNoticeParseCache(Path(directory))
            with patch(
                "qdii_ranking.cache.quota.parse_quota_notice", return_value=[]
            ):
                with self.assertRaisesRegex(ranking.DataError, "no effective"):
                    first_run.get(object(), fund, notice, documents)

            second_run = ranking.QuotaNoticeParseCache(Path(directory))
            with patch(
                "qdii_ranking.cache.quota.parse_quota_notice",
                return_value=[transition],
            ) as parse:
                self.assertEqual(
                    [transition], second_run.get(object(), fund, notice, documents)
                )
                self.assertEqual(1, parse.call_count)

            third_run = ranking.QuotaNoticeParseCache(Path(directory))
            with patch("qdii_ranking.cache.quota.parse_quota_notice") as parse:
                self.assertEqual(
                    [transition], third_run.get(object(), fund, notice, documents)
                )
                parse.assert_not_called()
        self.assertEqual(2, documents.calls)

    def test_retries_legacy_persisted_failure(self):
        class Documents:
            def __init__(self):
                self.calls = 0

            def get_text(self, *_args, **kwargs):
                self.calls += 1
                if kwargs.get("force_refresh") is not True:
                    raise AssertionError("quota cache misses must refresh the PDF")
                return "notice text"

        fund = {"code": "016701", "fund_page_url": "https://example.test/016701"}
        notice = {
            "id": "legacy-failure",
            "title": "调整大额申购限制金额的公告",
            "published": date(2026, 9, 7),
            "url": "https://example.test/legacy-failure.pdf",
        }
        identity = ranking.QuotaNoticeParseCache._identity(notice)
        transition = {
            "effective_date": date(2026, 9, 8),
            "direct_amount_cny": 2000000,
            "agency_amount_cny": 1000,
            "global_amount_cny": 2000000,
            "global_status": "limited",
            "source_url": notice["url"],
            "published_date": "2026-09-07",
            "share_aggregation": "A/C combined",
            "all_channels_combined": False,
            "confidence": "high",
        }
        documents = Documents()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-failure.json"
            ranking.write_json(
                path,
                {
                    "schema_version": ranking.QUOTA_NOTICE_CACHE_SCHEMA_VERSION,
                    "method_version": ranking.QUOTA_NOTICE_METHOD_VERSION,
                    "identity": identity,
                    "ok": False,
                    "error": "quota notice produced no effective limit transition",
                },
            )
            cache = ranking.QuotaNoticeParseCache(Path(directory))
            with patch(
                "qdii_ranking.cache.quota.parse_quota_notice",
                return_value=[transition],
            ) as parse:
                self.assertEqual([transition], cache.get(object(), fund, notice, documents))
                self.assertEqual(1, parse.call_count)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(persisted["ok"])
            self.assertEqual(1, cache.stats()["misses"])
            self.assertEqual(0, cache.stats()["corrupt_rebuilds"])
        self.assertEqual(1, documents.calls)
