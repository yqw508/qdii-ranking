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

class CacheAndArtifactTests(unittest.TestCase):
    def test_source_cache_fingerprint_change_is_a_miss(self):
        catalog, catalog_hash = load_catalog()
        cache = make_cache_bundle()["snowball"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            valuation.atomic_write_json(root / "snowball.json", cache)
            loaded, states = valuation.load_source_caches(
                root, catalog, "0" * 64, AS_OF
            )
        self.assertNotIn("snowball", loaded)
        self.assertEqual("fingerprint_mismatch", states["snowball"])
        self.assertNotEqual(valuation.source_fingerprint(catalog_hash, "snowball"), "0" * 64)

    def test_corrupt_source_cache_is_a_miss(self):
        catalog, catalog_hash = load_catalog()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gold.json").write_text("{broken", encoding="utf-8")
            loaded, states = valuation.load_source_caches(root, catalog, catalog_hash, AS_OF)
        self.assertNotIn("gold", loaded)
        self.assertEqual("corrupt", states["gold"])

    def test_validator_accepts_byte_identical_multi_asset_artifacts(self):
        payload, _cache, _manifest, _metrics = run_build()
        document = valuation.render_html(
            payload, valuation.DEFAULT_PAGE_SCRIPT.read_text(encoding="utf-8")
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            publish = root / "public"
            valuation.atomic_write_json(output / "latest.json", payload)
            valuation.atomic_write_text(output / "latest.html", document)
            valuation.atomic_write_text(publish / "valuation" / "index.html", document)
            validated = validator.validate_local_artifacts(output, publish, "2026-08-25")
        self.assertEqual(8, len(validated["assets"]))

    def test_validator_rejects_inconsistent_proxy_percentile(self):
        payload, _cache, _manifest, _metrics = run_build()
        proxy = next(item for item in payload["assets"] if item["source_mode"] == "proxy")
        proxy["current"]["proxy_percentile_10y"] += 1
        with self.assertRaisesRegex(validator.ValidationError, "midrank"):
            validator.validate_payload(payload, "2026-08-25")

    def test_validator_rejects_source_rating_on_proxy(self):
        payload, _cache, _manifest, _metrics = run_build()
        proxy = next(item for item in payload["assets"] if item["source_mode"] == "proxy")
        proxy["current"]["source_rating"] = {"label": "高估"}
        with self.assertRaisesRegex(validator.ValidationError, "must not expose"):
            validator.validate_payload(payload, "2026-08-25")

    def test_validator_rejects_all_assets_unavailable(self):
        payload, _cache, _manifest, _metrics = run_build(
            client=FakeClient(fail=set(valuation.all_source_ids()))
        )
        self.assertEqual("unavailable", payload["status"])
        with self.assertRaisesRegex(validator.ValidationError, "All valuation assets"):
            validator.validate_payload(payload, "2026-08-25")
