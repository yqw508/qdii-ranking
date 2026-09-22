"""Candidate discovery rules shared by ranking lists."""

from __future__ import annotations

import re
from typing import Any

from ..config import EXCLUDED_FUND_TYPES, NASDAQ100_OTC_NAME_RE


def is_rmb_a_share(meta: dict[str, str]) -> bool:
    name = meta["name"]
    if not (
        meta["fund_type"].startswith("QDII")
        or meta["fund_type"] == "指数型-海外股票"
    ):
        return False
    if re.search(
        r"美元|港币|后端|人民币[CD]|[CD](?:类)?(?:份额)?人民币|"
        r"(?:\(|（|/|\s)[CD](?:类|份额|\)|）|$)|[CD](?:类|份额|\)|）|$)",
        name,
    ):
        return False
    if re.search(
        r"(?:人民币|RMB)\s*[CDEFI](?:类|份额)?(?:$|[)）])"
        r"|\)[\s]*[CDEFI](?:类|份额)?\s*\(?(?:人民币|RMB)?"
        r"|[（(][CDEFI](?:类|份额)?[)）]",
        name,
        re.IGNORECASE,
    ):
        return False
    if re.search(r"人民币A|A(?:类|份额)?人民币|A类|A1(?:\(|$)|A(?:\(|$)", name):
        return True
    return "人民币" in name


def is_otc_share(meta: dict[str, str]) -> bool:
    name = meta["name"]
    if not is_rmb_a_share(meta):
        return False
    return not ("ETF" in name.upper() and "联接" not in name and "LOF" not in name.upper())


def is_nasdaq100_otc_name(name: str) -> bool:
    return bool(NASDAQ100_OTC_NAME_RE.search(str(name or "")))


def nasdaq100_holder_details(
    metadata: dict[str, dict[str, str]],
    holder_rows: list[list[str]],
) -> dict[str, dict[str, Any]]:
    holder_by_code: dict[str, dict[str, Any]] = {}
    for row in holder_rows:
        if len(row) < 6 or row[0] not in metadata:
            continue
        try:
            institution = float(row[2]) if row[2] else None
            personal = float(row[3]) if row[3] else None
            total = float(row[5].replace(",", "")) if row[5] else None
        except (TypeError, ValueError):
            continue
        holder_by_code[row[0]] = {
            "institution_holding_ratio_pct": institution,
            "personal_holding_ratio_pct": personal,
            "holder_total_shares_100m": total,
        }
    return holder_by_code


def build_nasdaq100_otc_candidates(
    metadata: dict[str, dict[str, str]],
    holder_rows: list[list[str]],
) -> list[dict[str, Any]]:
    holder_by_code = nasdaq100_holder_details(metadata, holder_rows)
    candidates: list[dict[str, Any]] = []
    for meta in metadata.values():
        if (
            not is_otc_share(meta)
            or meta["fund_type"] in EXCLUDED_FUND_TYPES
            or not is_nasdaq100_otc_name(meta["name"])
        ):
            continue
        candidates.append(
            {
                **meta,
                **holder_by_code.get(
                    meta["code"],
                    {
                        "institution_holding_ratio_pct": None,
                        "personal_holding_ratio_pct": None,
                        "holder_total_shares_100m": None,
                    },
                ),
            }
        )
    return sorted(candidates, key=lambda item: item["code"])
