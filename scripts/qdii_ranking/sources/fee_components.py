"""Primary holding cost: management + custody + sales-service fees from the fund archive page."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from ..config import DOCUMENT_WORKERS, FUND_ARCHIVE_URL
from ..errors import DataError

COMPONENT_LABELS = (
    ("management_pct", "管理费率"),
    ("custody_pct", "托管费率"),
    ("sales_service_pct", "销售服务费率"),
)
FEE_SUM_TITLE = "天天基金基金概况：管理费率 + 托管费率 + 销售服务费率"
FEE_SUM_STATUS = "fee_sum"
_VALUE_RE = r"([0-9]+(?:\.[0-9]+)?\s*%|-{2,}|—+)"


def unavailable_fee_sum(code: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "annualized_pct": None,
        "components": None,
        "measurement_date": None,
        "source_title": None,
        "source_published_date": None,
        "source_url": None,
    }


def parse_fee_components(page: str, code: str, as_of: date) -> dict[str, Any]:
    """Sum the three annual fees; ``---`` means zero, a missing row fails closed."""
    components: dict[str, float] = {}
    for field, label in COMPONENT_LABELS:
        match = re.search(rf"<th>\s*{label}\s*</th>\s*<td>\s*{_VALUE_RE}", page)
        if not match:
            raise DataError(f"Fund archive page for {code} has no {label}")
        raw = match.group(1)
        components[field] = float(raw.rstrip("% ").strip()) if "%" in raw else 0.0
        if not 0 <= components[field] <= 100:
            raise DataError(f"Fund archive {label} for {code} is outside its valid range")
    total = round(sum(components.values()), 2)
    return {
        "status": FEE_SUM_STATUS,
        "annualized_pct": total,
        "components": components,
        "measurement_date": None,
        "source_title": FEE_SUM_TITLE,
        "source_published_date": as_of.isoformat(),
        "source_url": FUND_ARCHIVE_URL.format(code=code),
    }


def fetch_fee_sums(client: Any, codes: list[str], as_of: date) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Fetch each code once in this run; failures become ``unavailable`` with a warning."""
    def one(code: str) -> tuple[str, dict[str, Any], str | None]:
        try:
            page = client.get_text(FUND_ARCHIVE_URL.format(code=code), referer="https://fundf10.eastmoney.com/")
            return code, parse_fee_components(page, code, as_of), None
        except (DataError, OSError, ValueError) as exc:
            return code, unavailable_fee_sum(code), f"持有费率告警 {code}：基金概况页三项费率无法读取：{exc}"

    unique = sorted(set(codes))
    with ThreadPoolExecutor(max_workers=max(1, min(DOCUMENT_WORKERS, len(unique) or 1))) as pool:
        results = list(pool.map(one, unique))
    return {code: cost for code, cost, _ in results}, [w for _, _, w in results if w]


def apply_fee_sums(payload: dict[str, Any], client: Any, as_of: date) -> list[str]:
    """Move summary fees to ``summary_holding_cost`` and set the three-fee sum as primary."""
    sections = [
        payload.get("records") or [],
        (payload.get("global_supplement") or {}).get("records") or [],
        (payload.get("nasdaq100_otc") or {}).get("records") or [],
        (payload.get("exchange_premium") or {}).get("records") or [],
    ]
    records = [record for section in sections for record in section]
    costs, warnings = fetch_fee_sums(client, [record["code"] for record in records], as_of)
    for record in records:
        record["summary_holding_cost"] = record.get("holding_cost")
        record["holding_cost"] = dict(costs[record["code"]])
    return warnings
