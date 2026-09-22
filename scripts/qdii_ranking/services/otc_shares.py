"""OTC-only supplemental discovery using the existing announcement/PDF caches."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..cache.announcements import AnnouncementIndexCache, PeriodicReportCache
from ..config import FUND_PAGE_URL
from ..errors import DataError
from ..models import FundAnnouncementSnapshot
from ..runtime import HttpClient
from ..sources.candidates import build_nasdaq100_otc_candidates, nasdaq100_holder_details
from ..sources.contracts import fetch_latest_legal_documents
from ..sources.otc_shares import needs_primary_share_verification, parse_primary_share_evidence


def discover_nasdaq100_otc_candidates(
    client: HttpClient, metadata: dict[str, dict[str, str]], holder_rows: list[list[str]],
    as_of: date, announcement_cache: AnnouncementIndexCache, document_cache: PeriodicReportCache,
) -> tuple[list[dict[str, Any]], dict[str, FundAnnouncementSnapshot], list[str]]:
    candidates = build_nasdaq100_otc_candidates(metadata, holder_rows)
    holders = nasdaq100_holder_details(metadata, holder_rows)
    snapshots: dict[str, FundAnnouncementSnapshot] = {}
    warnings: list[str] = []
    known = {item["code"] for item in candidates}
    for meta in metadata.values():
        code = meta["code"]
        if code in known or not needs_primary_share_verification(meta):
            continue
        try:
            snapshot = announcement_cache.get(client, code, as_of)
            snapshots[code] = snapshot
            _, summary = fetch_latest_legal_documents(client, code, as_of, snapshot)
            if summary is None:
                raise DataError("截至排名日缺少可用的产品概要")
            evidence = parse_primary_share_evidence(
                document_cache.get_text(client, summary, FUND_PAGE_URL.format(code=code)),
                meta, summary, as_of,
            )
        except (DataError, OSError, ValueError) as exc:
            warnings.append(f"场外纳指100告警 {code}：未标记主份额未纳入，{exc}")
            continue
        # Holder coverage, purchase status and quotas do not gate this display list.
        candidates.append({
            **meta, "share_class_evidence": evidence,
            "institution_holding_ratio_pct": None,
            "personal_holding_ratio_pct": None,
            "holder_total_shares_100m": None,
            **holders.get(code, {}),
        })
        known.add(code)
    return sorted(candidates, key=lambda item: item["code"]), snapshots, warnings
