"""Listed-QDII discovery and NAV source adapter."""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any

from ..cache.base import parse_cache_date as parse_source_date
from ..config import (
    ETF_LOF_NAV_API_URL,
    ETF_MARKET_LIST_API_URL,
    ETF_MARKET_LIST_FS,
    ETF_MARKET_LIST_PAGE_SIZE,
    ETF_QUOTE_PAGE_URL,
    PERFORMANCE_WORKERS,
)
from ..errors import DataError
from .fund import is_qdii_fund_metadata


def exchange_premium_market_url(
    page: int = 1, page_size: int = ETF_MARKET_LIST_PAGE_SIZE
) -> str:
    params = {
        "pn": str(page),
        "pz": str(page_size),
        "po": "0",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": ETF_MARKET_LIST_FS,
        "fields": "f2,f3,f6,f12,f13,f14,f18,f124,f297,f402,f441",
    }
    return f"{ETF_MARKET_LIST_API_URL}?{urllib.parse.urlencode(params)}"



def _parse_exchange_premium_market_page(
    payload: Any, page: int
) -> tuple[int, list[dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise DataError(f"ETF market list page {page} has no data object")
    data = payload["data"]
    try:
        total = int(data.get("total"))
    except (TypeError, ValueError) as exc:
        raise DataError(f"ETF market list page {page} has no valid total") from exc
    rows = data.get("diff")
    if not isinstance(rows, list):
        raise DataError(f"ETF market list page {page} has no record list")
    return total, [row for row in rows if isinstance(row, dict)]



def exchange_premium_lof_nav_url(code: str) -> str:
    params = {
        "fundCode": code,
        "pageIndex": "1",
        "pageSize": "1",
        "startDate": "",
        "endDate": "",
    }
    return f"{ETF_LOF_NAV_API_URL}?{urllib.parse.urlencode(params)}"



def parse_exchange_premium_lof_nav(
    payload: Any, code: str, as_of: date
) -> dict[str, Any]:
    rows = (
        ((payload.get("Data") or {}).get("LSJZList"))
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise DataError(f"LOF NAV response contains no record for {code}")
    try:
        observed = parse_source_date(str(rows[0]["FSRQ"]))
        nav = float(rows[0]["DWJZ"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"LOF NAV response is invalid for {code}") from exc
    if observed > as_of or not math.isfinite(nav) or nav <= 0:
        raise DataError(f"LOF NAV is outside its valid range for {code}")
    return {
        "reference_value_type": "nav",
        "reference_value_cny": round(nav, 4),
        "reference_value_date": observed.isoformat(),
        "reference_value_source_url": exchange_premium_lof_nav_url(code),
    }



def fetch_exchange_premium_lof_navs(
    client: HttpClient,
    entries: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    as_of: date,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    entry_by_code = {entry["code"]: entry for entry in entries}
    candidates: dict[str, dict[str, Any]] = {}
    for raw in rows:
        code = str(raw.get("f12", "")) if isinstance(raw, dict) else ""
        entry = entry_by_code.get(code)
        if (
            entry
            and raw.get("f441") in {None, "", "-"}
            and all(raw.get(field) not in {None, "", "-"} for field in ("f2", "f402", "f3", "f6"))
        ):
            candidates[code] = entry

    references: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    def fetch(code: str) -> tuple[str, dict[str, Any]]:
        url = exchange_premium_lof_nav_url(code)
        payload = client.get_json(
            url, referer=f"https://fundf10.eastmoney.com/jjjz_{code}.html"
        )
        return code, parse_exchange_premium_lof_nav(payload, code, as_of)

    with ThreadPoolExecutor(max_workers=min(PERFORMANCE_WORKERS, len(candidates) or 1)) as executor:
        futures = {executor.submit(fetch, code): code for code in candidates}
        for future in as_completed(futures):
            code = futures[future]
            try:
                resolved_code, reference = future.result()
                references[resolved_code] = reference
            except (DataError, OSError, ValueError) as exc:
                warnings.append(f"场内溢价告警 {code}：LOF 最新单位净值无法读取：{exc}")
    return references, warnings



def load_qdii_exchange_premium_catalog(
    client: HttpClient,
    metadata: dict[str, dict[str, str]],
    page_size: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    """Load all listed funds, then retain the repository's QDII fund scope."""
    page_size = page_size or ETF_MARKET_LIST_PAGE_SIZE
    first_page = 1
    total, first_rows = _parse_exchange_premium_market_page(
        client.get_json(
            exchange_premium_market_url(first_page, page_size),
            referer=ETF_QUOTE_PAGE_URL,
        ),
        first_page,
    )
    if total <= 0:
        raise DataError("ETF market list returned no records")
    page_count = math.ceil(total / page_size)
    all_rows = list(first_rows)
    for page in range(2, page_count + 1):
        page_total, rows = _parse_exchange_premium_market_page(
            client.get_json(
                exchange_premium_market_url(page, page_size),
                referer=ETF_QUOTE_PAGE_URL,
            ),
            page,
        )
        if page_total != total:
            raise DataError("ETF market list total changed during pagination")
        all_rows.extend(rows)
    if len(all_rows) != total:
        raise DataError(
            f"ETF market list is incomplete: expected {total}, got {len(all_rows)}"
        )

    entries: list[dict[str, Any]] = []
    quote_rows: dict[str, dict[str, Any]] = {}
    seen_codes: set[str] = set()
    for raw in all_rows:
        code = str(raw.get("f12", ""))
        market_id = raw.get("f13")
        fund = metadata.get(code)
        if (
            not re.fullmatch(r"\d{6}", code)
            or market_id not in {0, 1}
            or code in seen_codes
            or not is_qdii_fund_metadata(fund)
        ):
            continue
        seen_codes.add(code)
        fund_type = str(fund.get("fund_type", "")).strip()
        entries.append(
            {
                "code": code,
                "name": str(raw.get("f14") or fund.get("name") or code).strip(),
                "exchange": "SSE" if int(market_id) == 1 else "SZSE",
                "market_id": int(market_id),
                "category": "qdii",
                "benchmark_group": fund_type or "QDII",
                "fund_type": fund_type,
                "source_url": ETF_QUOTE_PAGE_URL,
            }
        )
        quote_rows[code] = raw
    if not entries:
        raise DataError("No listed QDII funds matched the fund metadata")
    entries.sort(key=lambda item: item["code"])
    fingerprint = hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return entries, quote_rows, fingerprint
