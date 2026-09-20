"""Valuation payload orchestration."""

import time
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .cache import (
    _anchor_months,
    all_source_ids,
    cache_fingerprint,
    conditional_headers,
    fetch_ndxtmc_source,
    format_nasdaq_url,
    source_fingerprint,
    validate_source_data,
)
from .common import (
    CACHE_SCHEMA_VERSION,
    DQYDJ_HEADERS,
    DQYDJ_SOURCE_ID,
    FULL_SCAN_BUFFER_MONTHS,
    FetchResponse,
    GOLD_HEADERS,
    GOLD_SOURCE_ID,
    HttpClient,
    NASDAQ_HEADERS,
    NASDAQ_TICKERS,
    NDXTMC_SOURCE_ID,
    SCHEMA_VERSION,
    SNOWBALL_HEADERS,
    SNOWBALL_SOURCE_ID,
    TAIL_DAYS,
    ValuationError,
    WINDOW_MONTHS,
    add_months,
    last_complete_month,
    month_start,
    source_cache_id,
)
from .models import (
    _parse_response,
    _source_name,
    _source_page_url,
    aggregate_status,
    build_direct_asset,
    build_gold_asset,
    build_proxy_asset,
    build_relative_proxy_asset,
    iso_age_hours,
    run_parallel_requests,
)


def _refresh_sources(
    as_of: date,
    catalog: dict[str, Any],
    caches: dict[str, dict[str, Any]],
    manifest: dict[str, Any] | None,
    client: HttpClient,
) -> tuple[str, date, dict[str, FetchResponse], dict[str, str], float]:
    run_month = as_of.strftime("%Y-%m")
    nasdaq_ids = tuple(source_cache_id(ticker) for ticker in NASDAQ_TICKERS)
    all_nasdaq_cached = all(
        source_id in caches for source_id in nasdaq_ids + (NDXTMC_SOURCE_ID,)
    )
    refresh_mode = (
        "tail"
        if manifest is not None
        and manifest.get("last_full_refresh_month") == run_month
        and all_nasdaq_cached
        else "full"
    )
    through = last_complete_month(as_of)
    anchor_months = _anchor_months(catalog)
    earliest = min(add_months(through, 1 - WINDOW_MONTHS), min(anchor_months))
    price_start = (
        month_start(add_months(earliest, -FULL_SCAN_BUFFER_MONTHS))
        if refresh_mode == "full"
        else as_of - timedelta(days=TAIL_DAYS)
    )
    sources = catalog["sources"]
    requests: dict[str, Callable[[], FetchResponse]] = {
        SNOWBALL_SOURCE_ID: lambda: client.fetch(
            sources[SNOWBALL_SOURCE_ID]["url"],
            conditional_headers(SNOWBALL_HEADERS, caches.get(SNOWBALL_SOURCE_ID)),
        ),
        GOLD_SOURCE_ID: lambda: client.fetch(
            sources[GOLD_SOURCE_ID]["url"],
            conditional_headers(GOLD_HEADERS, caches.get(GOLD_SOURCE_ID)),
        ),
        DQYDJ_SOURCE_ID: lambda: client.fetch(
            sources[DQYDJ_SOURCE_ID]["url"], DQYDJ_HEADERS
        ),
        NDXTMC_SOURCE_ID: lambda: fetch_ndxtmc_source(
            client, sources["ndxtmc"], caches.get(NDXTMC_SOURCE_ID), as_of, refresh_mode
        ),
    }
    for ticker in NASDAQ_TICKERS:
        source_id = source_cache_id(ticker)
        url = format_nasdaq_url(ticker, price_start, as_of, sources["nasdaq"]["url"])
        requests[source_id] = lambda request_url=url: client.fetch(
            request_url, NASDAQ_HEADERS
        )
    responses, failures, wall_seconds = run_parallel_requests(requests)
    return refresh_mode, through, responses, failures, wall_seconds


def _build_assets(
    catalog: dict[str, Any],
    normalized: dict[str, Any],
    source_states: dict[str, str],
    through: date,
    as_of: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    assets: list[dict[str, Any]] = []
    warnings: list[str] = []
    for config in catalog["assets"]:
        if config["source_mode"] == "direct":
            asset = build_direct_asset(
                config, normalized[SNOWBALL_SOURCE_ID], source_states
            )
        elif config["source_mode"] == "proxy":
            asset = (
                build_relative_proxy_asset(config, normalized, source_states, through)
                if config.get("value_kind") == "relative_score"
                else build_proxy_asset(config, normalized, source_states, through)
            )
        else:
            asset = build_gold_asset(
                config, normalized[GOLD_SOURCE_ID], source_states, catalog, as_of
            )
        assets.append(asset)
        warnings.extend(f"{asset['name']}：{item}" for item in asset["warnings"])
    return assets, warnings


def build_payload(
    *,
    as_of: date,
    now: datetime,
    catalog: dict[str, Any],
    catalog_hash: str,
    caches: dict[str, dict[str, Any]],
    cache_states: dict[str, str],
    manifest: dict[str, Any] | None,
    manifest_state: str,
    client: HttpClient,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    run_month = as_of.strftime("%Y-%m")
    refresh_mode, through, responses, failures, request_wall_seconds = _refresh_sources(
        as_of, catalog, caches, manifest, client
    )
    parse_started = time.perf_counter()
    normalized: dict[str, Any] = {}
    new_caches: dict[str, dict[str, Any]] = {}
    source_states: dict[str, str] = {}
    source_metrics: dict[str, Any] = {}
    public_sources: list[dict[str, Any]] = []
    warnings: list[str] = []
    generated_at = now.isoformat()

    for source_id in all_source_ids():
        cached = caches.get(source_id)
        response = responses.get(source_id)
        error = failures.get(source_id)
        request_mode = (
            refresh_mode if source_id.startswith("nasdaq-") else
            ("conditional" if source_id in {SNOWBALL_SOURCE_ID, GOLD_SOURCE_ID} else "full")
        )
        cache_fallback = False
        source_error: str | None = None
        try:
            if response is None:
                raise ValuationError(error or "source returned no response")
            data = _parse_response(
                source_id, response, cached, catalog, as_of, refresh_mode
            )
            validate_source_data(source_id, data, catalog, as_of)
            normalized[source_id] = data
            source_states[source_id] = "fresh"
            previous_data_at = (cached or {}).get("data_updated_at")
            data_updated_at = previous_data_at if response.status == 304 else generated_at
            new_caches[source_id] = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "fingerprint": source_fingerprint(catalog_hash, source_id),
                "source_id": source_id,
                "last_success_at": generated_at,
                "data_updated_at": data_updated_at or generated_at,
                "etag": response.headers.get("etag") or (cached or {}).get("etag"),
                "last_modified": response.headers.get("last-modified")
                or (cached or {}).get("last_modified"),
                "data": data,
            }
        except (KeyError, TypeError, ValueError, ValuationError) as exc:
            source_error = str(exc).replace("\n", " ")[:280]
            if cached is not None:
                normalized[source_id] = cached["data"]
                new_caches[source_id] = cached
                source_states[source_id] = "cached_stale"
                cache_fallback = True
                warnings.append(
                    f"{_source_name(source_id, catalog)}刷新失败，使用 "
                    f"{cached['last_success_at']} 的缓存：{source_error}"
                )
            else:
                normalized[source_id] = None
                source_states[source_id] = "unavailable"
                warnings.append(
                    f"{_source_name(source_id, catalog)}不可用且无有效缓存：{source_error}"
                )
        active_cache = new_caches.get(source_id)
        public_sources.append(
            {
                "id": source_id,
                "name": _source_name(source_id, catalog),
                "url": _source_page_url(source_id, catalog),
                "status": source_states[source_id],
                "request_mode": request_mode,
                "http_status": response.status if response else None,
                "last_success_at": active_cache.get("last_success_at") if active_cache else None,
                "data_updated_at": active_cache.get("data_updated_at") if active_cache else None,
                "age_hours": iso_age_hours(
                    active_cache.get("last_success_at") if active_cache else None, now
                ),
                "error": source_error,
            }
        )
        source_metrics[source_id] = {
            "seconds": round(response.elapsed_seconds, 3) if response else 0.0,
            "bytes": (
                int(response.headers.get("x-download-bytes", len(response.body)))
                if response else 0
            ),
            "attempts": response.attempts if response else 0,
            "http_status": response.status if response else None,
            "request_mode": request_mode,
            "cache_fallback": cache_fallback,
            "unavailable": source_states[source_id] == "unavailable",
            **({"error": source_error} if source_error else {}),
        }

    assets, asset_warnings = _build_assets(
        catalog, normalized, source_states, through, as_of
    )
    warnings.extend(asset_warnings)

    proxy_sources_fresh = all(
        source_states[source_id] == "fresh"
        for source_id in (DQYDJ_SOURCE_ID,)
        + tuple(source_cache_id(ticker) for ticker in NASDAQ_TICKERS)
        + (NDXTMC_SOURCE_ID,)
    )
    last_full_month = (
        run_month
        if refresh_mode == "full" and proxy_sources_fresh
        else (manifest or {}).get("last_full_refresh_month")
    )
    global_fingerprint = cache_fingerprint(catalog, catalog_hash)
    new_manifest = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "fingerprint": global_fingerprint,
        "catalog_sha256": catalog_hash,
        "updated_at": generated_at,
        "last_full_refresh_month": last_full_month,
    }
    status = aggregate_status(assets)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "generated_at": generated_at,
        "default_asset_id": catalog["default_asset_id"],
        "assets": assets,
        "sources": public_sources,
        "cache": {
            "key": global_fingerprint,
            "startup": "hot" if len(caches) == len(all_source_ids()) else "cold",
            "hit": bool(caches),
            "hit_count": len(caches),
            "source_count": len(all_source_ids()),
            "load_states": cache_states,
            "manifest_state": manifest_state,
            "refresh_mode": refresh_mode,
            "fallback": any(state == "cached_stale" for state in source_states.values()),
            "unavailable": any(state == "unavailable" for state in source_states.values()),
            "updated_at": generated_at,
            "last_full_refresh_month": last_full_month,
        },
        "warnings": warnings,
    }
    request_metrics = {
        "request_wall_seconds": round(request_wall_seconds, 3),
        "parse_seconds": round(time.perf_counter() - parse_started, 3),
        "sources": source_metrics,
    }
    return payload, new_caches, new_manifest, request_metrics


def _summary_values(asset: dict[str, Any]) -> tuple[str, str, str]:
    if asset["status"] == "unavailable":
        return "--", "--", "--"
    current = asset["current"]
    if asset["source_mode"] == "direct":
        return (
            f"PE {current['pe_ttm']:.2f}",
            f"{current['pe_percentile_10y']:.1f}%",
            current["source_rating"]["label"],
        )
    if asset["source_mode"] == "proxy":
        if asset.get("method", {}).get("value_kind") == "relative_score":
            return (
                f"{current['sample_count']} 月样本",
                f"{current['relative_percentile_10y']:.1f}%",
                "--",
            )
        return (
            f"代理 PE {current['proxy_pe_ttm']:.2f}",
            f"{current['proxy_percentile_10y']:.1f}%",
            "--",
        )
    return (
        f"${current['spot_usd_oz']:,.2f}",
        f"{current['percentiles']['10y']:.1f}%",
        current["source_rating_all"]["label"],
    )
