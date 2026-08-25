#!/usr/bin/env python3
"""Validate local multi-asset valuation artifacts and an optional deployment."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import update_index_valuation as valuation


class ValidationError(RuntimeError):
    """Raised when valuation artifacts cannot be published safely."""


class ValuationHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.text: list[str] = []
        self.source_links = 0
        self.home_links = 0
        self.asset_links = 0
        self.hidden_ids: set[str] = set()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))
            if "hidden" in attributes:
                self.hidden_ids.add(str(attributes["id"]))
        if tag == "a" and attributes.get("href") == "../":
            self.home_links += 1
        if tag == "a" and attributes.get("target") == "_blank":
            self.source_links += 1
        if tag == "a" and attributes.get("data-asset-link"):
            self.asset_links += 1

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def as_number(value: Any, label: str) -> float:
    require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{label} must be numeric")
    number = float(value)
    require(math.isfinite(number), f"{label} must be finite")
    return number


def as_positive(value: Any, label: str) -> float:
    number = as_number(value, label)
    require(number > 0, f"{label} must be positive")
    return number


def valid_date(value: Any, label: str, monthly: bool = False) -> str:
    text = str(value or "")
    try:
        datetime.strptime(text, "%Y-%m" if monthly else "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError(f"{label} is invalid") from exc
    return text


def load_payload(path: Path) -> dict[str, Any]:
    require(path.is_file(), f"Missing artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Could not parse {path}: {exc}") from exc
    require(isinstance(payload, dict), "Valuation JSON must be an object")
    return payload


def validate_proxy(asset: dict[str, Any], expected_date: datetime) -> None:
    history = asset.get("history")
    require(
        isinstance(history, list) and len(history) == valuation.WINDOW_MONTHS,
        f"{asset['id']} history must contain 120 months",
    )
    months: list[str] = []
    values: list[float] = []
    for index, item in enumerate(history):
        require(isinstance(item, dict), f"{asset['id']} history row {index} is invalid")
        month = valid_date(item.get("month"), f"{asset['id']} history month", monthly=True)
        value = as_positive(item.get("proxy_pe_ttm"), f"{asset['id']} proxy PE")
        months.append(month)
        values.append(value)
    require(len(set(months)) == len(months), f"{asset['id']} history months are duplicated")
    require(
        months == [valuation.add_months(months[-1], offset) for offset in range(1 - len(months), 1)],
        f"{asset['id']} history months are not consecutive",
    )
    require(asset.get("as_of") == months[-1], f"{asset['id']} as_of differs from history")
    require(
        months[-1] <= valuation.last_complete_month(expected_date.date()),
        f"{asset['id']} includes an unfinished month",
    )
    current = asset.get("current")
    require(isinstance(current, dict), f"{asset['id']} current is missing")
    require(current.get("sample_count") == valuation.WINDOW_MONTHS, f"{asset['id']} sample count is invalid")
    require(
        math.isclose(as_positive(current.get("proxy_pe_ttm"), "current proxy PE"), values[-1], abs_tol=1e-9),
        f"{asset['id']} current proxy PE differs from history",
    )
    expected_percentile = round(valuation.percentile_midrank(values, values[-1]), 2)
    require(
        math.isclose(as_number(current.get("proxy_percentile_10y"), "proxy percentile"), expected_percentile, abs_tol=0.005),
        f"{asset['id']} percentile does not reproduce midrank",
    )
    levels = current.get("reference_levels")
    require(isinstance(levels, dict), f"{asset['id']} reference levels are missing")
    for key, probability in (("p30", 0.30), ("p50", 0.50), ("p70", 0.70)):
        expected = round(valuation.quantile(values, probability), 4)
        require(
            math.isclose(as_number(levels.get(key), key), expected, abs_tol=0.00005),
            f"{asset['id']} {key} differs from the quantile formula",
        )
    method = asset.get("method")
    require(isinstance(method, dict), f"{asset['id']} method is missing")
    require(method.get("id") in {
        "rsp_spy_relative_price_proxy_v1",
        "eqwl_spy_relative_price_proxy_v1",
        "ewu_spy_relative_price_proxy_v1",
    }, f"{asset['id']} method is unsupported")
    require("source_rating" not in current, f"{asset['id']} proxy must not expose a source rating")
    anchor = method.get("anchor")
    require(isinstance(anchor, dict), f"{asset['id']} anchor is missing")
    target = as_positive(anchor.get("pe_ttm"), f"{asset['id']} anchor PE")
    source_pe = as_positive(anchor.get("sp500_pe_ttm"), f"{asset['id']} source anchor PE")
    ratio = as_positive(anchor.get("relative_price_ratio"), f"{asset['id']} anchor ratio")
    calibration = as_positive(anchor.get("calibration_constant"), f"{asset['id']} calibration")
    require(
        math.isclose(source_pe * ratio / calibration, target, abs_tol=1e-5),
        f"{asset['id']} anchor does not reproduce its target PE",
    )
    require(
        math.isclose(as_positive(anchor.get("reproduced_pe_ttm"), "reproduced anchor"), target, abs_tol=1e-5),
        f"{asset['id']} stored anchor reproduction is inconsistent",
    )
    require(asset.get("history_status") == "available", f"{asset['id']} history status is invalid")


def validate_direct(asset: dict[str, Any]) -> None:
    current = asset.get("current")
    require(isinstance(current, dict), f"{asset['id']} current is missing")
    for key in ("pe_ttm", "pb_mrq"):
        as_positive(current.get(key), f"{asset['id']} {key}")
    for key in ("pe_percentile_10y", "pb_percentile_10y"):
        value = as_number(current.get(key), f"{asset['id']} {key}")
        require(0 <= value <= 100, f"{asset['id']} {key} is outside 0-100")
    for key in ("roe_pct", "dividend_yield_pct"):
        as_number(current.get(key), f"{asset['id']} {key}")
    rating = current.get("source_rating")
    require(
        isinstance(rating, dict) and rating.get("provider") == "雪球" and rating.get("label"),
        f"{asset['id']} source rating is invalid",
    )
    valid_date(asset.get("as_of"), f"{asset['id']} as_of")
    require(asset.get("history") == [], f"{asset['id']} direct snapshot must not contain history")
    require(
        asset.get("history_status") == "not_provided_by_source",
        f"{asset['id']} direct history status is invalid",
    )
    require(asset.get("method", {}).get("id") == "snowball_index_eva_snapshot_v1", f"{asset['id']} method is invalid")


def validate_gold(asset: dict[str, Any]) -> None:
    current = asset.get("current")
    require(isinstance(current, dict), "Gold current snapshot is missing")
    as_positive(current.get("spot_usd_oz"), "gold spot")
    percentiles = current.get("percentiles")
    require(isinstance(percentiles, dict) and set(percentiles) == {"1y", "3y", "5y", "10y", "all"}, "Gold percentiles are incomplete")
    for key, value in percentiles.items():
        number = as_number(value, f"gold {key} percentile")
        require(0 <= number <= 100, f"gold {key} percentile is outside 0-100")
    for key in ("source_rating_1y", "source_rating_all"):
        rating = current.get(key)
        require(isinstance(rating, dict) and rating.get("provider") and rating.get("label"), f"Gold {key} is invalid")
    as_number(current.get("residual"), "gold residual")
    factors = current.get("factors")
    require(
        isinstance(factors, dict)
        and set(factors) == {"tips_real_yield", "china_us_spread", "gold_oil_ratio"},
        "Gold factors are incomplete",
    )
    for key, factor in factors.items():
        as_number(factor.get("value"), f"gold factor {key}")
        valid_date(factor.get("date"), f"gold factor {key} date")
        require(isinstance(factor.get("lag_days"), int) and factor["lag_days"] >= 0, f"gold factor {key} lag is invalid")
    valid_date(asset.get("as_of"), "gold as_of")
    require(asset.get("history") == [], "Gold snapshot must not contain history")
    require(asset.get("history_status") == "not_published_by_source", "Gold history status is invalid")
    require(asset.get("method", {}).get("id") == "external_dual_anchor_three_factor_snapshot_v1", "Gold method is invalid")


def validate_payload(payload: dict[str, Any], expected_date: str) -> None:
    require(payload.get("schema_version") == valuation.SCHEMA_VERSION, "Unexpected valuation JSON schema")
    require(payload.get("status") in {"fresh", "partial", "stale", "unavailable"}, "Invalid valuation status")
    try:
        generated = datetime.fromisoformat(str(payload["generated_at"]))
        expected = datetime.strptime(expected_date, "%Y-%m-%d")
    except (KeyError, ValueError) as exc:
        raise ValidationError("Invalid generation or expected date") from exc
    require(generated.date() == expected.date(), "Valuation generation date differs from expected date")
    require(payload.get("default_asset_id") == "sp-500-equal-weight", "Unexpected default asset")
    assets = payload.get("assets")
    require(isinstance(assets, list) and len(assets) == 7, "Exactly seven valuation assets are required")
    require(tuple(asset.get("id") for asset in assets) == valuation.EXPECTED_ASSET_IDS, "Valuation asset IDs or order differ")
    allowed_statuses = {"fresh", "cached_stale", "unavailable"}
    available_count = 0
    for asset in assets:
        require(asset.get("status") in allowed_statuses, f"{asset.get('id')} has an invalid status")
        require(asset.get("source_mode") in {"direct", "proxy", "external_model"}, f"{asset.get('id')} has an invalid source mode")
        require(isinstance(asset.get("source_ids"), list) and asset["source_ids"], f"{asset.get('id')} source IDs are missing")
        require(isinstance(asset.get("warnings"), list), f"{asset.get('id')} warnings are invalid")
        if asset["status"] == "unavailable":
            require(asset.get("as_of") is None and asset.get("current") == {} and asset.get("history") == [], f"{asset['id']} unavailable shape is invalid")
            require(bool(asset["warnings"]), f"{asset['id']} unavailable status lacks a warning")
            continue
        available_count += 1
        if asset["source_mode"] == "direct":
            validate_direct(asset)
        elif asset["source_mode"] == "proxy":
            validate_proxy(asset, expected)
        else:
            validate_gold(asset)
    require(available_count > 0, "All valuation assets are unavailable")
    expected_status = valuation.aggregate_status(assets)
    require(payload["status"] == expected_status, "Page status does not match asset states")

    sources = payload.get("sources")
    require(isinstance(sources, list) and len(sources) == 7, "Exactly seven normalized sources are required")
    require(tuple(source.get("id") for source in sources) == valuation.all_source_ids(), "Valuation source IDs differ")
    stale_or_missing = 0
    for source in sources:
        require(source.get("status") in allowed_statuses, f"{source.get('id')} has an invalid source status")
        require(str(source.get("url", "")).startswith("https://"), f"{source.get('id')} URL is invalid")
        require(source.get("request_mode") in {"full", "tail", "conditional"}, f"{source.get('id')} request mode is invalid")
        if source["status"] == "unavailable":
            require(source.get("last_success_at") is None, f"{source['id']} unavailable source has a success time")
        else:
            try:
                datetime.fromisoformat(str(source["last_success_at"]))
            except ValueError as exc:
                raise ValidationError(f"{source['id']} last-success timestamp is invalid") from exc
            require(as_number(source.get("age_hours"), f"{source['id']} age") >= 0, f"{source['id']} age is negative")
        stale_or_missing += source["status"] != "fresh"

    cache = payload.get("cache")
    require(isinstance(cache, dict), "Cache metadata must be an object")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", str(cache.get("key", "")))), "Cache key is invalid")
    require(cache.get("startup") in {"cold", "hot"}, "Cache startup is invalid")
    require(cache.get("refresh_mode") in {"full", "tail"}, "Cache refresh mode is invalid")
    for key in ("hit", "fallback", "unavailable"):
        require(isinstance(cache.get(key), bool), f"Cache {key} must be boolean")
    warnings = payload.get("warnings")
    require(isinstance(warnings, list) and all(isinstance(item, str) for item in warnings), "Warnings must be strings")
    degraded_assets = any(asset["status"] != "fresh" for asset in assets)
    require(
        bool(warnings) == bool(stale_or_missing or degraded_assets),
        "Source or asset degradation must match visible warnings",
    )
    source_statuses = {source["id"]: source["status"] for source in sources}
    for asset in assets:
        dependency_states = [source_statuses[source_id] for source_id in asset["source_ids"]]
        expected_asset_status = (
            "unavailable" if "unavailable" in dependency_states
            else "cached_stale" if "cached_stale" in dependency_states
            else asset["status"]
        )
        require(
            asset["status"] == expected_asset_status,
            f"{asset['id']} status differs from its source dependencies",
        )


def validate_html(document: str, payload: dict[str, Any], label: str) -> None:
    parser = ValuationHtmlParser()
    parser.feed(document)
    text = " ".join(" ".join(parser.text).split())
    for required_id in ("valuation-overview", "overview-heading", "asset-table", "valuation-detail"):
        require(required_id in parser.ids, f"{label} is missing #{required_id}")
    require("valuation-detail" in parser.hidden_ids, f"{label} detail view must be hidden by default")
    require("valuation-overview" not in parser.hidden_ids, f"{label} overview must be visible by default")
    require(parser.home_links == 1, f"{label} must link back to the ranking page")
    require(parser.source_links >= 4, f"{label} source links are incomplete")
    require(parser.asset_links == 14, f"{label} must contain two native detail links per asset")
    for expected in (
        "指数与黄金估值研究",
        "雪球直取",
        "研究代理",
        "来源评级",
        "不代表本站判断",
        "返回估值概览",
        "asset-switcher",
        "window.__INDEX_VALUATION__=",
    ):
        require(expected in document or expected in text, f"{label} is missing expected text: {expected}")
    for asset in payload["assets"]:
        require(asset["name"] in text, f"{label} is missing asset name {asset['name']}")
        require(asset["id"] in document, f"{label} is missing asset ID {asset['id']}")
        require(asset["method"]["id"] in document, f"{label} is missing method {asset['method']['id']}")
    require("data-chart-overlay" in document, f"{label} lacks chart rendering code")
    require("data-filter=\"proxy\"" in document, f"{label} lacks source-mode filters")


def validate_local_artifacts(
    output_dir: Path, publish_dir: Path, expected_date: str
) -> dict[str, Any]:
    payload = load_payload(output_dir / "latest.json")
    validate_payload(payload, expected_date)
    generated_path = output_dir / "latest.html"
    published_path = publish_dir / "valuation" / "index.html"
    require(generated_path.is_file(), f"Missing artifact: {generated_path}")
    require(published_path.is_file(), f"Missing artifact: {published_path}")
    generated = generated_path.read_bytes()
    published = published_path.read_bytes()
    require(generated == published, "Valuation HTML files are not byte-identical")
    try:
        document = generated.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("Valuation HTML is not UTF-8") from exc
    validate_html(document, payload, "Valuation HTML")
    return payload


def validate_deployment(
    url: str, payload: dict[str, Any], attempts: int, delay_seconds: float
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "index-valuation-deployment-validator/2.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                require(response.status == 200, f"Deployment returned HTTP {response.status}")
                document = response.read().decode("utf-8")
            validate_html(document, payload, "Deployed valuation HTML")
            return
        except (OSError, UnicodeDecodeError, urllib.error.HTTPError, urllib.error.URLError, ValidationError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)
    raise ValidationError(f"Valuation deployment verification failed after {attempts} attempts: {last_error}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("output/index-valuation"))
    parser.add_argument("--publish-dir", type=Path, default=Path("public"))
    parser.add_argument("--expected-date", default=valuation.current_shanghai_time().date().isoformat())
    parser.add_argument("--deployed-url")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--delay-seconds", type=float, default=0)
    args = parser.parse_args(argv)
    if args.attempts <= 0:
        parser.error("--attempts must be positive")
    if args.delay_seconds < 0:
        parser.error("--delay-seconds must be non-negative")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = validate_local_artifacts(
            args.output_dir.resolve(), args.publish_dir.resolve(), args.expected_date
        )
        if args.deployed_url:
            validate_deployment(args.deployed_url, payload, args.attempts, args.delay_seconds)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    available = sum(asset["status"] != "unavailable" for asset in payload["assets"])
    print(
        f"Validated {available}/{len(payload['assets'])} valuation assets"
        + (" and the deployed page" if args.deployed_url else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
