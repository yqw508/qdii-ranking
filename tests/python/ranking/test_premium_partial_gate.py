import copy
import json
import unittest
from pathlib import Path

from scripts.validate_premium_service import validate_payload


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tmp/diag40252543707/ranking-diagnostics-40252543707-1/qdii-ranking"


class PremiumPartialGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = json.loads((FIXTURE / "premium-snapshot.json").read_text(encoding="utf-8"))
        cls.payload = json.loads((FIXTURE / "premium-service-check.json").read_text(encoding="utf-8"))

    def replay_payload(self):
        payload = copy.deepcopy(self.payload)
        records = {record["code"]: record for record in payload["records"]}
        # The archived run has representative ETF evidence in the validated cache.
        records["160125"]["status"] = "cached_stale"
        records["160140"]["status"] = "cached_stale"
        records["513500"]["status"] = "fresh"
        records["159941"]["status"] = "fresh"
        payload["fresh_count"] = 34
        payload["status"] = "partial"
        payload["stale_count"] = 87
        payload["catalog_count"] = 122
        payload["unavailable_count"] = 1
        payload["unaccounted_count"] = 0
        payload["publication_decision"] = "allow_with_stale"
        return payload

    def test_partial_refresh_full_coverage_is_publishable(self):
        result = validate_payload(self.replay_payload(), self.seed, require_live=True)
        self.assertEqual("allow_with_stale", result["publication_decision"])
        self.assertEqual(34, result["fresh_count"])
        self.assertEqual(87, result["stale_count"])
        self.assertEqual(1, len(result["unavailable_products"]))

    def test_archived_run_has_explainable_full_coverage(self):
        result = validate_payload(self.payload, self.seed, require_live=False)
        self.assertEqual(34, result["fresh_count"])
        self.assertEqual(87, result["stale_count"])
        self.assertEqual(1, len(result["unavailable_products"]))

    def test_expired_stale_record_is_blocked(self):
        payload = self.replay_payload()
        next(record for record in payload["records"] if record["status"] == "cached_stale")["quoteDate"] = "2026-10-18"
        with self.assertRaisesRegex(AssertionError, "7-day hard stale"):
            validate_payload(payload, self.seed, require_live=False)

    def test_no_fresh_evidence_is_blocked(self):
        payload = self.replay_payload()
        for record in payload["records"]:
            if record["status"] == "fresh":
                record["status"] = "cached_stale"
        payload["fresh_count"] = 0
        payload["stale_count"] = 121
        payload["status"] = "cached_stale"
        with self.assertRaisesRegex(AssertionError, "No validated live upstream quote"):
            validate_payload(payload, self.seed, require_live=True)


if __name__ == "__main__":
    unittest.main()
