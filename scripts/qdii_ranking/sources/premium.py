"""Listed-QDII discovery and NAV source adapter."""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..artifacts import write_json
from ..cache.announcements import AnnouncementIndexCache, PeriodicReportCache
from ..cache.premium import ExchangePremiumHoldingCostCache
from ..cache.base import parse_cache_date as parse_source_date
from ..config import (
    DOCUMENT_WORKERS,
    ETF_PREMIUM_CACHE_SCHEMA_VERSION,
    ETF_PREMIUM_CATALOG_SCHEMA_VERSION,
    ETF_PREMIUM_DELAY_MINUTES,
    ETF_PREMIUM_GROUP_ORDER,
    ETF_LOF_NAV_API_URL,
    ETF_MARKET_LIST_API_URL,
    ETF_MARKET_LIST_FS,
    ETF_MARKET_LIST_PAGE_SIZE,
    ETF_QUOTE_API_URL,
    ETF_QUOTE_PAGE_URL,
    FUND_PAGE_URL,
    PERFORMANCE_WORKERS,
)
from ..errors import DataError
from ..runtime import HttpClient, SHANGHAI_TZ, parse_date
from .contracts import (
    fetch_latest_legal_documents,
    parse_holding_cost,
    unavailable_holding_cost,
)
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


def load_exchange_premium_catalog(path: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataError(f"Could not read US-equity ETF catalog {path}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ETF_PREMIUM_CATALOG_SCHEMA_VERSION
        or not isinstance(payload.get("entries"), list)
    ):
        raise DataError("US-equity ETF catalog has an unsupported schema")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in payload["entries"]:
        if not isinstance(raw_entry, dict):
            raise DataError("US-equity ETF catalog entries must be objects")
        code = str(raw_entry.get("code", ""))
        name = str(raw_entry.get("name", "")).strip()
        exchange = str(raw_entry.get("exchange", ""))
        market_id = raw_entry.get("market_id")
        category = str(raw_entry.get("category", ""))
        benchmark_group = str(raw_entry.get("benchmark_group", ""))
        source_url = str(raw_entry.get("source_url", ""))
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            raise DataError(f"Invalid or duplicate ETF code in catalog: {code!r}")
        if not name:
            raise DataError(f"ETF catalog name is missing for {code}")
        if exchange not in {"SSE", "SZSE"} or market_id not in {0, 1}:
            raise DataError(f"ETF catalog exchange is invalid for {code}")
        if (exchange == "SSE") != (market_id == 1):
            raise DataError(f"ETF catalog market ID differs from exchange for {code}")
        if category not in {"broad_market", "sector_theme"}:
            raise DataError(f"ETF catalog category is invalid for {code}")
        if benchmark_group not in ETF_PREMIUM_GROUP_ORDER:
            raise DataError(f"ETF catalog benchmark group is invalid for {code}")
        if not source_url.startswith("https://"):
            raise DataError(f"ETF catalog source URL is invalid for {code}")
        seen.add(code)
        entries.append(
            {
                "code": code,
                "name": name,
                "exchange": exchange,
                "market_id": int(market_id),
                "category": category,
                "benchmark_group": benchmark_group,
                "source_url": source_url,
            }
        )
    if not entries:
        raise DataError("US-equity ETF catalog is empty")
    order = {group: index for index, group in enumerate(ETF_PREMIUM_GROUP_ORDER)}
    entries.sort(key=lambda item: (order[item["benchmark_group"]], item["code"]))
    return entries, hashlib.sha256(raw).hexdigest()


def resolve_exchange_premium_holding_cost(
    client: HttpClient,
    entry: dict[str, Any],
    as_of: date,
    announcement_cache: AnnouncementIndexCache,
    document_cache: PeriodicReportCache,
    result_cache: ExchangePremiumHoldingCostCache,
) -> tuple[dict[str, Any], list[str]]:
    code = entry["code"]
    cached = result_cache.load(code, as_of)
    try:
        snapshot = announcement_cache.get(client, code, as_of)
    except (DataError, OSError, ValueError) as exc:
        if cached is not None and cached[1]["status"] == "parsed":
            result_cache.mark_stale_fallback()
            return (
                {**cached[1], "status": "stale"},
                [f"场内费率告警 {code}：公告索引无法读取，使用上次费率：{exc}"],
            )
        return (
            unavailable_holding_cost(None),
            [f"场内费率告警 {code}：公告索引无法读取，且没有可用旧值：{exc}"],
        )

    try:
        _prospectus, summary = fetch_latest_legal_documents(
            client, code, as_of, snapshot=snapshot
        )
    except (DataError, OSError, ValueError) as exc:
        result_cache.mark_miss()
        return (
            unavailable_holding_cost(None),
            [f"场内费率告警 {code}：无法选择最新产品概要：{exc}"],
        )
    if summary is None:
        result_cache.mark_miss()
        cost = unavailable_holding_cost(None)
        warnings = [f"场内费率告警 {code}：截至 {as_of} 没有可用的产品概要。"]
        try:
            result_cache.save(code, None, cost)
        except OSError as exc:
            warnings.append(f"场内费率告警 {code}：无法保存费率缓存：{exc}")
        return cost, warnings

    if cached is not None and cached[0] == summary.announcement_id and cached[1]["status"] == "parsed":
        result_cache.mark_hit()
        return cached[1], []

    result_cache.mark_miss()
    try:
        summary_text = document_cache.get_text(
            client, summary, FUND_PAGE_URL.format(code=code)
        )
        cost = parse_holding_cost(summary_text, summary, as_of)
        warnings: list[str] = []
    except (DataError, OSError, ValueError) as exc:
        cost = unavailable_holding_cost(summary)
        warnings = [f"场内费率告警 {code}：最新产品概要无法解析：{exc}"]
    try:
        result_cache.save(code, summary.announcement_id, cost)
    except OSError as exc:
        warnings.append(f"场内费率告警 {code}：无法保存费率缓存：{exc}")
    return cost, warnings


def build_exchange_premium_holding_costs(
    client: HttpClient,
    entries: list[dict[str, Any]],
    as_of: date,
    announcement_cache: AnnouncementIndexCache,
    document_cache: PeriodicReportCache,
    result_cache: ExchangePremiumHoldingCostCache,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    costs: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=min(DOCUMENT_WORKERS, len(entries))) as executor:
        futures = {
            executor.submit(
                resolve_exchange_premium_holding_cost,
                client,
                entry,
                as_of,
                announcement_cache,
                document_cache,
                result_cache,
            ): entry["code"]
            for entry in entries
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                cost, item_warnings = future.result()
            except Exception as exc:  # pragma: no cover - defensive isolation for auxiliary data
                cost = unavailable_holding_cost(None)
                item_warnings = [f"场内费率告警 {code}：费率处理失败：{exc}"]
            costs[code] = cost
            warnings.extend(item_warnings)
    return costs, warnings


def attach_exchange_premium_holding_costs(
    section: dict[str, Any], costs: dict[str, dict[str, Any]]
) -> None:
    for record in section["records"]:
        record["holding_cost"] = costs.get(
            record["code"], unavailable_holding_cost(None)
        )
    section["schema_version"] = 2


def exchange_premium_quote_url(entries: list[dict[str, Any]]) -> str:
    if any(entry.get("category") == "qdii" for entry in entries):
        return exchange_premium_market_url()
    params = {
        "fltt": "2",
        "invt": "2",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "secids": ",".join(
            f"{entry['market_id']}.{entry['code']}" for entry in entries
        ),
        "fields": "f2,f3,f6,f12,f13,f14,f18,f124,f297,f402,f441",
    }
    return f"{ETF_QUOTE_API_URL}?{urllib.parse.urlencode(params)}"


def _finite_quote_number(value: Any, label: str, code: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{code} {label} is missing or non-numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{code} {label} is not finite")
    return number


def normalize_exchange_premium_quote(
    raw: dict[str, Any],
    catalog_entry: dict[str, Any],
    as_of: date,
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = catalog_entry["code"]
    if str(raw.get("f12", "")) != code:
        raise ValueError(f"Quote code differs from requested ETF {code}")
    price = _finite_quote_number(raw.get("f2"), "price", code)
    source_discount = _finite_quote_number(raw.get("f402"), "discount rate", code)
    change_pct = _finite_quote_number(raw.get("f3"), "change percentage", code)
    turnover_cny = _finite_quote_number(raw.get("f6"), "turnover", code)
    raw_iopv = raw.get("f441")
    if raw_iopv not in {None, "", "-"}:
        reference_value = _finite_quote_number(raw_iopv, "IOPV", code)
        reference_type = "iopv"
        reference_date: str | None = None
        reference_source_url = ETF_QUOTE_PAGE_URL
    elif reference and reference.get("reference_value_type") == "nav":
        reference_value = _finite_quote_number(
            reference.get("reference_value_cny"), "NAV", code
        )
        reference_type = "nav"
        reference_date = str(reference.get("reference_value_date") or "")
        reference_source_url = str(reference.get("reference_value_source_url") or "")
        if not reference_date or not reference_source_url.startswith("https://"):
            raise ValueError(f"{code} NAV reference metadata is incomplete")
    else:
        raise ValueError(f"{code} has neither IOPV nor a verified NAV reference")
    if price <= 0 or reference_value <= 0 or turnover_cny < 0:
        raise ValueError(
            f"{code} price, reference value, or turnover is outside its valid range"
        )
    try:
        quote_date = datetime.strptime(str(raw.get("f297")), "%Y%m%d").date()
        updated_timestamp = int(raw.get("f124"))
        updated_at = datetime.fromtimestamp(updated_timestamp, SHANGHAI_TZ)
    except (TypeError, ValueError, OSError) as exc:
        raise ValueError(f"{code} quote date or timestamp is invalid") from exc
    if quote_date > as_of or updated_at.date() > as_of:
        raise ValueError(f"{code} quote contains future data")
    if reference_type == "nav" and parse_date(reference_date) > as_of:
        raise ValueError(f"{code} NAV reference contains future data")
    premium_pct = -source_discount
    calculated_premium = (price / reference_value - 1) * 100
    tolerance_pct_points = max(0.05, 0.0005 / reference_value * 100 + 0.01)
    if abs(premium_pct - calculated_premium) > tolerance_pct_points:
        raise ValueError(
            f"{code} premium differs from price/{reference_type.upper()} calculation by "
            f"{abs(premium_pct - calculated_premium):.3f} percentage points"
        )
    return {
        **catalog_entry,
        "name": str(raw.get("f14") or catalog_entry["name"]).strip(),
        "market_price_cny": round(price, 4),
        "iopv_cny": round(reference_value, 4) if reference_type == "iopv" else None,
        "reference_value_type": reference_type,
        "reference_value_cny": round(reference_value, 4),
        "reference_value_date": reference_date,
        "reference_value_source_url": reference_source_url,
        "source_discount_pct": round(source_discount, 2),
        "premium_pct": round(premium_pct, 2),
        "change_pct": round(change_pct, 2),
        "turnover_cny": round(turnover_cny, 3),
        "quote_date": quote_date.isoformat(),
        "updated_at": updated_at.isoformat(timespec="seconds"),
        "quote_source_url": (
            f"https://quote.eastmoney.com/{'sh' if catalog_entry['market_id'] == 1 else 'sz'}{code}.html"
        ),
    }


def _cached_exchange_quote_valid(
    record: Any, entry: dict[str, Any], as_of: date
) -> bool:
    if not isinstance(record, dict) or record.get("code") != entry["code"]:
        return False
    try:
        quote_date = parse_date(str(record.get("quote_date", "")))
        updated_at = datetime.fromisoformat(str(record.get("updated_at", "")))
        reference_type = str(record.get("reference_value_type") or "iopv")
        reference_value = float(
            record.get("reference_value_cny", record.get("iopv_cny"))
        )
        reference_date = record.get("reference_value_date")
        reference_observed = (
            parse_date(str(reference_date)) if reference_date is not None else None
        )
        numeric = (
            float(record["market_price_cny"]),
            reference_value,
            float(record["source_discount_pct"]),
            float(record["premium_pct"]),
            float(record["change_pct"]),
            float(record["turnover_cny"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        quote_date <= as_of
        and updated_at.date() <= as_of
        and reference_type in {"iopv", "nav"}
        and (
            reference_type != "nav"
            or (
                reference_observed is not None
                and reference_observed <= as_of
                and str(record.get("reference_value_source_url") or "").startswith(
                    "https://"
                )
            )
        )
        and all(math.isfinite(value) for value in numeric)
        and numeric[0] > 0
        and numeric[1] > 0
        and numeric[5] >= 0
    )


def _load_exchange_premium_cache(
    path: Path, entries: list[dict[str, Any]], as_of: date
) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ETF_PREMIUM_CACHE_SCHEMA_VERSION
        or not isinstance(payload.get("records"), list)
    ):
        return {}
    entry_by_code = {entry["code"]: entry for entry in entries}
    cached: dict[str, dict[str, Any]] = {}
    for record in payload["records"]:
        code = str(record.get("code", "")) if isinstance(record, dict) else ""
        entry = entry_by_code.get(code)
        if entry and code not in cached and _cached_exchange_quote_valid(record, entry, as_of):
            normalized = {**entry, **record}
            normalized.setdefault("reference_value_type", "iopv")
            normalized.setdefault("reference_value_cny", normalized.get("iopv_cny"))
            normalized.setdefault("reference_value_date", None)
            normalized.setdefault("reference_value_source_url", ETF_QUOTE_PAGE_URL)
            cached[code] = normalized
    return cached


def _load_cached_qdii_exchange_premium_catalog(
    path: Path, metadata: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw_entries = payload.get("catalog") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        return None
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", ""))
        fund = metadata.get(code)
        if (
            not re.fullmatch(r"\d{6}", code)
            or code in seen
            or not is_qdii_fund_metadata(fund)
            or entry.get("category") != "qdii"
            or entry.get("market_id") not in {0, 1}
        ):
            continue
        seen.add(code)
        entries.append(dict(entry))
    if not entries:
        return None
    entries.sort(key=lambda item: item["code"])
    fingerprint = hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return entries, fingerprint


def exchange_premium_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    premium = record.get("premium_pct")
    return (
        premium is None,
        -float(premium) if premium is not None else math.inf,
        record["code"],
    )


def build_exchange_premium_snapshot(
    client: HttpClient,
    catalog_path: Path,
    cache_path: Path,
    as_of: date,
    *,
    catalog_entries: list[dict[str, Any]] | None = None,
    quote_rows: dict[str, dict[str, Any]] | None = None,
    catalog_fingerprint: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if catalog_entries is None:
        entries, loaded_fingerprint = load_exchange_premium_catalog(catalog_path)
        catalog_fingerprint = catalog_fingerprint or loaded_fingerprint
    else:
        entries = list(catalog_entries)
        catalog_fingerprint = catalog_fingerprint or hashlib.sha256(
            json.dumps(
                entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    cached = _load_exchange_premium_cache(cache_path, entries, as_of)
    dynamic_catalog = any(entry.get("category") == "qdii" for entry in entries)
    entry_by_code = {entry["code"]: entry for entry in entries}
    warnings: list[str] = []
    fresh: dict[str, dict[str, Any]] = {}
    quote_url = exchange_premium_quote_url(entries)
    try:
        if quote_rows is None:
            payload = client.get_json(quote_url, referer=ETF_QUOTE_PAGE_URL)
            rows = (
                ((payload.get("data") or {}).get("diff"))
                if isinstance(payload, dict)
                else None
            )
        else:
            rows = list(quote_rows.values())
        if not isinstance(rows, list):
            raise DataError("ETF quote response does not contain a record list")
        quote_references: dict[str, dict[str, Any]] = {}
        if dynamic_catalog:
            quote_references, reference_warnings = fetch_exchange_premium_lof_navs(
                client, entries, rows, as_of
            )
            warnings.extend(reference_warnings)
        invalid: list[str] = []
        seen_response: set[str] = set()
        for raw in rows:
            code = str(raw.get("f12", "")) if isinstance(raw, dict) else ""
            entry = entry_by_code.get(code)
            if entry is None:
                continue
            if code in seen_response:
                invalid.append(f"{code} duplicate response")
                fresh.pop(code, None)
                continue
            seen_response.add(code)
            if (
                any(
                    raw.get(field) in {None, "", "-"}
                    for field in ("f2", "f402", "f3", "f6")
                )
                and entry.get("category") == "qdii"
            ):
                continue
            try:
                fresh[code] = normalize_exchange_premium_quote(
                    raw, entry, as_of, quote_references.get(code)
                )
            except ValueError as exc:
                invalid.append(str(exc))
        missing = [entry["code"] for entry in entries if entry["code"] not in seen_response]
        if missing:
            invalid.append("missing " + ", ".join(missing))
        if invalid:
            warnings.append("场内溢价告警：部分行情未更新：" + "；".join(invalid))
    except (DataError, OSError, ValueError) as exc:
        warnings.append(f"场内溢价告警：行情刷新失败，使用缓存或空值：{exc}")

    records: list[dict[str, Any]] = []
    for entry in entries:
        code = entry["code"]
        if code in fresh:
            record = {**fresh[code], "quote_status": "fresh"}
        elif code in cached:
            record = {**entry, **cached[code], "quote_status": "stale"}
        else:
            record = {
                **entry,
                "market_price_cny": None,
                "iopv_cny": None,
                "reference_value_type": None,
                "reference_value_cny": None,
                "reference_value_date": None,
                "reference_value_source_url": None,
                "source_discount_pct": None,
                "premium_pct": None,
                "change_pct": None,
                "turnover_cny": None,
                "quote_date": None,
                "updated_at": None,
                "quote_source_url": None,
                "quote_status": "unavailable",
            }
        records.append(record)
    records.sort(key=exchange_premium_sort_key)
    discovered_count = len(records)
    if dynamic_catalog:
        records = [record for record in records if record["quote_status"] != "unavailable"]
    filtered_unavailable_count = discovered_count - len(records)
    fresh_count = sum(record["quote_status"] == "fresh" for record in records)
    stale_count = sum(record["quote_status"] == "stale" for record in records)

    if records and fresh_count == len(records):
        status = "fresh"
    elif fresh_count:
        status = "partial"
    elif stale_count:
        status = "stale"
    else:
        status = "unavailable"
    requested_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    cache_records = [record for record in records if record["premium_pct"] is not None]
    group_order = (
        sorted({str(entry["benchmark_group"]) for entry in entries})
        if dynamic_catalog
        else list(ETF_PREMIUM_GROUP_ORDER)
    )
    try:
        write_json(
            cache_path,
            {
                "schema_version": ETF_PREMIUM_CACHE_SCHEMA_VERSION,
                "catalog_fingerprint": catalog_fingerprint,
                "saved_at": requested_at,
                "catalog": entries,
                "records": cache_records,
            },
        )
    except OSError as exc:
        warnings.append(f"场内溢价告警：无法保存行情缓存：{exc}")
    return (
        {
            "schema_version": 1,
            "status": status,
            "requested_at": requested_at,
            "quote_delay_minutes": ETF_PREMIUM_DELAY_MINUTES,
            "discovered_count": discovered_count,
            "filtered_unavailable_count": filtered_unavailable_count,
            "expected_count": len(records),
            "fresh_count": fresh_count,
            "cache_hit_count": stale_count,
            "catalog_fingerprint": catalog_fingerprint,
            "group_order": group_order,
            "source_name": "东方财富场内基金行情",
            "source_url": ETF_QUOTE_PAGE_URL,
            "refresh_url": quote_url,
            "records": records,
        },
        warnings,
    )
