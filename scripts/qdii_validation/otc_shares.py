"""Validate OTC-only share evidence and its public representations."""

import html
import re
from datetime import date
from typing import Any

from qdii_ranking.config import ANNOUNCEMENT_PDF_URL, EXCLUDED_FUND_TYPES
from qdii_ranking.sources.candidates import is_otc_share
from qdii_ranking.sources.otc_shares import (
    EVIDENCE_FIELDS, EVIDENCE_LABEL, needs_primary_share_verification, normalized_share_name,
)

from .common import ValidationError, require


def validate_share_evidence(record: dict[str, Any], run_date: date) -> None:
    code = record["code"]
    require(record["fund_type"] not in EXCLUDED_FUND_TYPES, f"{code} has an excluded fund type")
    evidence = record.get("share_class_evidence")
    if evidence is None:
        require(is_otc_share(record), f"{code} requires verified share_class_evidence")
        return
    require(needs_primary_share_verification(record), f"{code} is outside supplemental share scope")
    require(isinstance(evidence, dict), f"{code} has invalid share_class_evidence")
    require(
        all(isinstance(evidence.get(field), str) and evidence[field] for field in EVIDENCE_FIELDS),
        f"{code} has incomplete share_class_evidence",
    )
    for field, expected in {
        "method": "product_summary", "currency": "CNY", "share_class": "unlabeled_primary",
        "operation_mode": "普通开放式", "code": code, "name": record["name"],
    }.items():
        require(evidence[field] == expected, f"{code} share evidence {field} differs")
    require(
        normalized_share_name(evidence["summary_short_name"]) == normalized_share_name(record["name"]),
        f"{code} share evidence summary identity differs",
    )
    try:
        published = date.fromisoformat(evidence["published_date"])
    except ValueError as exc:
        raise ValidationError(f"{code} share evidence publication date is invalid") from exc
    require(published.isoformat() == evidence["published_date"], f"{code} share evidence date must use YYYY-MM-DD")
    require(published <= run_date, f"{code} share evidence uses a future announcement")
    require(re.fullmatch(r"AN\d+", evidence["announcement_id"]), f"{code} share evidence announcement ID is invalid")
    require(
        evidence["source_url"] == ANNOUNCEMENT_PDF_URL.format(announcement_id=evidence["announcement_id"]),
        f"{code} share evidence source does not match announcement ID",
    )


def validate_share_evidence_csv(row: dict[str, str], record: dict[str, Any]) -> None:
    evidence = record.get("share_class_evidence") or {}
    for field in EVIDENCE_FIELDS:
        require(
            row.get(f"share_evidence_{field}") == str(evidence.get(field) or ""),
            f"CSV share evidence {field} differs for {record['code']}",
        )


def validate_share_evidence_markdown(document: str, records: list[dict[str, Any]]) -> None:
    for record in records:
        if evidence := record.get("share_class_evidence"):
            expected = (
                f"{record['code']}：{EVIDENCE_LABEL}；"
                f"[产品概要 {evidence['published_date']}]({evidence['source_url']})"
            )
            require(expected in document, f"Markdown share evidence differs for {record['code']}")


def validate_share_evidence_html(document: str, record: dict[str, Any]) -> None:
    if evidence := record.get("share_class_evidence"):
        block = re.search(
            rf'<details class="nasdaq-item" data-code="{record["code"]}">(.*?)</details>',
            document, re.S,
        )
        require(block is not None, f"HTML share evidence record missing for {record['code']}")
        require(
            all(html.escape(value, quote=True) in block.group(1) for value in (
                EVIDENCE_LABEL, evidence["source_url"], evidence["published_date"],
            )),
            f"HTML share evidence differs for {record['code']}",
        )
