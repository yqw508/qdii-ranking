#!/usr/bin/env python3
"""Build the standalone multi-asset valuation research page."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from threading import get_ident
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 2
CACHE_SCHEMA_VERSION = 2
WINDOW_MONTHS = 120
TAIL_DAYS = 100
SHANGHAI_TZ = timezone(timedelta(hours=8))
DEFAULT_CATALOG = (
    Path(__file__).resolve().parents[1] / "references" / "index-valuation-catalog.json"
)
DEFAULT_PAGE_SCRIPT = Path(__file__).resolve().parent / "valuation_page.js"
DEFAULT_OUTPUT_DIR = Path.cwd() / "output" / "index-valuation"
DEFAULT_PUBLISH_DIR = Path.cwd() / "public"
DEFAULT_CACHE_DIR = (
    Path.cwd() / "output" / "qdii-ranking" / "cache" / "index-valuation"
)
SNOWBALL_SOURCE_ID = "snowball"
GOLD_SOURCE_ID = "gold"
DQYDJ_SOURCE_ID = "dqydj"
NASDAQ_TICKERS = ("RSP", "EQWL", "EWU", "SPY")
DIRECT_ASSET_IDS = ("nasdaq-100", "sp-500", "dax")
PROXY_ASSET_IDS = (
    "sp-500-equal-weight",
    "sp-100-equal-weight",
    "ftse-100-proxy",
)
GOLD_ASSET_ID = "gold-dual-anchor"
EXPECTED_ASSET_IDS = DIRECT_ASSET_IDS + PROXY_ASSET_IDS + (GOLD_ASSET_ID,)
PERFORMANCE_TARGETS = {
    "hot_seconds": 10.0,
    "cold_seconds": 15.0,
    "hot_download_bytes": 300_000,
}
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
NASDAQ_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
DQYDJ_HEADERS = {
    **BASE_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SNOWBALL_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://danjuanfunds.com/rn/value-center",
}
GOLD_HEADERS = {**DQYDJ_HEADERS}
SOURCE_RATING_LABELS = {
    "low": "偏低",
    "normal": "适中",
    "mid": "适中",
    "middle": "适中",
    "high": "偏高",
}


class ValuationError(RuntimeError):
    """Raised when valuation artifacts cannot be generated safely."""


@dataclass(frozen=True)
class FetchResponse:
    body: bytes
    elapsed_seconds: float
    attempts: int
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)


class HttpClient:
    def __init__(self, timeout: float = 30.0, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries

    def fetch(self, url: str, headers: dict[str, str]) -> FetchResponse:
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    status = int(response.status)
                    response_headers = {key.lower(): value for key, value in response.headers.items()}
                if status == 304:
                    body = b""
                elif status != 200 or not body:
                    raise ValuationError(f"HTTP {status} or empty response")
                return FetchResponse(
                    body=body,
                    elapsed_seconds=time.perf_counter() - started,
                    attempts=attempt,
                    status=status,
                    url=url,
                    headers=response_headers,
                )
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return FetchResponse(
                        body=b"",
                        elapsed_seconds=time.perf_counter() - started,
                        attempts=attempt,
                        status=304,
                        url=url,
                        headers={key.lower(): value for key, value in exc.headers.items()},
                    )
                last_error = exc
            except (OSError, urllib.error.URLError, ValuationError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(0.4 * attempt)
        raise ValuationError(f"request failed after {self.retries} attempts: {last_error}")


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.text: list[str] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def current_shanghai_time() -> datetime:
    return datetime.now(SHANGHAI_TZ).replace(microsecond=0)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{get_ident()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def month_start(month: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError(f"Invalid month: {month!r}")
    return datetime.strptime(month + "-01", "%Y-%m-%d").date()


def add_months(month: str, offset: int) -> str:
    value = month_start(month)
    ordinal = value.year * 12 + value.month - 1 + offset
    return f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}"


def last_complete_month(as_of: date) -> str:
    return (as_of.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def positive_number(value: Any, label: str) -> float:
    cleaned = re.sub(r"[^0-9.+-]", "", str(value or ""))
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise ValuationError(f"Invalid {label}: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValuationError(f"Invalid {label}: {value!r}")
    return number


def finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValuationError(f"Invalid {label}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValuationError(f"Invalid {label}: {value!r}")
    return number


def parse_nasdaq_history(body: bytes, ticker: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
        rows = payload["data"]["tradesTable"]["rows"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValuationError(f"{ticker} Nasdaq response has an unsupported shape") from exc
    if not isinstance(rows, list) or not rows:
        raise ValuationError(f"{ticker} Nasdaq response has no price rows")
    points: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            trading_date = datetime.strptime(str(row.get("date")), "%m/%d/%Y").date()
            close = positive_number(row.get("close"), f"{ticker} close")
        except (ValueError, ValuationError):
            continue
        points[trading_date.isoformat()] = close
    if not points:
        raise ValuationError(f"{ticker} Nasdaq response has no valid price rows")
    return [{"date": key, "close": points[key]} for key in sorted(points)]


def parse_dqydj_pe(body: bytes) -> list[dict[str, Any]]:
    try:
        document = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValuationError("DQYDJ response is not UTF-8 HTML") from exc
    parser = TableParser()
    parser.feed(document)
    points: dict[str, float] = {}
    for row in parser.rows:
        if len(row) < 2:
            continue
        match = re.fullmatch(r"(0?[1-9]|1[0-2])[-/](\d{4})", row[0].strip())
        if not match:
            continue
        month = f"{match.group(2)}-{int(match.group(1)):02d}"
        candidates = row[3:4] if len(row) >= 4 else row[-1:]
        try:
            points[month] = positive_number(candidates[0], "S&P 500 PE")
        except ValuationError:
            continue
    if len(points) < WINDOW_MONTHS:
        raise ValuationError(
            f"DQYDJ response has only {len(points)} valid monthly PE observations"
        )
    return [{"month": key, "pe_ttm": points[key]} for key in sorted(points)]


def parse_snowball_snapshot(
    body: bytes, expected_codes: set[str]
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
        rows = payload["data"]["items"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValuationError("雪球指数估值响应结构不受支持") from exc
    normalized: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("index_code")) not in expected_codes:
            continue
        code = str(row["index_code"])
        rating_code = str(row.get("eva_type", "")).lower()
        timestamp = int(finite_number(row.get("ts"), f"{code} timestamp"))
        begin_at = int(finite_number(row.get("begin_at"), f"{code} begin timestamp"))
        normalized.append(
            {
                "code": code,
                "name": str(row.get("name") or code),
                "pe_ttm": positive_number(row.get("pe"), f"{code} PE"),
                "pe_percentile_10y": finite_number(
                    row.get("pe_percentile"), f"{code} PE percentile"
                )
                * 100,
                "pb_mrq": positive_number(row.get("pb"), f"{code} PB"),
                "pb_percentile_10y": finite_number(
                    row.get("pb_percentile"), f"{code} PB percentile"
                )
                * 100,
                "roe_pct": finite_number(row.get("roe"), f"{code} ROE") * 100,
                "dividend_yield_pct": finite_number(
                    row.get("yeild"), f"{code} dividend yield"
                )
                * 100,
                "as_of": datetime.fromtimestamp(timestamp / 1000, timezone.utc)
                .date()
                .isoformat(),
                "history_since": datetime.fromtimestamp(begin_at / 1000, timezone.utc)
                .date()
                .isoformat(),
                "source_rating": {
                    "code": rating_code,
                    "label": SOURCE_RATING_LABELS.get(rating_code, rating_code or "未提供"),
                    "provider": "雪球",
                },
            }
        )
    found = {item["code"] for item in normalized}
    if found != expected_codes:
        raise ValuationError(
            f"雪球指数估值缺少白名单项目：{', '.join(sorted(expected_codes - found))}"
        )
    return sorted(normalized, key=lambda item: item["code"])


def _match_text(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        raise ValuationError(f"黄金估值页面缺少{label}")
    return match.group(1)


def parse_gold_snapshot(body: bytes, fetched_on: date) -> dict[str, Any]:
    try:
        document = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValuationError("黄金估值页面不是 UTF-8 HTML") from exc
    document = re.sub(r"<img\b[^>]*>", " ", document, flags=re.IGNORECASE)
    parser = TableParser()
    parser.feed(document)
    text = " ".join(" ".join(parser.text).split())
    updated = _match_text(r"更新于\s*(\d{4}-\d{2}-\d{2})", text, "更新时间")
    spot = positive_number(
        _match_text(r"最新金价\s*([0-9,.]+)\s*美元", text, "最新金价"),
        "gold spot",
    )
    tips = finite_number(_match_text(r"TIPS:\s*([+-]?[0-9.]+)%", text, "TIPS"), "TIPS")
    spread = finite_number(
        _match_text(r"中美利差:\s*([+-]?[0-9.]+)%", text, "中美利差"),
        "China-US spread",
    )
    gold_oil = positive_number(
        _match_text(r"金油比:\s*([0-9.]+)", text, "金油比"), "gold-oil ratio"
    )
    residual = finite_number(
        _match_text(r"当前残差为\s*([+-]?[0-9.]+)", text, "当前残差"),
        "gold residual",
    )
    labels = {
        "1y": "近1年",
        "3y": "近3年",
        "5y": "近5年",
        "10y": "近10年",
        "all": "全部历史",
    }
    percentiles: dict[str, float] = {}
    ratings: dict[str, str] = {}
    for key, source_label in labels.items():
        match = re.search(
            rf"{source_label}\s*([0-9.]+)%\s*([^\s]+)", text, re.IGNORECASE
        )
        if not match:
            raise ValuationError(f"黄金估值页面缺少{source_label}分位")
        percentiles[key] = finite_number(match.group(1), f"gold {key} percentile")
        ratings[key] = match.group(2)

    def factor_date(label: str) -> str:
        return _match_text(rf"{label}\s*(\d{{4}}-\d{{2}}-\d{{2}})", text, label)

    tips_date = factor_date("TIPS 实际利率")
    cn_date = factor_date("中国10年期国债")
    us_date = factor_date("美国10年期国债")
    gold_oil_date = factor_date("金油比")
    spread_date = min(cn_date, us_date)

    def lag_days(value: str) -> int:
        return max(0, (fetched_on - datetime.strptime(value, "%Y-%m-%d").date()).days)

    return {
        "as_of": updated,
        "spot_usd_oz": spot,
        "percentiles": percentiles,
        "source_ratings": ratings,
        "residual": residual,
        "factors": {
            "tips_real_yield": {
                "label": "TIPS 实际利率",
                "value": tips,
                "unit": "%",
                "date": tips_date,
                "lag_days": lag_days(tips_date),
            },
            "china_us_spread": {
                "label": "中美利差",
                "value": spread,
                "unit": "%",
                "date": spread_date,
                "lag_days": lag_days(spread_date),
            },
            "gold_oil_ratio": {
                "label": "金油比",
                "value": gold_oil,
                "unit": "ratio",
                "date": gold_oil_date,
                "lag_days": lag_days(gold_oil_date),
            },
        },
    }


def monthly_average(points: list[dict[str, Any]]) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for point in points:
        try:
            parsed = datetime.strptime(str(point["date"]), "%Y-%m-%d").date()
            close = positive_number(point["close"], "normalized close")
        except (KeyError, ValueError, ValuationError) as exc:
            raise ValuationError(f"Invalid normalized price point: {point!r}") from exc
        grouped.setdefault(parsed.strftime("%Y-%m"), []).append(close)
    return {month: sum(values) / len(values) for month, values in grouped.items()}


def validate_price_points(
    points: list[dict[str, Any]], ticker: str, anchor_months: set[str]
) -> None:
    averages = monthly_average(points)
    if len(averages) < WINDOW_MONTHS:
        raise ValuationError(
            f"{ticker} cache has only {len(averages)} monthly price observations"
        )
    missing = anchor_months - set(averages)
    if missing:
        raise ValuationError(f"{ticker} cache lacks anchor months {sorted(missing)}")


def validate_pe_points(points: list[dict[str, Any]], anchor_months: set[str]) -> None:
    months: set[str] = set()
    for point in points:
        month = str(point.get("month", ""))
        if not re.fullmatch(r"\d{4}-\d{2}", month) or month in months:
            raise ValuationError("PE cache contains an invalid or duplicate month")
        positive_number(point.get("pe_ttm"), "cached S&P 500 PE")
        months.add(month)
    if len(months) < WINDOW_MONTHS or not anchor_months.issubset(months):
        raise ValuationError("PE cache is incomplete or lacks a proxy anchor month")


def merge_price_points(
    cached: list[dict[str, Any]], refreshed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = {str(item["date"]): float(item["close"]) for item in cached}
    merged.update({str(item["date"]): float(item["close"]) for item in refreshed})
    return [{"date": key, "close": merged[key]} for key in sorted(merged)]


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
                if item["ticker"] not in NASDAQ_TICKERS or item["baseline_ticker"] != "SPY":
                    raise ValuationError("Proxy catalog ticker configuration is unsupported")
                month_start(item["anchor"]["month"])
                positive_number(item["anchor"]["pe_ttm"], "proxy anchor PE")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValuationError("Valuation catalog is incomplete") from exc
    return catalog, hashlib.sha256(raw).hexdigest()


def source_cache_id(ticker: str) -> str:
    return f"nasdaq-{ticker.lower()}"


def all_source_ids() -> tuple[str, ...]:
    return (SNOWBALL_SOURCE_ID, GOLD_SOURCE_ID, DQYDJ_SOURCE_ID) + tuple(
        source_cache_id(ticker) for ticker in NASDAQ_TICKERS
    )


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
        if asset["source_mode"] == "proxy"
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
    elif source_id.startswith("nasdaq-"):
        ticker = source_id.removeprefix("nasdaq-").upper()
        required = {
            asset["anchor"]["month"]
            for asset in catalog["assets"]
            if asset["source_mode"] == "proxy"
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


def run_parallel_requests(
    client: HttpClient,
    requests: dict[str, tuple[str, dict[str, str]]],
) -> tuple[dict[str, FetchResponse], dict[str, str], float]:
    responses: dict[str, FetchResponse] = {}
    failures: dict[str, str] = {}
    started = time.perf_counter()
    with ThreadPoolExecutor(
        max_workers=len(requests), thread_name_prefix="valuation-source"
    ) as pool:
        futures = {
            pool.submit(client.fetch, url, headers): source_id
            for source_id, (url, headers) in requests.items()
        }
        for future in as_completed(futures):
            source_id = futures[future]
            try:
                responses[source_id] = future.result()
            except Exception as exc:  # Independently handled with cache or unavailable state.
                failures[source_id] = str(exc)
    return responses, failures, time.perf_counter() - started


def select_contiguous_window(
    target: dict[str, float], baseline: dict[str, float], pe: dict[str, float], through: str
) -> list[str]:
    eligible = sorted(month for month in set(target) & set(baseline) & set(pe) if month <= through)
    for end in reversed(eligible):
        expected = [add_months(end, offset) for offset in range(1 - WINDOW_MONTHS, 1)]
        if all(month in target and month in baseline and month in pe for month in expected):
            return expected
    raise ValuationError(
        f"Sources do not contain {WINDOW_MONTHS} consecutive complete common months"
    )


def percentile_midrank(values: list[float], current: float) -> float:
    below = sum(value < current for value in values)
    equal = sum(value == current for value in values)
    return 100.0 * (below + 0.5 * equal) / len(values)


def quantile(values: list[float], probability: float) -> float:
    if not values or not 0 <= probability <= 1:
        raise ValueError("Invalid quantile input")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def iso_age_hours(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return round(max(0.0, (now - parsed.astimezone(now.tzinfo)).total_seconds() / 3600), 2)


def _parse_response(
    source_id: str,
    response: FetchResponse,
    cached: dict[str, Any] | None,
    catalog: dict[str, Any],
    as_of: date,
    refresh_mode: str,
) -> Any:
    if response.status == 304:
        if cached is None:
            raise ValuationError("source returned 304 without a valid cache")
        return cached["data"]
    if source_id == SNOWBALL_SOURCE_ID:
        expected = {
            asset["source_code"]
            for asset in catalog["assets"]
            if asset["source_mode"] == "direct"
        }
        return parse_snowball_snapshot(response.body, expected)
    if source_id == GOLD_SOURCE_ID:
        return parse_gold_snapshot(response.body, as_of)
    if source_id == DQYDJ_SOURCE_ID:
        return parse_dqydj_pe(response.body)
    ticker = source_id.removeprefix("nasdaq-").upper()
    refreshed = parse_nasdaq_history(response.body, ticker)
    if refresh_mode == "tail":
        if cached is None:
            raise ValuationError(f"{ticker} tail response has no full cache to merge")
        return merge_price_points(cached["data"], refreshed)
    return refreshed


def _source_name(source_id: str, catalog: dict[str, Any]) -> str:
    if source_id.startswith("nasdaq-"):
        return f"Nasdaq {source_id.removeprefix('nasdaq-').upper()} 历史行情"
    return catalog["sources"][source_id]["name"]


def _source_page_url(source_id: str, catalog: dict[str, Any]) -> str:
    if source_id.startswith("nasdaq-"):
        ticker = source_id.removeprefix("nasdaq-")
        return catalog["sources"]["nasdaq"]["page_url"].format(ticker=ticker)
    source = catalog["sources"][source_id]
    return source.get("page_url", source["url"])


def _asset_status(source_ids: list[str], source_states: dict[str, str]) -> str:
    statuses = [source_states.get(source_id, "unavailable") for source_id in source_ids]
    if any(status == "unavailable" for status in statuses):
        return "unavailable"
    if any(status == "cached_stale" for status in statuses):
        return "cached_stale"
    return "fresh"


def _unavailable_asset(config: dict[str, Any], source_ids: list[str], warning: str) -> dict[str, Any]:
    return {
        "id": config["id"],
        "asset_class": config["asset_class"],
        "source_mode": config["source_mode"],
        "name": config["name"],
        "code": config["code"],
        "region": config["region"],
        "frequency": "monthly" if config["source_mode"] == "proxy" else "snapshot",
        "status": "unavailable",
        "as_of": None,
        "current": {},
        "history": [],
        "history_status": "unavailable",
        "method": {"id": config.get("method_id", "snowball_index_eva_snapshot_v1")},
        "source_ids": source_ids,
        "warnings": [warning],
    }


def build_direct_asset(
    config: dict[str, Any], data: Any, source_states: dict[str, str]
) -> dict[str, Any]:
    source_ids = [SNOWBALL_SOURCE_ID]
    status = _asset_status(source_ids, source_states)
    if status == "unavailable" or not isinstance(data, list):
        return _unavailable_asset(config, source_ids, "雪球当前快照不可用且无有效缓存。")
    item = next((row for row in data if row.get("code") == config["source_code"]), None)
    if item is None:
        return _unavailable_asset(config, source_ids, "雪球当前快照缺少该指数。")
    current = {
        key: round(float(item[key]), 4)
        for key in (
            "pe_ttm",
            "pe_percentile_10y",
            "pb_mrq",
            "pb_percentile_10y",
            "roe_pct",
            "dividend_yield_pct",
        )
    }
    current["source_rating"] = item["source_rating"]
    current["history_since"] = item["history_since"]
    return {
        "id": config["id"],
        "asset_class": config["asset_class"],
        "source_mode": config["source_mode"],
        "name": config["name"],
        "code": config["code"],
        "region": config["region"],
        "frequency": "snapshot",
        "status": status,
        "as_of": item["as_of"],
        "current": current,
        "history": [],
        "history_status": "not_provided_by_source",
        "method": {
            "id": "snowball_index_eva_snapshot_v1",
            "label": "雪球指数估值当前快照",
            "limitations": [
                "10 年百分位由雪球提供，本页不重新计算。",
                "来源未提供可发布的历史估值序列，因此不展示曲线。",
            ],
        },
        "source_ids": source_ids,
        "warnings": [],
    }


def build_proxy_asset(
    config: dict[str, Any], data: dict[str, Any], source_states: dict[str, str], through: str
) -> dict[str, Any]:
    target_id = source_cache_id(config["ticker"])
    baseline_id = source_cache_id(config["baseline_ticker"])
    source_ids = [target_id, baseline_id, DQYDJ_SOURCE_ID]
    status = _asset_status(source_ids, source_states)
    if status == "unavailable" or any(data.get(source_id) is None for source_id in source_ids):
        return _unavailable_asset(config, source_ids, "代理模型的行情或盈利数据不可用且无有效缓存。")
    target = monthly_average(data[target_id])
    baseline = monthly_average(data[baseline_id])
    pe = {str(item["month"]): float(item["pe_ttm"]) for item in data[DQYDJ_SOURCE_ID]}
    anchor = config["anchor"]
    anchor_month = anchor["month"]
    try:
        anchor_ratio = target[anchor_month] / baseline[anchor_month]
        calibration = pe[anchor_month] * anchor_ratio / float(anchor["pe_ttm"])
    except (KeyError, ZeroDivisionError) as exc:
        return _unavailable_asset(config, source_ids, f"代理锚点 {anchor_month} 无法复现。")
    if not math.isfinite(calibration) or calibration <= 0:
        return _unavailable_asset(config, source_ids, "代理锚点生成了无效校准常数。")
    try:
        months = select_contiguous_window(target, baseline, pe, through)
    except ValuationError as exc:
        return _unavailable_asset(config, source_ids, str(exc))
    values: list[float] = []
    history: list[dict[str, Any]] = []
    for month in months:
        value = round(pe[month] * (target[month] / baseline[month]) / calibration, 4)
        values.append(value)
        history.append({"month": month, "proxy_pe_ttm": value})
    current_value = values[-1]
    method = {
        "id": config["method_id"],
        "label": f"{config['ticker']}/SPY 相对价格校准代理",
        "formula": (
            f"PE_proxy_m = S&P500_PE_m × ({config['ticker']}_m / SPY_m) / K"
        ),
        "price_aggregation": "calendar_month_mean_close",
        "percentile": "midrank_120_complete_months",
        "quantile": "linear_interpolation_inclusive",
        "definition_url": config["definition_url"],
        "experimental": bool(config.get("experimental", False)),
        "anchor": {
            **anchor,
            "sp500_pe_ttm": round(pe[anchor_month], 6),
            "relative_price_ratio": round(anchor_ratio, 8),
            "calibration_constant": round(calibration, 8),
            "reproduced_pe_ttm": round(
                pe[anchor_month] * anchor_ratio / calibration, 6
            ),
        },
        "limitations": [
            *config["limitations"],
            "结果不构成投资建议，也不输出低估或高估判断。",
        ],
    }
    return {
        "id": config["id"],
        "asset_class": config["asset_class"],
        "source_mode": config["source_mode"],
        "name": config["name"],
        "code": config["code"],
        "region": config["region"],
        "frequency": "monthly",
        "status": status,
        "as_of": months[-1],
        "current": {
            "proxy_pe_ttm": current_value,
            "proxy_percentile_10y": round(
                percentile_midrank(values, current_value), 2
            ),
            "sample_count": len(values),
            "reference_levels": {
                "p30": round(quantile(values, 0.30), 4),
                "p50": round(quantile(values, 0.50), 4),
                "p70": round(quantile(values, 0.70), 4),
            },
        },
        "history": history,
        "history_status": "available",
        "method": method,
        "source_ids": source_ids,
        "warnings": [],
    }


def build_gold_asset(
    config: dict[str, Any],
    data: Any,
    source_states: dict[str, str],
    catalog: dict[str, Any],
    as_of: date,
) -> dict[str, Any]:
    source_ids = [GOLD_SOURCE_ID]
    status = _asset_status(source_ids, source_states)
    if status == "unavailable" or not isinstance(data, dict):
        return _unavailable_asset(config, source_ids, "黄金估值快照不可用且无有效缓存。")
    factors = {
        key: {
            **factor,
            "lag_days": max(
                0,
                (as_of - datetime.strptime(factor["date"], "%Y-%m-%d").date()).days,
            ),
        }
        for key, factor in data["factors"].items()
    }
    current = {
        "spot_usd_oz": round(float(data["spot_usd_oz"]), 4),
        "percentiles": {
            key: round(float(value), 2) for key, value in data["percentiles"].items()
        },
        "source_rating_1y": {
            "label": data["source_ratings"]["1y"],
            "provider": "中美双锚三因子黄金估值模型",
        },
        "source_rating_all": {
            "label": data["source_ratings"]["all"],
            "provider": "中美双锚三因子黄金估值模型",
        },
        "residual": round(float(data["residual"]), 6),
        "factors": factors,
    }
    source = catalog["sources"][GOLD_SOURCE_ID]
    return {
        "id": config["id"],
        "asset_class": config["asset_class"],
        "source_mode": config["source_mode"],
        "name": config["name"],
        "code": config["code"],
        "region": config["region"],
        "frequency": "snapshot",
        "status": status,
        "as_of": data["as_of"],
        "current": current,
        "history": [],
        "history_status": "not_published_by_source",
        "method": {
            "id": config["method_id"],
            "label": "外部中美双锚三因子模型当前快照",
            "attribution_url": source["url"],
            "repository_url": source["repository_url"],
            "limitations": [
                "本页仅转述来源模型的当前数值、分位、评级与因子新鲜度。",
                "不复制来源图表、回测、仓位建议或买卖策略。",
                "来源评级不代表本站判断，结果不构成投资建议。",
            ],
        },
        "source_ids": source_ids,
        "warnings": [],
    }


def aggregate_status(assets: list[dict[str, Any]]) -> str:
    statuses = [asset["status"] for asset in assets]
    if all(status == "fresh" for status in statuses):
        return "fresh"
    if all(status == "unavailable" for status in statuses):
        return "unavailable"
    available = [status for status in statuses if status != "unavailable"]
    if available and all(status == "cached_stale" for status in available) and len(available) == len(statuses):
        return "stale"
    return "partial"


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
    all_nasdaq_cached = all(source_cache_id(ticker) in caches for ticker in NASDAQ_TICKERS)
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
        max(month_start(earliest), years_ago(as_of, 10))
        if refresh_mode == "full"
        else as_of - timedelta(days=TAIL_DAYS)
    )
    sources = catalog["sources"]
    requests: dict[str, tuple[str, dict[str, str]]] = {
        SNOWBALL_SOURCE_ID: (
            sources[SNOWBALL_SOURCE_ID]["url"],
            conditional_headers(SNOWBALL_HEADERS, caches.get(SNOWBALL_SOURCE_ID)),
        ),
        GOLD_SOURCE_ID: (
            sources[GOLD_SOURCE_ID]["url"],
            conditional_headers(GOLD_HEADERS, caches.get(GOLD_SOURCE_ID)),
        ),
        DQYDJ_SOURCE_ID: (sources[DQYDJ_SOURCE_ID]["url"], DQYDJ_HEADERS),
    }
    for ticker in NASDAQ_TICKERS:
        source_id = source_cache_id(ticker)
        requests[source_id] = (
            format_nasdaq_url(ticker, price_start, as_of, sources["nasdaq"]["url"]),
            NASDAQ_HEADERS,
        )
    responses, failures, request_wall_seconds = run_parallel_requests(client, requests)
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
            "bytes": len(response.body) if response else 0,
            "attempts": response.attempts if response else 0,
            "http_status": response.status if response else None,
            "request_mode": request_mode,
            "cache_fallback": cache_fallback,
            "unavailable": source_states[source_id] == "unavailable",
            **({"error": source_error} if source_error else {}),
        }

    assets: list[dict[str, Any]] = []
    for config in catalog["assets"]:
        if config["source_mode"] == "direct":
            asset = build_direct_asset(config, normalized[SNOWBALL_SOURCE_ID], source_states)
        elif config["source_mode"] == "proxy":
            asset = build_proxy_asset(config, normalized, source_states, through)
        else:
            asset = build_gold_asset(
                config, normalized[GOLD_SOURCE_ID], source_states, catalog, as_of
            )
        assets.append(asset)
        warnings.extend(f"{asset['name']}：{item}" for item in asset["warnings"])

    proxy_sources_fresh = all(
        source_states[source_id] == "fresh"
        for source_id in (DQYDJ_SOURCE_ID,)
        + tuple(source_cache_id(ticker) for ticker in NASDAQ_TICKERS)
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


def render_html(payload: dict[str, Any], page_script: str) -> str:
    mode_labels = {"direct": "雪球直取", "proxy": "研究代理", "external_model": "黄金模型"}
    status_labels = {"fresh": "已更新", "cached_stale": "缓存", "unavailable": "暂不可用"}
    rows = []
    for asset in payload["assets"]:
        core, percentile, rating = _summary_values(asset)
        experimental = "<span class=\"tag experimental\">实验</span>" if asset.get("method", {}).get("experimental") else ""
        rows.append(
            f"""<tr class="asset-row" data-asset-id="{html.escape(asset['id'], quote=True)}" data-source-mode="{html.escape(asset['source_mode'], quote=True)}">
              <td><a class="asset-link" data-asset-link="{html.escape(asset['id'], quote=True)}" href="?asset={html.escape(asset['id'], quote=True)}"><strong>{html.escape(asset['name'])}</strong>{experimental}<small>{html.escape(asset['code'])}</small></a></td>
              <td>{html.escape(asset['region'])}</td>
              <td><span class="mode mode-{html.escape(asset['source_mode'])}">{html.escape(mode_labels[asset['source_mode']])}</span></td>
              <td>{html.escape(core)}</td><td>{html.escape(percentile)}</td>
              <td>{html.escape(rating)}{('<small>来源评级</small>' if rating != '--' else '')}</td>
              <td>{html.escape(asset['as_of'] or '--')}</td>
              <td><span class="status status-{html.escape(asset['status'])}">{html.escape(status_labels[asset['status']])}</span><a class="detail-arrow" data-asset-link="{html.escape(asset['id'], quote=True)}" href="?asset={html.escape(asset['id'], quote=True)}" aria-label="查看{html.escape(asset['name'], quote=True)}详情"><span aria-hidden="true">→</span></a></td>
            </tr>"""
        )
    embedded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    warning_text = "；".join(payload["warnings"][:4])
    if len(payload["warnings"]) > 4:
        warning_text += f"；另有 {len(payload['warnings']) - 4} 条详见对应资产"
    banner_class = "" if payload["status"] == "fresh" else " warning"
    banner_title = {
        "fresh": "全部数据已完成本次重验",
        "partial": "部分数据使用缓存或暂不可用",
        "stale": "全部数据来自有效缓存",
        "unavailable": "估值数据暂不可用",
    }[payload["status"]]
    banner_detail = warning_text or "7 个标的均通过来源与结构校验。"
    source_links = "".join(
        f'<a href="{html.escape(source["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(source["name"])}</a>'
        for source in payload["sources"]
        if source["id"] in {SNOWBALL_SOURCE_ID, GOLD_SOURCE_ID, DQYDJ_SOURCE_ID, "nasdaq-spy"}
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>指数与黄金估值研究</title>
  <style>
    :root {{ color-scheme:light; --bg:#f3f5f6; --surface:#fff; --text:#19232c; --muted:#66727d; --border:#d7dde2; --blue:#1d6098; --blue-soft:#eaf2f8; --green:#176653; --green-soft:#e7f2ee; --amber:#8a520d; --amber-soft:#fff3d8; --red:#a33d35; --red-soft:#faeae7; --violet:#765078; --line:#236da7; }}
    * {{ box-sizing:border-box; }} html {{ background:var(--bg); }} [hidden] {{ display:none !important; }}
    body {{ margin:0; min-width:280px; color:var(--text); background:var(--bg); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; font-size:14px; line-height:1.5; letter-spacing:0; overflow-wrap:anywhere; }}
    a {{ color:var(--blue); text-underline-offset:3px; }} button {{ font:inherit; }}
    .page {{ width:min(100%,1180px); margin:0 auto; padding:max(16px,env(safe-area-inset-top)) max(14px,env(safe-area-inset-right)) max(30px,env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-left)); }}
    .route-tabs {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:4px; margin:0 0 18px; padding:4px; border:1px solid var(--border); border-radius:6px; background:#e8ecef; }}
    .route-tab {{ display:grid; min-height:42px; place-content:center; padding:3px; border-radius:4px; color:#42505c; font-size:13px; font-weight:700; text-align:center; text-decoration:none; }}
    .route-tab.current {{ color:var(--text); background:var(--surface); box-shadow:0 1px 2px rgba(20,32,44,.12); }} .route-tab small {{ display:block; color:var(--muted); font-size:10px; }}
    .page-header {{ display:flex; align-items:flex-end; justify-content:space-between; gap:22px; padding:2px 2px 16px; border-bottom:1px solid var(--border); }}
    h1 {{ margin:0; font-size:25px; line-height:1.25; }} .subtitle {{ max-width:720px; margin:7px 0 0; color:#46535e; }} .as-of {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
    .status-banner {{ display:grid; grid-template-columns:auto minmax(0,1fr); gap:8px 14px; margin:14px 0 0; padding:9px 11px; border-left:3px solid var(--green); color:#365248; background:var(--green-soft); font-size:12px; }}
    .status-banner.warning {{ border-left-color:var(--amber); color:#684612; background:var(--amber-soft); }}
    main {{ display:grid; gap:28px; padding-top:22px; }} section {{ min-width:0; }}
    .section-heading {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:10px; }} h2 {{ margin:0; font-size:18px; }} .section-heading span {{ color:var(--muted); font-size:12px; }}
    .filters {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }} .filter-button {{ min-height:34px; padding:5px 11px; border:1px solid var(--border); border-radius:5px; color:#44515c; background:#fff; cursor:pointer; }} .filter-button[aria-pressed="true"] {{ color:#fff; border-color:#344652; background:#344652; }}
    .table-wrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:6px; background:var(--surface); }} table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
    th,td {{ padding:10px 12px; border-bottom:1px solid #e4e8eb; text-align:right; white-space:nowrap; }} th {{ color:#52606c; background:#f7f8f9; font-size:11px; }} th:first-child,td:first-child {{ min-width:170px; text-align:left; }} tr:last-child td {{ border-bottom:0; }}
    .asset-row {{ cursor:pointer; }} .asset-row:hover {{ background:var(--blue-soft); box-shadow:inset 3px 0 var(--blue); }} .asset-row:focus-within {{ background:#f4f8fb; box-shadow:inset 3px 0 var(--blue); }} .asset-row[hidden] {{ display:none; }}
    .asset-link {{ display:block; color:var(--text); text-decoration:none; }} .asset-link:focus-visible,.detail-arrow:focus-visible,.back-link:focus-visible,select:focus-visible {{ outline:2px solid var(--blue); outline-offset:2px; }} .detail-arrow {{ display:inline-grid; width:24px; height:24px; margin-left:7px; place-items:center; border-radius:50%; color:var(--blue); text-decoration:none; font-size:17px; vertical-align:middle; }} .detail-arrow:hover {{ background:#dcebf6; }}
    td small {{ display:block; color:var(--muted); font-size:10px; }} .tag {{ display:inline-block; margin-left:6px; padding:0 4px; border:1px solid #d7b36a; border-radius:3px; color:#76510b; background:#fff7df; font-size:9px; vertical-align:2px; }}
    .mode,.status {{ display:inline-block; font-size:11px; }} .mode-direct {{ color:var(--blue); }} .mode-proxy {{ color:var(--violet); }} .mode-external_model {{ color:#85600c; }} .status-fresh {{ color:var(--green); }} .status-cached_stale {{ color:var(--amber); }} .status-unavailable {{ color:var(--red); }}
    .detail {{ min-height:360px; padding-top:2px; }} .detail-toolbar {{ display:flex; align-items:flex-end; justify-content:space-between; gap:20px; margin-bottom:18px; padding-bottom:14px; border-bottom:1px solid var(--border); }} .back-link {{ display:inline-flex; align-items:center; gap:6px; min-height:36px; color:var(--blue); font-weight:700; text-decoration:none; }} .back-link span {{ font-size:18px; }} .asset-switcher-label {{ display:grid; gap:4px; color:var(--muted); font-size:10px; font-weight:700; }} .asset-switcher-label select {{ min-width:230px; height:36px; padding:4px 32px 4px 9px; border:1px solid #bfc8cf; border-radius:5px; color:var(--text); background:var(--surface); font-size:13px; }} .detail-header {{ display:flex; align-items:flex-end; justify-content:space-between; gap:20px; padding-bottom:13px; border-bottom:1px solid var(--border); }} .detail-title {{ margin:0; font-size:22px; }} .detail-kicker {{ color:var(--muted); font-size:11px; font-weight:700; }} .detail-meta {{ color:var(--muted); font-size:12px; text-align:right; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); margin-top:14px; border-top:1px solid var(--border); border-bottom:1px solid var(--border); background:var(--surface); }} .metric {{ min-width:0; min-height:92px; padding:13px 14px; border-left:3px solid #9aa6af; }} .metric:first-child {{ border-left-color:var(--blue); }} .metric:nth-child(2) {{ border-left-color:var(--green); }} .metric-label {{ display:block; color:var(--muted); font-size:11px; }} .metric-value {{ display:block; margin-top:4px; font-size:24px; line-height:1.2; font-weight:800; font-variant-numeric:tabular-nums; }} .metric-note {{ display:block; margin-top:4px; color:var(--muted); font-size:10px; }}
    .detail-grid {{ display:grid; gap:24px; margin-top:24px; }} .chart-shell {{ position:relative; min-height:300px; border:1px solid var(--border); border-radius:6px; background:var(--surface); overflow:hidden; }} .chart-shell svg {{ display:block; width:100%; aspect-ratio:920/390; min-height:300px; }} .chart-tooltip {{ position:absolute; z-index:2; min-width:112px; padding:7px 9px; border:1px solid #b8c4cf; border-radius:4px; background:rgba(255,255,255,.97); box-shadow:0 2px 8px rgba(20,32,44,.13); pointer-events:none; font-size:12px; }} .chart-tooltip[hidden] {{ display:none; }}
    .chart-legend {{ display:flex; flex-wrap:wrap; gap:7px 18px; margin:8px 2px 0; color:var(--muted); font-size:11px; }} .legend-line {{ display:inline-block; width:18px; margin-right:6px; border-top:2px solid var(--line); vertical-align:middle; }} .legend-line.reference {{ border-top:1px dashed #83909c; }}
    .two-column {{ display:grid; grid-template-columns:minmax(260px,.8fr) minmax(0,1.2fr); gap:24px; }} .panel {{ min-width:0; }} .panel h3 {{ margin:0 0 8px; font-size:14px; }} .compact-table {{ border:1px solid var(--border); border-radius:6px; overflow:auto; background:#fff; }} .compact-table th,.compact-table td {{ padding:8px 10px; }}
    .source-list,.limitations {{ margin:0; padding:0; list-style:none; border-top:1px solid var(--border); }} .source-list li {{ display:grid; grid-template-columns:minmax(0,1fr) auto auto; align-items:center; gap:10px; min-height:50px; border-bottom:1px solid var(--border); }} .source-list small {{ color:var(--muted); }} .limitations li {{ padding:7px 2px 7px 18px; border-bottom:1px solid var(--border); position:relative; }} .limitations li::before {{ content:""; position:absolute; left:2px; top:15px; width:5px; height:5px; border-radius:50%; background:#84919b; }}
    .notice {{ padding:12px 14px; border-left:3px solid var(--amber); color:#624514; background:var(--amber-soft); }} code {{ padding:1px 4px; border-radius:3px; color:#214f70; background:#edf4fa; font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:11px; }}
    .source-directory {{ display:flex; flex-wrap:wrap; gap:8px 18px; }} footer {{ margin-top:28px; padding-top:13px; border-top:1px solid var(--border); color:var(--muted); font-size:11px; }}
    @media(max-width:760px) {{ .page-header,.detail-header,.detail-toolbar {{ align-items:stretch; flex-direction:column; gap:8px; }} .detail-meta {{ text-align:left; }} .asset-switcher-label select {{ width:100%; min-width:0; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .two-column {{ grid-template-columns:1fr; }} }}
    @media(max-width:560px) {{ h1 {{ font-size:22px; }} .route-tab {{ font-size:12px; }} .section-heading {{ align-items:flex-start; flex-direction:column; gap:4px; }} .metric {{ min-height:84px; padding:11px 9px; }} .metric-value {{ font-size:20px; }} .status-banner {{ grid-template-columns:1fr; }} .chart-shell,.chart-shell svg {{ min-height:250px; }} }}
  </style>
</head>
<body>
  <div class="page">
    <nav class="route-tabs" aria-label="页面切换">
      <a class="route-tab" href="../">美国主榜</a><a class="route-tab" href="../?tab=global">全球补充榜</a><a class="route-tab" href="../?tab=premium">场内溢价</a><span class="route-tab current" aria-current="page">估值研究<small>7 个标的</small></span>
    </nav>
    <header class="page-header"><div><h1>指数与黄金估值研究</h1><p class="subtitle">直取来源数据与研究代理分开呈现。代理值不是官方指数 PE，所有数据仅用于研究。</p></div><time class="as-of" datetime="{html.escape(payload['generated_at'], quote=True)}">生成于 {html.escape(payload['generated_at'])}</time></header>
    <div class="status-banner{banner_class}" role="status"><strong>{html.escape(banner_title)}</strong><span>{html.escape(banner_detail)}</span></div>
    <main>
      <section id="valuation-overview" aria-labelledby="overview-heading">
        <div class="section-heading"><h2 id="overview-heading">估值概览</h2><span>来源评级仅转述，不代表本站判断</span></div>
        <div class="filters" role="group" aria-label="数据类型筛选"><button class="filter-button" type="button" data-filter="all" aria-pressed="true">全部</button><button class="filter-button" type="button" data-filter="direct" aria-pressed="false">雪球直取</button><button class="filter-button" type="button" data-filter="proxy" aria-pressed="false">研究代理</button><button class="filter-button" type="button" data-filter="external_model" aria-pressed="false">黄金</button></div>
        <div class="table-wrap"><table id="asset-table"><thead><tr><th>标的</th><th>市场</th><th>来源类型</th><th>核心值</th><th>10 年分位</th><th>来源评级</th><th>数据日期</th><th>状态</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
      </section>
      <section class="detail" id="valuation-detail" aria-live="polite" hidden></section>
    </main>
    <footer><div class="source-directory">{source_links}</div><p>公开产物只包含规范化快照和派生代理序列，不镜像原始响应、图表或策略内容。</p></footer>
  </div>
  <script>window.__INDEX_VALUATION__={embedded};</script>
  <script>{page_script}</script>
</body>
</html>
"""


def build_performance_metrics(
    *, payload: dict[str, Any], request_metrics: dict[str, Any], total_seconds: float
) -> dict[str, Any]:
    total_bytes = sum(int(item.get("bytes", 0)) for item in request_metrics["sources"].values())
    startup = payload["cache"]["startup"]
    warnings: list[str] = []
    seconds_target = PERFORMANCE_TARGETS[f"{startup}_seconds"]
    if total_seconds >= seconds_target:
        warnings.append(
            f"{startup} startup took {total_seconds:.3f}s; target is <{seconds_target:g}s"
        )
    if startup == "hot" and total_bytes >= PERFORMANCE_TARGETS["hot_download_bytes"]:
        warnings.append(
            f"hot startup downloaded {total_bytes} bytes; target is <{PERFORMANCE_TARGETS['hot_download_bytes']}"
        )
    return {
        "schema_version": 2,
        "status": "success",
        "generated_at": payload["generated_at"],
        "asset_status": payload["status"],
        "total_seconds": round(total_seconds, 3),
        "request_wall_seconds": request_metrics["request_wall_seconds"],
        "parse_seconds": request_metrics["parse_seconds"],
        "total_download_bytes": total_bytes,
        "startup": startup,
        "refresh_mode": payload["cache"]["refresh_mode"],
        "cache_hit": payload["cache"]["hit"],
        "fallback": payload["cache"]["fallback"],
        "sources": request_metrics["sources"],
        "targets": PERFORMANCE_TARGETS,
        "warnings": warnings,
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--publish-dir", type=Path, default=DEFAULT_PUBLISH_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--page-script", type=Path, default=DEFAULT_PAGE_SCRIPT)
    parser.add_argument("--as-of", help="Evaluation date in YYYY-MM-DD format")
    args = parser.parse_args(argv)
    if args.as_of:
        try:
            datetime.strptime(args.as_of, "%Y-%m-%d")
        except ValueError:
            parser.error("--as-of must use YYYY-MM-DD")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.perf_counter()
    output_dir = args.output_dir.resolve()
    publish_dir = args.publish_dir.resolve()
    cache_dir = args.cache_dir.resolve()
    try:
        as_of = (
            datetime.strptime(args.as_of, "%Y-%m-%d").date()
            if args.as_of
            else current_shanghai_time().date()
        )
        now = current_shanghai_time()
        catalog, catalog_hash = load_catalog(args.catalog.resolve())
        fingerprint = cache_fingerprint(catalog, catalog_hash)
        caches, cache_states = load_source_caches(cache_dir, catalog, catalog_hash, as_of)
        manifest, manifest_state = load_manifest(cache_dir, fingerprint)
        payload, new_caches, new_manifest, request_metrics = build_payload(
            as_of=as_of,
            now=now,
            catalog=catalog,
            catalog_hash=catalog_hash,
            caches=caches,
            cache_states=cache_states,
            manifest=manifest,
            manifest_state=manifest_state,
            client=HttpClient(),
        )
        if payload["status"] == "unavailable":
            raise ValuationError("All valuation assets are unavailable")
        script = args.page_script.resolve().read_text(encoding="utf-8")
        document = render_html(payload, script)
        for source_id, cached in new_caches.items():
            atomic_write_json(cache_dir / f"{source_id}.json", cached)
        atomic_write_json(cache_dir / "manifest.json", new_manifest)
        atomic_write_json(output_dir / "latest.json", payload)
        atomic_write_text(output_dir / "latest.html", document)
        atomic_write_text(publish_dir / "valuation" / "index.html", document)
        metrics = build_performance_metrics(
            payload=payload,
            request_metrics=request_metrics,
            total_seconds=time.perf_counter() - started,
        )
        atomic_write_json(output_dir / "run-metrics.json", metrics)
        for warning in metrics["warnings"]:
            print(f"::warning title=Index valuation performance::{warning}", file=sys.stderr)
        for warning in payload["warnings"]:
            print(f"SOURCE WARNING: {warning}", file=sys.stderr)
    except (OSError, ValueError, ValuationError) as exc:
        failure = {
            "schema_version": 2,
            "status": "failure",
            "generated_at": current_shanghai_time().isoformat(),
            "total_seconds": round(time.perf_counter() - started, 3),
            "error": str(exc),
        }
        try:
            atomic_write_json(output_dir / "run-metrics.json", failure)
        except OSError:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    available = sum(asset["status"] != "unavailable" for asset in payload["assets"])
    print(
        f"Generated {available}/{len(payload['assets'])} valuation assets "
        f"with status {payload['status']} in {metrics['total_seconds']:.3f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
