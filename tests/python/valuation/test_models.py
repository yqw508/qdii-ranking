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

class ModelAndRefreshTests(unittest.TestCase):
    def test_cold_start_runs_eight_sources_concurrently(self):
        client = FakeClient(delay=0.04)
        payload, _caches, _manifest, metrics = run_build(client=client)
        logical_sources = {
            valuation.NDXTMC_SOURCE_ID if item[0].startswith("ndxtmc-") else item[0]
            for item in client.calls
        }
        self.assertEqual(set(valuation.all_source_ids()), logical_sources)
        self.assertEqual(8, len(payload["sources"]))
        self.assertGreaterEqual(client.max_active, 7)
        self.assertEqual("cold", payload["cache"]["startup"])
        self.assertEqual("full", payload["cache"]["refresh_mode"])
        self.assertLess(metrics["request_wall_seconds"], 0.30)

    def test_builds_all_assets_and_reproduces_each_anchor(self):
        payload, _caches, _manifest, _metrics = run_build()
        self.assertEqual("fresh", payload["status"])
        self.assertEqual(list(valuation.EXPECTED_ASSET_IDS), [item["id"] for item in payload["assets"]])
        proxies = [item for item in payload["assets"] if item["source_mode"] == "proxy"]
        self.assertEqual(4, len(proxies))
        for asset in proxies:
            self.assertEqual(120, len(asset["history"]))
            if asset["id"] == valuation.RELATIVE_PROXY_ASSET_ID:
                values = [item["relative_score"] for item in asset["history"]]
                self.assertAlmostEqual(
                    valuation.percentile_midrank(values, values[-1]),
                    asset["current"]["relative_percentile_10y"],
                    places=2,
                )
                self.assertNotIn("proxy_pe_ttm", asset["current"])
                self.assertNotIn("anchor", asset["method"])
                continue
            anchor = asset["method"]["anchor"]
            self.assertAlmostEqual(anchor["pe_ttm"], anchor["reproduced_pe_ttm"], places=5)
            values = [item["proxy_pe_ttm"] for item in asset["history"]]
            self.assertAlmostEqual(
                valuation.percentile_midrank(values, values[-1]),
                asset["current"]["proxy_percentile_10y"],
                places=2,
            )
            self.assertNotIn("source_rating", asset["current"])
        self.assertTrue(next(item for item in proxies if item["id"] == "ftse-100-proxy")["method"]["experimental"])

    def test_relative_proxy_percentile_is_scale_invariant(self):
        payload, _caches, _manifest, _metrics = run_build()
        asset = next(item for item in payload["assets"] if item["id"] == valuation.RELATIVE_PROXY_ASSET_ID)
        values = [item["relative_score"] for item in asset["history"]]
        scaled = [value * 17.25 for value in values]
        self.assertEqual(
            valuation.percentile_midrank(values, values[-1]),
            valuation.percentile_midrank(scaled, scaled[-1]),
        )

    def test_hot_start_uses_five_price_tails_and_three_conditional_requests(self):
        caches = make_cache_bundle()
        client = FakeClient(tail_prices=True, conditional_304=True)
        payload, _new, _manifest, metrics = run_build(
            caches=caches, manifest=make_manifest(), client=client
        )
        self.assertEqual("hot", payload["cache"]["startup"])
        self.assertEqual("tail", payload["cache"]["refresh_mode"])
        self.assertEqual(304, metrics["sources"]["snowball"]["http_status"])
        self.assertEqual(304, metrics["sources"]["gold"]["http_status"])
        self.assertEqual("tail", metrics["sources"][valuation.NDXTMC_SOURCE_ID]["request_mode"])
        self.assertEqual("full", metrics["sources"]["dqydj"]["request_mode"])
        for source, url, headers in client.calls:
            if source.startswith("nasdaq-") and source != valuation.NDXTMC_SOURCE_ID:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                self.assertGreaterEqual(query["fromdate"][0], "2026-05-01")
                self.assertEqual("tail", metrics["sources"][source]["request_mode"])
            if source in {"snowball", "gold", "ndxtmc-workbook"}:
                self.assertIn("If-None-Match", headers)

    def test_unchanged_ndxtmc_workbook_200_uses_only_online_tail(self):
        caches = make_cache_bundle()
        client = FakeClient(tail_prices=True)
        _payload, new_caches, _manifest, _metrics = run_build(
            caches=caches, manifest=make_manifest(), client=client
        )
        live_calls = [call for call in client.calls if call[0] == "ndxtmc-history"]
        self.assertEqual(1, len(live_calls))
        self.assertEqual(
            caches[valuation.NDXTMC_SOURCE_ID]["data"][0],
            new_caches[valuation.NDXTMC_SOURCE_ID]["data"][0],
        )

    def test_new_month_runs_all_five_full_price_scans(self):
        caches = make_cache_bundle()
        payload, _new, manifest, _metrics = run_build(
            caches=caches, manifest=make_manifest("2026-07"), client=FakeClient()
        )
        self.assertEqual("full", payload["cache"]["refresh_mode"])
        self.assertEqual("2026-08", manifest["last_full_refresh_month"])

    def test_ndxtmc_full_refresh_chunks_and_merges_live_history(self):
        client = FakeClient()
        payload, _new, _manifest, _metrics = run_build(client=client)
        chunks = [call for call in client.calls if call[0] == "ndxtmc-history"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("fresh", next(
            item for item in payload["assets"] if item["id"] == valuation.RELATIVE_PROXY_ASSET_ID
        )["status"])
        self.assertTrue(all("startDate" in call[2] and "endDate" in call[2] for call in chunks))

    def test_ndxtmc_failure_without_cache_only_disables_relative_proxy(self):
        payload, _new, _manifest, _metrics = run_build(
            client=FakeClient(fail={valuation.NDXTMC_SOURCE_ID})
        )
        status = {item["id"]: item["status"] for item in payload["assets"]}
        self.assertEqual("unavailable", status[valuation.RELATIVE_PROXY_ASSET_ID])
        self.assertTrue(all(
            status[asset_id] == "fresh"
            for asset_id in valuation.EXPECTED_ASSET_IDS
            if asset_id != valuation.RELATIVE_PROXY_ASSET_ID
        ))

    def test_ndxtmc_failure_with_cache_marks_relative_proxy_stale(self):
        caches = make_cache_bundle()
        payload, _new, _manifest, _metrics = run_build(
            caches=caches,
            manifest=make_manifest(),
            client=FakeClient(fail={valuation.NDXTMC_SOURCE_ID}),
        )
        asset = next(
            item for item in payload["assets"] if item["id"] == valuation.RELATIVE_PROXY_ASSET_ID
        )
        self.assertEqual("cached_stale", asset["status"])

    def test_full_scan_keeps_cached_boundary_for_lagging_pe_source(self):
        """A one-month DQYDJ lag must not discard the prior 120-month price window."""
        caches = make_cache_bundle()
        catalog, catalog_hash = load_catalog()
        payload, _new, _manifest, _metrics = valuation.build_payload(
            as_of=date(2026, 9, 3),
            now=datetime(2026, 9, 3, 7, 7, tzinfo=valuation.SHANGHAI_TZ),
            catalog=catalog,
            catalog_hash=catalog_hash,
            caches=caches,
            cache_states={source_id: "valid" for source_id in valuation.all_source_ids()},
            manifest=make_manifest("2026-08"),
            manifest_state="valid",
            client=FakeClient(),
        )
        self.assertEqual("full", payload["cache"]["refresh_mode"])
        self.assertTrue(
            all(
                asset["status"] == "fresh"
                for asset in payload["assets"]
                if asset["source_mode"] == "proxy"
            )
        )
        self.assertEqual("2016-08", next(
            asset for asset in payload["assets"] if asset["id"] == "sp-500-equal-weight"
        )["history"][0]["month"])

    def test_shared_spy_failure_without_cache_only_disables_proxies(self):
        payload, _new, _manifest, _metrics = run_build(
            client=FakeClient(fail={"nasdaq-spy"})
        )
        self.assertEqual("partial", payload["status"])
        status = {item["id"]: item["status"] for item in payload["assets"]}
        self.assertTrue(all(status[item] == "unavailable" for item in valuation.PROXY_ASSET_IDS))
        self.assertTrue(all(status[item] == "fresh" for item in valuation.DIRECT_ASSET_IDS))
        self.assertEqual("fresh", status[valuation.GOLD_ASSET_ID])

    def test_source_failure_with_cache_marks_only_dependents_stale(self):
        caches = make_cache_bundle()
        payload, new_caches, _manifest, metrics = run_build(
            caches=caches,
            manifest=make_manifest(),
            client=FakeClient(fail={"dqydj"}, tail_prices=True),
        )
        self.assertEqual("partial", payload["status"])
        proxies = [item for item in payload["assets"] if item["source_mode"] == "proxy"]
        self.assertTrue(all(item["status"] == "cached_stale" for item in proxies))
        self.assertTrue(all(item["status"] == "fresh" for item in payload["assets"] if item["source_mode"] != "proxy"))
        self.assertTrue(metrics["sources"]["dqydj"]["cache_fallback"])
        self.assertEqual(caches["dqydj"]["last_success_at"], new_caches["dqydj"]["last_success_at"])

    def test_failed_monthly_full_scan_does_not_advance_marker(self):
        caches = make_cache_bundle()
        _payload, _new, manifest, _metrics = run_build(
            caches=caches,
            manifest=make_manifest("2026-07"),
            client=FakeClient(fail={"nasdaq-rsp"}),
        )
        self.assertEqual("2026-07", manifest["last_full_refresh_month"])
