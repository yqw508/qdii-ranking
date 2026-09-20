import json
import io
import math
import threading
import time
import unittest
import urllib.parse
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import update_index_valuation as valuation
import validate_index_valuation as validator


AS_OF = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 7, 7, tzinfo=valuation.SHANGHAI_TZ)

from tests.python.support.valuation import (
    FakeClient,
    dqydj_body,
    fixture_months,
    fixture_series,
    gold_body,
    load_catalog,
    make_cache_bundle,
    make_manifest,
    nasdaq_body,
    ndxtmc_fixture_points,
    ndxtmc_history_body,
    ndxtmc_workbook,
    run_build,
    snowball_body,
)

class SourceParsingTests(unittest.TestCase):
    def test_parses_all_source_shapes_and_allowlist(self):
        prices, pe = fixture_series()
        self.assertEqual(120, len(valuation.parse_nasdaq_history(nasdaq_body(prices["RSP"]), "RSP")))
        self.assertEqual(120, len(valuation.parse_dqydj_pe(dqydj_body(pe))))
        direct = valuation.parse_snowball_snapshot(snowball_body(), {"NDX", "SP500", "GDAXI"})
        self.assertEqual({"NDX", "SP500", "GDAXI"}, {item["code"] for item in direct})
        self.assertEqual("偏高", direct[0]["source_rating"]["label"])
        gold = valuation.parse_gold_snapshot(gold_body(), AS_OF)
        self.assertEqual(96.6, gold["percentiles"]["10y"])
        self.assertEqual(4, gold["factors"]["tips_real_yield"]["lag_days"])
        self.assertNotIn("回测", json.dumps(gold, ensure_ascii=False))

    def test_parses_and_merges_ndxtmc_official_sources(self):
        static = valuation.parse_ndxtmc_workbook(ndxtmc_workbook())
        live = valuation.parse_ndxtmc_history(ndxtmc_history_body())
        valuation.validate_ndxtmc_boundary(static, live)
        merged = valuation.merge_ndxtmc_points(static, live, [live[0]])
        self.assertEqual("2022-03-18", static[-1]["date"])
        self.assertEqual("2022-03-21", live[0]["date"])
        self.assertEqual(len(static) + len(live), len(merged))

    def test_ndxtmc_merge_rejects_conflicting_duplicates(self):
        with self.assertRaisesRegex(valuation.ValuationError, "conflicting values"):
            valuation.merge_ndxtmc_points(
                [{"date": "2022-03-21", "close": 1005.0}],
                [{"date": "2022-03-21", "close": 1006.0}],
            )

    def test_ndxtmc_boundary_rejects_non_trading_gap(self):
        with self.assertRaisesRegex(valuation.ValuationError, "boundary"):
            valuation.validate_ndxtmc_boundary(
                [{"date": "2022-03-18", "close": 1000.0}],
                [{"date": "2022-03-25", "close": 1005.0}],
            )

    def test_snowball_missing_expected_item_is_rejected(self):
        with self.assertRaisesRegex(valuation.ValuationError, "缺少白名单"):
            valuation.parse_snowball_snapshot(
                snowball_body(missing="NDX"), {"NDX", "SP500", "GDAXI"}
            )

    def test_gold_requires_complete_factor_freshness(self):
        with self.assertRaisesRegex(valuation.ValuationError, "美国10年期国债"):
            valuation.parse_gold_snapshot(
                gold_body().replace("美国10年期国债".encode(), b"missing factor"), AS_OF
            )
