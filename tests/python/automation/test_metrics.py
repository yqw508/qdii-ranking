import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import send_qdii_email as mailer
import report_update_metrics as metrics_reporter
import update_qdii_ranking as ranking
import validate_qdii_ranking as validator

from tests.python.support.automation import (
    RUN_DATE,
    make_exchange_premium,
    make_global_record,
    make_payload,
    make_record,
    write_artifacts,
)

class PerformanceReportingTests(unittest.TestCase):
    def test_report_quantifies_latency_requests_and_pdf_work(self):
        baseline = {
            "summary": {
                "refresh_seconds": {"median": 600},
                "end_to_end_seconds": {"median": 640},
                "announcement_index_calls_minimum": 102,
                "pdf_text_extractions": 301,
            },
            "targets": {
                "refresh_median_seconds_max": 240,
                "end_to_end_median_seconds_max": 300,
                "announcement_index_reduction_pct_min": 66,
            },
        }
        current = {
            "refresh_seconds": 180,
            "http": {"announcement_index": {"calls": 34}},
            "cache": {"announcement_pdfs": {"text_extractions": 0}},
        }
        report = metrics_reporter.build_report(baseline, current, 240)
        self.assertIn("70.0%", report)
        self.assertIn("66.7%", report)
        self.assertIn("100.0%", report)
        self.assertEqual(4, report.count("PASS"))

    def test_workflow_runs_daily_at_0707_and_has_cross_version_cache_restore(self):
        workflow = (
            Path(__file__).resolve().parents[3]
            / ".github"
            / "workflows"
            / "update-ranking.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('cron: "7 23 * * *"', workflow)
        self.assertIn("qdii-ranking-${{ runner.os }}-", workflow)
        self.assertIn("Report performance comparison", workflow)
        self.assertIn("Refresh index valuation", workflow)
        self.assertIn("scripts/validate_index_valuation.py", workflow)
        self.assertIn("node --test tests/js/test_valuation_page.mjs", workflow)
        self.assertIn("public/valuation/index.html", workflow)
        self.assertIn("references/index-valuation-catalog.json", workflow)
        self.assertNotIn("references/index-valuation-anchors.json", workflow)
