import json
import random
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from qdii_ranking.assemblers import build_nasdaq100_otc_records
from qdii_ranking.cache.fee_sums import FeeSumCache
from qdii_ranking.models import AnnouncementRecord, FundAnnouncementSnapshot
from qdii_ranking.runtime import http_category
from qdii_ranking.sources.contracts import unavailable_holding_cost, unreadable_contract_benchmark
from qdii_ranking.sources.fee_components import fetch_fee_sums

AS_OF = date(2026, 11, 24)
PAGE = (
    "<tr><th>管理费率</th><td>0.50%（每年）</td><th>托管费率</th><td>0.15%（每年）</td></tr>"
    "<tr><th>销售服务费率</th><td>0.20%（每年）</td></tr>"
)


def snapshot(*items):
    return FundAnnouncementSnapshot("019442", AS_OF, tuple(items))


class FeeSumCacheTests(unittest.TestCase):
    def run_fetch(self, directory, client, snap=None, as_of=AS_OF):
        cache = FeeSumCache(Path(directory))
        costs, warnings = fetch_fee_sums(client, ["019442"], as_of, cache, lambda code: snap)
        return cache, costs["019442"], warnings

    def client(self, page=PAGE):
        client = Mock()
        client.get_text.return_value = page
        return client

    def test_fresh_cache_is_reused_and_keeps_real_fetch_date(self):
        with TemporaryDirectory() as directory:
            first_day = AS_OF - timedelta(days=2)
            self.run_fetch(directory, self.client(), snapshot(), as_of=first_day)
            client = self.client()
            cache, cost, _ = self.run_fetch(directory, client, snapshot())
            client.get_text.assert_not_called()
            self.assertEqual(first_day.isoformat(), cost["source_published_date"])
            self.assertEqual(0.85, cost["annualized_pct"])
            self.assertEqual(1, cache.stats()["hits"])

    def test_expiry_and_fee_announcement_force_refetch(self):
        cases = {
            "expired": (AS_OF - timedelta(days=7), snapshot()),
            "revalidated_by_announcement": (
                AS_OF - timedelta(days=2),
                snapshot(AnnouncementRecord("A1", "关于调低管理费率的公告", AS_OF - timedelta(days=1))),
            ),
            "no_snapshot": (AS_OF - timedelta(days=2), None),
        }
        for reason, (fetched_on, snap) in cases.items():
            with self.subTest(reason), TemporaryDirectory() as directory:
                self.run_fetch(directory, self.client(), snapshot(), as_of=fetched_on)
                client = self.client()
                cache, cost, _ = self.run_fetch(directory, client, snap)
                client.get_text.assert_called_once()
                self.assertEqual(AS_OF.isoformat(), cost["source_published_date"])
                self.assertEqual(1, cache.stats()[reason])

    def test_unrelated_announcement_keeps_cache(self):
        with TemporaryDirectory() as directory:
            self.run_fetch(directory, self.client(), snapshot(), as_of=AS_OF - timedelta(days=2))
            client = self.client()
            self.run_fetch(directory, client, snapshot(
                AnnouncementRecord("A2", "基金经理变更公告", AS_OF - timedelta(days=1)),
            ))
            client.get_text.assert_not_called()

    def test_failure_is_unavailable_and_never_falls_back_or_persists(self):
        with TemporaryDirectory() as directory:
            self.run_fetch(directory, self.client(), snapshot(), as_of=AS_OF - timedelta(days=8))
            cache, cost, warnings = self.run_fetch(directory, self.client(page=""), snapshot())
            self.assertEqual("unavailable", cost["status"])
            self.assertEqual(1, len(warnings))
            self.assertEqual(1, cache.stats()["failures"])
            saved = json.loads((Path(directory) / "019442.json").read_text(encoding="utf-8"))
            self.assertEqual("fee_sum", saved["cost"]["status"])  # old file untouched, not replaced

    def test_corrupt_cache_is_rebuilt(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "019442.json").write_text("{bad", encoding="utf-8")
            client = self.client()
            cache, cost, _ = self.run_fetch(directory, client, snapshot())
            client.get_text.assert_called_once()
            self.assertEqual("fee_sum", cost["status"])
            self.assertEqual(1, cache.stats()["corrupt_rebuilds"])


class ConcurrentNasdaqTests(unittest.TestCase):
    def test_concurrent_order_matches_input_and_warnings_are_ordered(self):
        metadata = {
            f"0190{index:02d}": {
                "code": f"0190{index:02d}", "name": f"某纳斯达克100指数(QDII)A{index}",
                "fund_type": "指数型-海外股票",
            }
            for index in range(12)
        }
        # Same names would collapse the share class; give each a clean A label.
        for meta in metadata.values():
            meta["name"] = f"某{meta['code']}纳斯达克100指数(QDII)A"

        def slow_performance(_client, fund, *_args):
            time.sleep(random.uniform(0, 0.02))
            return {}, [f"perf-{fund['code']}"]

        performance, contracts = Mock(), Mock()
        performance.get.side_effect = slow_performance
        contracts.get.side_effect = lambda _c, fund, *_a: (
            {**unreadable_contract_benchmark(fund), "management_style": "passive"},
            unavailable_holding_cost(None), [],
        )

        def window(items, *_args):
            for index, item in enumerate(items):
                item["common_period_return_pct"] = 100.0 - index
            return {"status": "available"}, []

        page = {"inception_date": "2020-01-01", "purchase_status": "open", "purchase_status_text": "开放",
                "fund_page_url": "https://fund.eastmoney.com/x.html"}
        client = Mock()
        client.get_text.return_value = ""
        with patch("qdii_ranking.assemblers.parse_fund_page", return_value=page), patch(
            "qdii_ranking.assemblers.apply_common_window", side_effect=window
        ):
            records, warnings, *_ = build_nasdaq100_otc_records(
                client, metadata, [], [], AS_OF, "2026-09-30", Mock(), performance, Mock(),
                contracts, Mock(), Mock(),
            )
        codes = sorted(metadata)
        self.assertEqual(12, performance.get.call_count)
        self.assertEqual(codes, [item["code"] for item in records])
        self.assertEqual(
            [f"场外纳指100告警 {code}：perf-{code}" for code in codes],
            [item for item in warnings if "perf-" in item],
        )


class StaggeredExpiryTests(unittest.TestCase):
    def test_expiry_days_are_fixed_per_code_and_spread_over_five_to_seven(self):
        from qdii_ranking.cache.fee_sums import fee_sum_max_age_days
        codes = [f"{index:06d}" for index in range(171)]
        days = [fee_sum_max_age_days(code) for code in codes]
        self.assertEqual(days, [fee_sum_max_age_days(code) for code in codes])
        self.assertEqual({5, 6, 7}, set(days))

    def test_code_refetches_only_when_its_own_lifetime_ends(self):
        from qdii_ranking.cache.fee_sums import fee_sum_max_age_days
        code = next(c for c in (f"{i:06d}" for i in range(1000)) if fee_sum_max_age_days(c) == 5)
        cache = FeeSumCache(Path("unused"))
        cost = {"source_published_date": (AS_OF - timedelta(days=4)).isoformat()}
        self.assertIsNone(cache.refresh_reason(cost, snapshot(), AS_OF, code))
        cost = {"source_published_date": (AS_OF - timedelta(days=5)).isoformat()}
        self.assertEqual("expired", cache.refresh_reason(cost, snapshot(), AS_OF, code))


class ManualIncludeTests(unittest.TestCase):
    def build(self, metadata, manual):
        performance, contracts = Mock(), Mock()
        performance.get.return_value = ({}, [])
        contracts.get.side_effect = lambda _c, fund, *_a: (
            {**unreadable_contract_benchmark(fund), "management_style": "passive"},
            unavailable_holding_cost(None), [],
        )
        page = {"inception_date": "2020-01-01", "purchase_status": "open", "purchase_status_text": "开放",
                "fund_page_url": "https://fund.eastmoney.com/x.html"}

        def window(items, *_args):
            for index, item in enumerate(items):
                item["common_period_return_pct"] = 50.0 - index
            return {"status": "available"}, []

        client = Mock()
        client.get_text.return_value = ""
        with patch("qdii_ranking.assemblers.parse_fund_page", return_value=page), patch(
            "qdii_ranking.assemblers.apply_common_window", side_effect=window
        ), patch("qdii_ranking.assemblers.NASDAQ100_OTC_MANUAL_CODES", manual):
            return build_nasdaq100_otc_records(
                client, metadata, [], [], AS_OF, "2026-09-30", Mock(), performance, Mock(),
                contracts, Mock(), Mock(),
            )

    def test_manual_fund_joins_with_manual_routing(self):
        metadata = {
            "018043": {"code": "018043", "name": "天弘纳斯达克100指数发起(QDII)A", "fund_type": "指数型-海外股票"},
            "022523": {"code": "022523", "name": "天弘标普500发起(QDII-FOF)D", "fund_type": "QDII-FOF"},
        }
        records, *_ = self.build(metadata, ("022523", "018043"))
        by_code = {item["code"]: item for item in records}
        self.assertEqual("manual_include", by_code["022523"]["routing_reason"])
        self.assertTrue(by_code["022523"]["manual_include"])
        self.assertEqual("D", by_code["022523"]["share_class"])
        self.assertEqual("指定对比", by_code["022523"]["name_match_rule"])
        self.assertEqual("name_match", by_code["018043"]["routing_reason"])
        self.assertEqual(2, len(records))

    def test_missing_metadata_only_warns(self):
        metadata = {"018043": {"code": "018043", "name": "天弘纳斯达克100指数发起(QDII)A", "fund_type": "指数型-海外股票"}}
        records, warnings, *_ = self.build(metadata, ("022523",))
        self.assertEqual(["018043"], [item["code"] for item in records])
        self.assertTrue(any("022523" in item and "指定对比" in item for item in warnings))

class HttpCategoryTests(unittest.TestCase):
    def test_fund_archive_is_its_own_category(self):
        self.assertEqual("fund_archive", http_category("https://fundf10.eastmoney.com/jbgk_019442.html"))
        self.assertEqual("fund_page", http_category("https://fund.eastmoney.com/019442.html"))


if __name__ == "__main__":
    unittest.main()
