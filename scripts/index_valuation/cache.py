"""Valuation cache validation and source refresh."""

import hashlib
import json
import time
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .common import (
    CACHE_SCHEMA_VERSION,
    DQYDJ_SOURCE_ID,
    EXPECTED_ASSET_IDS,
    FetchResponse,
    GOLD_SOURCE_ID,
    HttpClient,
    NASDAQ_TICKERS,
    NDXTMC_HISTORY_CHUNK_DAYS,
    NDXTMC_HISTORY_HEADERS,
    NDXTMC_LIVE_START,
    NDXTMC_SOURCE_ID,
    NDXTMC_WORKBOOK_HEADERS,
    RELATIVE_PROXY_ASSET_ID,
    SCHEMA_VERSION,
    SNOWBALL_SOURCE_ID,
    TAIL_DAYS,
    ValuationError,
    month_start,
    positive_number,
)
from .sources import (
    merge_ndxtmc_points,
    ndxtmc_workbook_matches_cache,
    parse_ndxtmc_history,
    parse_ndxtmc_workbook,
    validate_ndxtmc_boundary,
    validate_pe_points,
    validate_price_points,
)

def load_catalog(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        catalog = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValuationError(f"Could not read valuation catalog {path}: {exc}") from exc
    try:
        if catalog["schema_version"] != SCHEMA_VERSION:
            raise ValuationError("Valuation catalog schema is unsupported")
        assets = catalog["assets"]
        ids = tuple(item["id"] for item in assets)
        if ids != EXPECTED_ASSET_IDS:
            raise ValuationError(f"Unexpected valuation asset order: {ids}")
        if catalog["default_asset_id"] not in ids:
            raise ValuationError("Default valuation asset is missing")
        for item in assets:
            if item["source_mode"] == "proxy":
                value_kind = item.get("value_kind", "calibrated_pe")
                if item["baseline_ticker"] != "SPY":
                    raise ValuationError("Proxy catalog baseline is unsupported")
                if value_kind == "relative_score":
                    if item["id"] != RELATIVE_PROXY_ASSET_ID or item["ticker"] != "NDXTMC":
                        raise ValuationError("Relative proxy catalog configuration is unsupported")
                    if "anchor" in item:
                        raise ValuationError("Relative proxy must not define a PE anchor")
                elif value_kind == "calibrated_pe" and item["ticker"] in NASDAQ_TICKERS:
                    month_start(item["anchor"]["month"])
                    positive_number(item["anchor"]["pe_ttm"], "proxy anchor PE")
                else:
                    raise ValuationError("Proxy catalog ticker configuration is unsupported")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValuationError("Valuation catalog is incomplete") from exc
    return catalog, hashlib.sha256(raw).hexdigest()


def source_cache_id(ticker: str) -> str:
    return f"nasdaq-{ticker.lower()}"


def all_source_ids() -> tuple[str, ...]:
    return (SNOWBALL_SOURCE_ID, GOLD_SOURCE_ID, DQYDJ_SOURCE_ID) + tuple(
        source_cache_id(ticker) for ticker in NASDAQ_TICKERS
    ) + (NDXTMC_SOURCE_ID,)


def source_fingerprint(catalog_hash: str, source_id: str) -> str:
    identity = {
        "cache_schema": CACHE_SCHEMA_VERSION,
        "catalog_sha256": catalog_hash,
        "source_id": source_id,
        "parser_version": {
            "snowball": 1,
            "gold": 1,
            "dqydj": 1,
            "nasdaq": 1,
            "ndxtmc": 1,
        },
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def cache_fingerprint(catalog: dict[str, Any], catalog_hash: str) -> str:
    del catalog
    identity = {
        "schema": CACHE_SCHEMA_VERSION,
        "catalog_sha256": catalog_hash,
        "sources": all_source_ids(),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _anchor_months(catalog: dict[str, Any]) -> set[str]:
    return {
        asset["anchor"]["month"]
        for asset in catalog["assets"]
        if asset["source_mode"] == "proxy" and "anchor" in asset
    }


def validate_source_data(
    source_id: str, data: Any, catalog: dict[str, Any], fetched_on: date
) -> None:
    if source_id == SNOWBALL_SOURCE_ID:
        expected = {
            asset["source_code"]
            for asset in catalog["assets"]
            if asset["source_mode"] == "direct"
        }
        if not isinstance(data, list) or {item.get("code") for item in data} != expected:
            raise ValuationError("雪球规范化缓存不完整")
        for item in data:
            positive_number(item.get("pe_ttm"), "cached direct PE")
            datetime.strptime(item["as_of"], "%Y-%m-%d")
    elif source_id == GOLD_SOURCE_ID:
        if not isinstance(data, dict):
            raise ValuationError("黄金规范化缓存不是对象")
        datetime.strptime(data["as_of"], "%Y-%m-%d")
        positive_number(data["spot_usd_oz"], "cached gold spot")
        if set(data["percentiles"]) != {"1y", "3y", "5y", "10y", "all"}:
            raise ValuationError("黄金规范化缓存缺少分位")
        if set(data["factors"]) != {
            "tips_real_yield",
            "china_us_spread",
            "gold_oil_ratio",
        }:
            raise ValuationError("黄金规范化缓存缺少因子")
    elif source_id == DQYDJ_SOURCE_ID:
        if not isinstance(data, list):
            raise ValuationError("DQYDJ 规范化缓存不是列表")
        validate_pe_points(data, _anchor_months(catalog))
    elif source_id == NDXTMC_SOURCE_ID:
        if not isinstance(data, list):
            raise ValuationError("NDXTMC 规范化缓存不是列表")
        validate_price_points(data, "NDXTMC", set())
        static_points = [
            item for item in data
            if datetime.strptime(str(item["date"]), "%Y-%m-%d").date() < NDXTMC_LIVE_START
        ]
        live_points = [
            item for item in data
            if datetime.strptime(str(item["date"]), "%Y-%m-%d").date() >= NDXTMC_LIVE_START
        ]
        if not static_points or not live_points:
            raise ValuationError("NDXTMC cache lacks an official static or live history segment")
        validate_ndxtmc_boundary([static_points[-1]], [live_points[0]])
    elif source_id.startswith("nasdaq-"):
        ticker = source_id.removeprefix("nasdaq-").upper()
        required = {
            asset["anchor"]["month"]
            for asset in catalog["assets"]
            if asset["source_mode"] == "proxy"
            and "anchor" in asset
            and (asset["ticker"] == ticker or asset["baseline_ticker"] == ticker)
        }
        if not isinstance(data, list):
            raise ValuationError(f"{ticker} 规范化缓存不是列表")
        validate_price_points(data, ticker, required)
    else:
        raise ValuationError(f"Unknown valuation source: {source_id}")
    del fetched_on


def load_source_caches(
    cache_dir: Path, catalog: dict[str, Any], catalog_hash: str, as_of: date
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    caches: dict[str, dict[str, Any]] = {}
    states: dict[str, str] = {}
    for source_id in all_source_ids():
        path = cache_dir / f"{source_id}.json"
        if not path.is_file():
            states[source_id] = "missing"
            continue
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("schema_version") != CACHE_SCHEMA_VERSION:
                states[source_id] = "schema_mismatch"
                continue
            if cached.get("fingerprint") != source_fingerprint(catalog_hash, source_id):
                states[source_id] = "fingerprint_mismatch"
                continue
            if cached.get("source_id") != source_id:
                states[source_id] = "source_mismatch"
                continue
            datetime.fromisoformat(cached["last_success_at"])
            validate_source_data(source_id, cached["data"], catalog, as_of)
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            json.JSONDecodeError,
            ValuationError,
        ):
            states[source_id] = "corrupt"
            continue
        caches[source_id] = cached
        states[source_id] = "valid"
    return caches, states


def load_manifest(
    cache_dir: Path, expected_fingerprint: str
) -> tuple[dict[str, Any] | None, str]:
    path = cache_dir / "manifest.json"
    if not path.is_file():
        return None, "missing"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None, "schema_mismatch"
        if manifest.get("fingerprint") != expected_fingerprint:
            return None, "fingerprint_mismatch"
        if manifest.get("last_full_refresh_month") is not None:
            month_start(manifest["last_full_refresh_month"])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None, "corrupt"
    return manifest, "valid"


def format_nasdaq_url(ticker: str, start: date, end: date, template: str) -> str:
    query = urllib.parse.urlencode(
        {
            "assetclass": "etf",
            "fromdate": start.isoformat(),
            "todate": end.isoformat(),
            "limit": "5000",
        }
    )
    return f"{template.format(ticker=ticker)}?{query}"


def conditional_headers(
    base: dict[str, str], cached: dict[str, Any] | None
) -> dict[str, str]:
    headers = dict(base)
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
    return headers


def _date_chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=NDXTMC_HISTORY_CHUNK_DAYS - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def fetch_ndxtmc_source(
    client: HttpClient,
    source: dict[str, Any],
    cached: dict[str, Any] | None,
    as_of: date,
    refresh_mode: str,
) -> FetchResponse:
    started = time.perf_counter()
    workbook_response = client.fetch(
        source["workbook_url"],
        conditional_headers(NDXTMC_WORKBOOK_HEADERS, cached),
    )
    workbook_changed = workbook_response.status == 200
    if workbook_changed:
        workbook_points = parse_ndxtmc_workbook(workbook_response.body)
        if cached is not None and ndxtmc_workbook_matches_cache(
            workbook_points, cached["data"]
        ):
            workbook_changed = False
        base_points = workbook_points if workbook_changed else cached["data"]
    elif workbook_response.status == 304 and cached is not None:
        workbook_points = []
        base_points = cached["data"]
    else:
        raise ValuationError("NDXTMC workbook returned 304 without a valid cache")

    live_start = (
        NDXTMC_LIVE_START
        if refresh_mode == "full" or cached is None or workbook_changed
        else max(NDXTMC_LIVE_START, as_of - timedelta(days=TAIL_DAYS))
    )
    live_parts: list[list[dict[str, Any]]] = []
    total_bytes = len(workbook_response.body)
    total_attempts = workbook_response.attempts
    for chunk_start, chunk_end in _date_chunks(live_start, as_of):
        response = client.post_form(
            source["history_url"],
            {
                "id": "NDXTMC",
                "startDate": f"{chunk_start.isoformat()}T00:00:00",
                "endDate": f"{chunk_end.isoformat()}T00:00:00",
            },
            NDXTMC_HISTORY_HEADERS,
        )
        live_parts.append(parse_ndxtmc_history(response.body))
        total_bytes += len(response.body)
        total_attempts += response.attempts
    live_points = merge_ndxtmc_points(*live_parts)
    if workbook_changed:
        validate_ndxtmc_boundary(workbook_points, live_points)
    merged = merge_ndxtmc_points(base_points, live_points)
    body = json.dumps(merged, separators=(",", ":")).encode("utf-8")
    return FetchResponse(
        body=body,
        elapsed_seconds=time.perf_counter() - started,
        attempts=total_attempts,
        status=200,
        url=source["page_url"],
        headers={
            "etag": workbook_response.headers.get("etag", ""),
            "last-modified": workbook_response.headers.get("last-modified", ""),
            "x-download-bytes": str(total_bytes),
        },
    )
