import csv
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from qdii_validation.common import ValidationError
from qdii_validation.otc_shares import (
    validate_share_evidence, validate_share_evidence_csv,
    validate_share_evidence_html, validate_share_evidence_markdown,
)
from qdii_validation.schema import validate_nasdaq100_otc_section
from tests.python.automation.test_validator import make_otc_payload
from tests.python.ranking.test_otc_shares import AS_OF, META, evidence
from tests.python.support.automation import write_artifacts


def verified_record():
    record = make_otc_payload()["nasdaq100_otc"]["records"][0]
    return {**record, **META, "share_class_evidence": evidence()}


class OTCShareEvidenceValidationTests(unittest.TestCase):
    def test_valid_evidence_and_original_name_are_accepted(self):
        validate_share_evidence(verified_record(), AS_OF)
        record = make_otc_payload()["nasdaq100_otc"]["records"][0]
        validate_share_evidence(record, AS_OF)

    def test_incomplete_conflicting_and_future_evidence_is_rejected(self):
        record = verified_record()
        for field in record["share_class_evidence"]:
            modified = deepcopy(record)
            del modified["share_class_evidence"][field]
            with self.subTest(missing=field), self.assertRaises(ValidationError):
                validate_share_evidence(modified, AS_OF)
        for field, value in (
            ("currency", "USD"), ("share_class", "C"), ("operation_mode", "交易型开放式"),
            ("code", "160214"), ("name", "其他基金"), ("summary_short_name", "国泰纳斯达克100指数C"),
            ("published_date", "2026-09-23"), ("announcement_id", "AN000000"),
            ("published_date", "2026-09-21extra"), ("published_date", "2026-02-31"),
            ("source_url", "https://example.test/other.pdf"), ("method", "name_guess"),
        ):
            modified = deepcopy(record)
            modified["share_class_evidence"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_share_evidence(modified, AS_OF)
        del record["share_class_evidence"]
        with self.assertRaisesRegex(ValidationError, "requires verified"):
            validate_share_evidence(record, AS_OF)

    def test_forged_evidence_cannot_override_share_scope(self):
        for suffix in ("C", "D", "E", "F", "I", "后端", "美元", "ETF"):
            record = verified_record()
            record["name"] += suffix
            record["share_class_evidence"]["name"] = record["name"]
            record["share_class_evidence"]["summary_short_name"] = record["name"]
            with self.subTest(suffix=suffix), self.assertRaises(ValidationError):
                validate_share_evidence(record, AS_OF)

    def test_evidence_is_preserved_in_all_formats_and_tampering_is_detected(self):
        record = verified_record()
        payload = make_otc_payload()
        payload["nasdaq100_otc"]["records"] = [record]
        with TemporaryDirectory() as directory:
            output, _ = write_artifacts(Path(directory), payload)
            markdown = (output / "latest.md").read_text(encoding="utf-8")
            document = (output / "latest.html").read_text(encoding="utf-8")
            with (output / "latest.csv").open(encoding="utf-8-sig", newline="") as stream:
                row = next(r for r in csv.DictReader(stream) if r["ranking_list"] == "nasdaq100_otc")
        self.assertEqual(META["name"], row["name"])
        validate_share_evidence_csv(row, record)
        validate_share_evidence_html(document, record)
        validate_share_evidence_markdown(markdown, [record])
        row["share_evidence_currency"] = "USD"
        with self.assertRaises(ValidationError):
            validate_share_evidence_csv(row, record)
        with self.assertRaises(ValidationError):
            validate_share_evidence_html(document.replace("人民币主份额，经产品概要确认", ""), record)
        with self.assertRaises(ValidationError):
            validate_share_evidence_markdown(markdown.replace(record["share_class_evidence"]["source_url"], ""), [record])

    def test_duplicate_otc_records_are_rejected(self):
        section = make_otc_payload()["nasdaq100_otc"]
        section["records"].append(deepcopy(section["records"][0]))
        section["candidate_count"] = 2
        with self.assertRaisesRegex(ValidationError, "duplicated"):
            validate_nasdaq100_otc_section(section, AS_OF)
