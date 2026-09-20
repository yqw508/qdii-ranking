#!/usr/bin/env python3
"""Build an on-demand ranking of purchasable RMB A-class QDII funds."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, get_ident
from typing import Any, Iterable

from qdii_ranking.config import (
    ANNOUNCEMENT_API_URL,
    ANNOUNCEMENT_INDEX_CACHE_SCHEMA_VERSION,
    ANNOUNCEMENT_PDF_URL,
    BENCHMARK_CACHE_SCHEMA_VERSION,
    BENCHMARK_HISTORY_BUFFER_DAYS,
    BENCHMARK_MAX_STALENESS_DAYS,
    BENCHMARK_WINDOW_YEARS,
    CONTRACT_RESULT_CACHE_SCHEMA_VERSION,
    CONTRACT_RESULT_METHOD_VERSION,
    DEFAULT_CONTRACT_BENCHMARK_CATALOG,
    DEFAULT_EXCLUDE_KEYWORDS,
    DEFAULT_MIN_DIRECT_LIMIT_CNY,
    DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
    DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
    DEFAULT_MIN_THREE_YEAR_RETURN_PCT,
    DEFAULT_US_EQUITY_CATALOG,
    DEFAULT_US_EQUITY_ETF_CATALOG,
    DOCUMENT_WORKERS,
    ETF_HOLDING_COST_CACHE_SCHEMA_VERSION,
    ETF_HOLDING_COST_METHOD_VERSION,
    ETF_LOF_NAV_API_URL,
    ETF_MARKET_LIST_API_URL,
    ETF_MARKET_LIST_FS,
    ETF_MARKET_LIST_PAGE_SIZE,
    ETF_PREMIUM_CACHE_SCHEMA_VERSION,
    ETF_PREMIUM_CATALOG_SCHEMA_VERSION,
    ETF_PREMIUM_DELAY_MINUTES,
    ETF_PREMIUM_GROUP_ORDER,
    ETF_QUOTE_API_URL,
    ETF_QUOTE_PAGE_URL,
    EXCLUDED_FUND_TYPES,
    FUND_EXPOSURE_CACHE_SCHEMA_VERSION,
    FUND_LIST_URL,
    FUND_PAGE_URL,
    HOLDER_API_URL,
    NASDAQ100_HISTORY_DATA_URL,
    NASDAQ100_HISTORY_PAGE_URL,
    NASDAQ100_MIN_OBSERVATIONS,
    NASDAQ100_MIN_SPAN_DAYS,
    NASDAQ100_OTC_NAME_RE,
    NASDAQ100_TWO_YEAR_MIN_OBSERVATIONS,
    NASDAQ100_TWO_YEAR_MIN_SPAN_DAYS,
    NASDAQ100_TWO_YEAR_WINDOW_YEARS,
    NOTICE_TITLE_RE,
    PERFORMANCE_CACHE_SCHEMA_VERSION,
    PERFORMANCE_DATA_URL,
    PERFORMANCE_WORKERS,
    QUOTA_NOTICE_CACHE_SCHEMA_VERSION,
    QUOTA_NOTICE_METHOD_VERSION,
    RANKING_SCHEMA_VERSION,
    REPORT_TITLE_EXCLUDE_RE,
    ROUTING_REASON_BELOW_US_THRESHOLD,
    ROUTING_REASON_CONFIRMED_US,
    ROUTING_REASON_GEOGRAPHY_OVERRIDE,
    ROUTING_REASON_LABELS,
    SAFE_USD_CNY_HISTORY_URL,
    THREE_YEAR_BOUNDARY_TOLERANCE_DAYS,
    USER_AGENT,
    US_EQUITY_METHOD_VERSION,
)
from qdii_ranking.pipeline import (
    RunMemo,
    apply_quota_gate,
    evaluate_batch,
    rank_candidates,
    route_candidates,
)
from qdii_ranking.errors import DataError
from qdii_ranking.models import (
    AnnouncementRecord,
    FundAnnouncementSnapshot,
    HolderPeriod,
    LegalDocument,
    Nasdaq100Benchmark,
    PeriodicReport,
)
from qdii_ranking.cache.performance import PerformanceResultCache
from qdii_ranking.cache.premium import ExchangePremiumHoldingCostCache
from qdii_ranking.cache.quota import QuotaNoticeParseCache
from qdii_ranking.cache.contracts import ContractProfileResultCache
from qdii_ranking.cache.exposure import FundExposureResultCache
from qdii_ranking.cache.announcements import AnnouncementIndexCache, PeriodicReportCache
from qdii_ranking.cache.benchmark import Nasdaq100BenchmarkCache
from qdii_ranking.renderers.json_renderer import render_json
from qdii_ranking.renderers.csv_renderer import render_csv
from qdii_ranking.renderers.markdown_renderer import render_markdown
from qdii_ranking.renderers.html_renderer import render_html
from qdii_ranking.renderers.common import (
    benchmark_display,
    format_beta,
    format_correlation,
    format_holding_cost,
    format_limit,
    format_long_return,
    format_optional_percentage,
    format_percentage,
    format_rule,
    summarize_periods,
)
from qdii_ranking.transport import HttpTransport
from qdii_ranking.sources.candidates import (
    build_nasdaq100_otc_candidates as _build_nasdaq100_otc_candidates,
    is_nasdaq100_otc_name as _is_nasdaq100_otc_name,
    is_otc_share as _is_otc_share,
    is_rmb_a_share as _is_rmb_a_share,
)
from qdii_ranking.sources.benchmark import (
    fetch_nasdaq100_history,
    fetch_safe_usd_cny_history,
    parse_nasdaq100_history,
    parse_safe_usd_cny_history,
)
from qdii_ranking.sources.performance import (
    adjusted_daily_factor,
    build_adjusted_wealth_series,
    calculate_nasdaq100_fit,
    calculate_performance_from_points,
    calculate_trailing_performance,
    fetch_trailing_performance,
    latest_series_value,
    parse_performance_page,
)
from qdii_ranking.sources.fund import (
    amount_to_billion,
    enrich_fund_pages,
    extract_data_array,
    fetch_fund_metadata,
    is_qdii_fund_metadata,
    parse_fund_page,
    strip_tags,
)
from qdii_ranking.sources.holder import (
    extract_page_count,
    fetch_holder_periods,
    fetch_holder_rows,
    period_key,
    select_holder_period,
)
from qdii_ranking.sources.premium import (
    _parse_exchange_premium_market_page,
    exchange_premium_lof_nav_url,
    exchange_premium_market_url as _exchange_premium_market_url,
    fetch_exchange_premium_lof_navs,
    load_qdii_exchange_premium_catalog as _load_qdii_exchange_premium_catalog,
    parse_exchange_premium_lof_nav,
)
from qdii_ranking.sources.announcements import (
    _announcement_has_legal_pair,
    _announcement_page,
    _is_rmb_product_summary,
    _parse_announcement_page,
    fetch_announcements,
    fetch_latest_periodic_report,
    parse_periodic_report_date,
)

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - exercised by the runtime dependency check
    raise SystemExit(
        "Missing dependency: pypdf. Run this script with the Codex bundled Python runtime."
    ) from exc


SHANGHAI_TZ = timezone(timedelta(hours=8))
DIRECT_CHANNEL_PATTERN = (
    r"(?<!非)直销销售机构|(?<!非)直销机构|直销渠道|直销中心柜台|电子直销平台|网上直销平台"
)
AGENCY_CHANNEL_PATTERN = r"非直销销售机构|代销机构|代销渠道"


class RunMetrics:
    def __init__(self) -> None:
        self.phase_seconds: dict[str, float] = {}
        self.counters: dict[str, int] = {}
        self._lock = Lock()

    @contextmanager
    def phase(self, name: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            with self._lock:
                self.phase_seconds[name] = round(
                    self.phase_seconds.get(name, 0.0) + elapsed, 3
                )

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + amount

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "phase_seconds": dict(self.phase_seconds),
                "counters": dict(self.counters),
            }


def _http_category(url: str) -> str:
    if "pingzhongdata" in url:
        return "nav_history"
    if url.startswith(ANNOUNCEMENT_API_URL):
        return "announcement_index"
    if url.startswith("https://pdf.dfcfw.com/"):
        return "announcement_pdf"
    if url.startswith(HOLDER_API_URL):
        return "holder_data"
    if "indexes.nasdaq.com" in url or "safe.gov.cn" in url:
        return "benchmark"
    if url.startswith((ETF_QUOTE_API_URL, ETF_MARKET_LIST_API_URL, ETF_LOF_NAV_API_URL)):
        return "exchange_premium"
    if url.startswith("https://fund.eastmoney.com/") and url.endswith(".html"):
        return "fund_page"
    if url == FUND_LIST_URL:
        return "fund_list"
    return "other"


class HttpClient(HttpTransport):
    """Compatibility name backed by the shared transport implementation."""

    _category = staticmethod(_http_category)

    def __init__(self, retries: int = 4, timeout: int = 30) -> None:
        super().__init__(
            retries=retries,
            timeout=timeout,
            user_agent=USER_AGENT,
            category_resolver=_http_category,
            error_type=DataError,
        )

    def metrics(self) -> dict[str, dict[str, float | int]]:
        return self.metrics_snapshot()




def parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def current_shanghai_date() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def is_older_than_years(inception_date: str, as_of: date, years: int) -> bool:
    return parse_date(inception_date) < years_ago(as_of, years)






























def exchange_premium_market_url(
    page: int = 1, page_size: int | None = None
) -> str:
    return _exchange_premium_market_url(
        page,
        ETF_MARKET_LIST_PAGE_SIZE if page_size is None else page_size,
    )


def load_qdii_exchange_premium_catalog(
    client: HttpClient, metadata: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    return _load_qdii_exchange_premium_catalog(
        client, metadata, page_size=ETF_MARKET_LIST_PAGE_SIZE
    )


def is_rmb_a_share(meta: dict[str, str]) -> bool:
    return _is_rmb_a_share(meta)


def is_otc_share(meta: dict[str, str]) -> bool:
    return _is_otc_share(meta)


def is_nasdaq100_otc_name(name: str) -> bool:
    return _is_nasdaq100_otc_name(name)


def contract_mentions_nasdaq100(profile: dict[str, Any]) -> bool:
    searchable = json.dumps(profile, ensure_ascii=False)
    return bool(NASDAQ100_OTC_NAME_RE.search(searchable))


def build_nasdaq100_otc_candidates(
    metadata: dict[str, dict[str, str]],
    holder_rows: list[list[str]],
) -> list[dict[str, Any]]:
    """Compatibility wrapper for the extracted candidate source adapter."""
    return _build_nasdaq100_otc_candidates(metadata, holder_rows)


def build_holder_candidates(
    rows: list[list[str]], metadata: dict[str, dict[str, str]], keywords: list[str]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 6 or row[0] not in metadata or not row[2]:
            continue
        meta = metadata[row[0]]
        if not is_otc_share(meta):
            continue
        if meta["fund_type"] in EXCLUDED_FUND_TYPES:
            continue
        if any(keyword and keyword in meta["name"] for keyword in keywords):
            continue
        try:
            ratio = float(row[2])
        except ValueError:
            continue
        candidates.append(
            {
                **meta,
                "institution_holding_ratio_pct": ratio,
                "personal_holding_ratio_pct": float(row[3]) if row[3] else None,
                "holder_total_shares_100m": float(row[5].replace(",", "")) if row[5] else None,
            }
        )
    candidates.sort(key=lambda item: (-item["institution_holding_ratio_pct"], item["code"]))
    return candidates










def filter_and_rank(
    candidates: list[dict[str, Any]],
    min_scale: float | None,
    top: int,
    exclude_keywords: Iterable[str] = (),
    as_of: date | None = None,
    min_age_years: int = 0,
) -> list[dict[str, Any]]:
    eligible = [
        item
        for item in candidates
        if (min_scale is None or item["scale_billion_cny"] > min_scale)
        and item["purchase_status"] in {"open", "limited"}
        and (
            min_age_years == 0
            or (
                as_of is not None
                and is_older_than_years(item["inception_date"], as_of, min_age_years)
            )
        )
        and not any(
            keyword and keyword in item["name"] for keyword in exclude_keywords
        )
    ]
    eligible.sort(key=lambda item: (-item["institution_holding_ratio_pct"], item["code"]))
    return eligible[:top]










def filter_performance_full_scan(
    client: HttpClient,
    candidates: list[dict[str, Any]],
    as_of: date,
    min_three_year_return_pct: float,
    top: int,
    min_five_year_return_pct: float = DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
    min_ten_year_return_pct: float = DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
) -> tuple[list[dict[str, Any]], list[str], int]:
    selected: list[dict[str, Any]] = []
    warnings: list[str] = []
    scanned = 0
    for fund in candidates:
        scanned += 1
        performance, performance_warnings = fetch_trailing_performance(client, fund, as_of)
        warnings.extend(performance_warnings)
        if performance_threshold_failures(
            performance,
            min_three_year_return_pct,
            min_five_year_return_pct,
            min_ten_year_return_pct,
        ):
            continue
        selected.append({**fund, **performance})
    return selected[:top], warnings, scanned


def filter_performance_and_us_exposure_full_scan(
    client: HttpClient,
    candidates: list[dict[str, Any]],
    as_of: date,
    min_three_year_return_pct: float,
    min_us_equity_pct: float,
    top: int,
    report_cache: PeriodicReportCache,
    resolver: LookthroughResolver,
    performance_cache: PerformanceResultCache | None = None,
    exposure_cache: FundExposureResultCache | None = None,
    performance_workers: int = PERFORMANCE_WORKERS,
    benchmark: Nasdaq100Benchmark | None = None,
    min_five_year_return_pct: float = DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
    min_ten_year_return_pct: float = DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
) -> tuple[list[dict[str, Any]], list[str], int, int, int, int]:
    warnings: list[str] = []
    performance_results: list[tuple[dict[str, Any], list[str]] | None] = [
        None
    ] * len(candidates)

    def evaluate_performance(
        fund: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        if performance_cache is not None:
            if benchmark is None:
                raise DataError("Nasdaq-100 benchmark is required with the performance cache")
            return performance_cache.get(client, fund, as_of, benchmark)
        return fetch_trailing_performance(client, fund, as_of, benchmark)

    if candidates:
        with ThreadPoolExecutor(
            max_workers=min(max(1, performance_workers), len(candidates))
        ) as executor:
            futures = {
                executor.submit(evaluate_performance, fund): index
                for index, fund in enumerate(candidates)
            }
            for future in as_completed(futures):
                performance_results[futures[future]] = future.result()

    performance_qualified: list[dict[str, Any]] = []
    for fund, result in zip(candidates, performance_results):
        if result is None:
            raise DataError(f"Performance was not evaluated for fund {fund['code']}")
        performance, performance_warnings = result
        warnings.extend(performance_warnings)
        if performance_threshold_failures(
            performance,
            min_three_year_return_pct,
            min_five_year_return_pct,
            min_ten_year_return_pct,
        ):
            continue
        performance_qualified.append({**fund, **performance})

    exposure_qualified: list[dict[str, Any]] = []
    for fund in performance_qualified:
        exposure, exposure_warnings = fetch_us_equity_exposure(
            client,
            fund,
            as_of,
            report_cache,
            resolver,
            min_us_equity_pct,
            exposure_cache,
        )
        warnings.extend(f"{fund['code']} {warning}" for warning in exposure_warnings)
        if exposure["status"] != "qualified":
            continue
        if not isinstance(fund.get("nasdaq100_fit"), dict):
            detail = fund.get("nasdaq100_fit_error") or "unknown calculation error"
            raise DataError(
                f"Nasdaq-100 fit is unavailable for qualified fund {fund['code']}: {detail}"
            )
        exposure_qualified.append(
            {**fund, "us_equity_exposure": exposure}
        )
    exposure_qualified.sort(
        key=lambda item: (
            -item["nasdaq100_fit"]["correlation"],
            abs(item["nasdaq100_fit"]["beta"] - 1),
            -item["us_equity_exposure"]["confirmed_pct"],
            -item.get("institution_holding_ratio_pct", 0),
            -item["three_year_return_pct"],
            item["code"],
        )
    )
    return (
        exposure_qualified[:top],
        warnings,
        len(candidates),
        len(performance_qualified),
        len(performance_qualified),
        len(exposure_qualified),
    )


def performance_threshold_failures(
    performance: dict[str, Any],
    min_three_year_return_pct: float,
    min_five_year_return_pct: float,
    min_ten_year_return_pct: float,
) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    three_year = performance.get("three_year_return_pct")
    five_year = performance.get("five_year_return_pct")
    ten_year = performance.get("ten_year_return_pct")
    if three_year is None or float(three_year) < min_three_year_return_pct:
        failures.append(
            (
                "three_year_return_below_threshold",
                f"近三年收益低于 {min_three_year_return_pct:g}%",
            )
        )
    if five_year is not None and float(five_year) < min_five_year_return_pct:
        failures.append(
            (
                "five_year_return_below_threshold",
                f"有完整五年历史且近五年收益低于 {min_five_year_return_pct:g}%",
            )
        )
    if ten_year is not None and float(ten_year) < min_ten_year_return_pct:
        failures.append(
            (
                "ten_year_return_below_threshold",
                f"有完整十年历史且近十年收益低于 {min_ten_year_return_pct:g}%",
            )
        )
    return failures


def evaluate_performance_full_scan(
    client: HttpClient,
    candidates: list[dict[str, Any]],
    as_of: date,
    min_three_year_return_pct: float,
    performance_cache: PerformanceResultCache,
    benchmark: Nasdaq100Benchmark,
    min_five_year_return_pct: float = DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
    min_ten_year_return_pct: float = DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
    performance_workers: int = PERFORMANCE_WORKERS,
    run_cache: dict[str, tuple[dict[str, Any], list[str]]] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[str],
    int,
    dict[str, list[tuple[str, str]]],
]:
    results: list[tuple[dict[str, Any], list[str]] | None] = [None] * len(candidates)

    def evaluate(fund: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        if run_cache is not None and fund["code"] in run_cache:
            return run_cache[fund["code"]]
        result = performance_cache.get(client, fund, as_of, benchmark)
        if run_cache is not None:
            run_cache[fund["code"]] = result
        return result

    if candidates:
        with ThreadPoolExecutor(
            max_workers=min(max(1, performance_workers), len(candidates))
        ) as executor:
            futures = {
                executor.submit(evaluate, fund): index
                for index, fund in enumerate(candidates)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()

    qualified: list[dict[str, Any]] = []
    rejected: dict[str, list[tuple[str, str]]] = {}
    warnings: list[str] = []
    for fund, result in zip(candidates, results):
        if result is None:
            raise DataError(f"Performance was not evaluated for fund {fund['code']}")
        performance, performance_warnings = result
        warnings.extend(performance_warnings)
        failures = performance_threshold_failures(
            performance,
            min_three_year_return_pct,
            min_five_year_return_pct,
            min_ten_year_return_pct,
        )
        if failures:
            rejected[fund["code"]] = failures
            continue
        qualified.append({**fund, **performance})
    return qualified, warnings, len(candidates), rejected


def direct_limit_qualifies(limit: dict[str, Any], threshold_cny: int) -> bool:
    if limit.get("status") == "unlimited":
        return True
    return (
        limit.get("status") == "limited"
        and isinstance(limit.get("amount_cny"), int)
        and int(limit["amount_cny"]) >= threshold_cny
    )


def calculate_return_drawdown_ratio(record: dict[str, Any]) -> tuple[float | None, float]:
    start = parse_date(str(record["three_year_performance_start_date"]))
    end = parse_date(str(record["three_year_performance_end_date"]))
    span_days = (end - start).days
    if span_days <= 0:
        raise DataError(f"Invalid three-year performance span for {record['code']}")
    total_return = float(record["three_year_return_pct"]) / 100
    if total_return <= -1:
        raise DataError(f"Invalid three-year return for {record['code']}")
    annualized = ((1 + total_return) ** (365 / span_days) - 1) * 100
    drawdown = abs(float(record["three_year_max_drawdown_pct"]))
    score = None if drawdown == 0 else annualized / drawdown
    return score, annualized


def us_main_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -float(item["nasdaq100_fit"]["correlation"]),
        abs(float(item["nasdaq100_fit"]["beta"]) - 1),
        -float(item["us_equity_exposure"]["confirmed_pct"]),
        -float(item.get("institution_holding_ratio_pct", 0)),
        -float(item["three_year_return_pct"]),
        item["code"],
    )


def global_supplement_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    score = item.get("_return_drawdown_ratio")
    score_key = float("-inf") if score is None else -round(float(score), 4)
    return (
        score_key,
        -float(item["three_year_return_pct"]),
        -float(item["three_year_max_drawdown_pct"]),
        -float(item.get("institution_holding_ratio_pct", 0)),
        -float(item["scale_billion_cny"]),
        item["code"],
    )


def nasdaq100_otc_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Sort complete name-matched display records, placing unavailable values last."""
    return (
        float("inf") if item.get("two_year_return_pct") is None else -float(item["two_year_return_pct"]),
        float("inf")
        if item.get("holding_cost", {}).get("annualized_pct") is None
        else float(item["holding_cost"]["annualized_pct"]),
        float("inf")
        if not isinstance(item.get("nasdaq100_fit_2y"), dict)
        else float(item["nasdaq100_fit_2y"].get("tracking_error_pct", float("inf"))),
        float("inf") if item.get("three_year_return_pct") is None else -float(item["three_year_return_pct"]),
        float("inf") if item.get("scale_billion_cny") is None else -float(item["scale_billion_cny"]),
        item["code"],
    )


def ranking_list_for_exposure(
    exposure: dict[str, Any], threshold_pct: float
) -> str:
    return (
        "us_main"
        if float(exposure["confirmed_pct"]) >= threshold_pct
        else "global_supplement"
    )


def ranking_route(
    name: str,
    exposure: dict[str, Any],
    threshold_pct: float,
    us_main_exclude_keywords: Iterable[str],
) -> tuple[str, str]:
    if any(keyword and keyword in name for keyword in us_main_exclude_keywords):
        return "global_supplement", ROUTING_REASON_GEOGRAPHY_OVERRIDE
    if float(exposure["confirmed_pct"]) >= threshold_pct:
        return "us_main", ROUTING_REASON_CONFIRMED_US
    return "global_supplement", ROUTING_REASON_BELOW_US_THRESHOLD


def routing_reason_label(reason: str) -> str:
    try:
        return ROUTING_REASON_LABELS[reason]
    except KeyError as exc:
        raise DataError(f"Unknown ranking routing reason: {reason}") from exc


def normalize_notice_text(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Some announcement PDFs extract every Chinese character and numeric
    # punctuation as separate text runs. Rejoin those runs before matching.
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"(?<=\d)\s*([,.])\s*(?=\d)", r"\1", text)


def parse_cny_amount(value: str, unit: str) -> int:
    number = float(value.replace(",", ""))
    if unit == "万元":
        number *= 10000
    return int(round(number))


def find_amounts(text: str) -> list[tuple[int, int, str]]:
    results: list[tuple[int, int, str]] = []
    for match in re.finditer(r"(?<![\d-])([\d][\d,.]*)\s*(万元|元)", text):
        try:
            results.append((match.start(), parse_cny_amount(match.group(1), match.group(2)), match.group(0)))
        except ValueError:
            continue
    return results


def extract_channel_amount(text: str, channel_pattern: str) -> int | None:
    best: tuple[int, int] | None = None
    for channel_match in re.finditer(channel_pattern, text):
        clause = re.split(r"[。；]", text[channel_match.end() :], maxsplit=1)[0]
        explicit = re.search(
            r"(?:不得超过|不超过|高于|超过|限额(?:调整)?为)\s*"
            r"([\d][\d,.]*)\s*(万元|元)",
            clause,
        )
        if explicit:
            return parse_cny_amount(explicit.group(1), explicit.group(2))
        above = re.search(r"([\d][\d,.]*)\s*(万元|元)\s*以上", clause)
        if above:
            return parse_cny_amount(above.group(1), above.group(2))
        start = max(0, channel_match.start() - 80)
        end = min(len(text), channel_match.end() + 220)
        window = text[start:end]
        for amount_pos, amount, _ in find_amounts(window):
            absolute_pos = start + amount_pos
            distance = abs(absolute_pos - channel_match.end())
            context_start = max(0, amount_pos - 55)
            context_end = min(len(window), amount_pos + 55)
            context = window[context_start:context_end]
            if not re.search(r"上限|超过|不得超过|不超过|限制|暂停办理|累计申购", context):
                continue
            candidate = (distance, amount)
            if best is None or candidate[0] < best[0]:
                best = candidate
    return best[1] if best else None


def extract_global_amount(text: str) -> int | None:
    patterns = [
        r"限制\s*(?:大额\s*)?申购\s*(?:及\s*定期定额投资\s*)?金额\s*"
        r"(?:[（(]\s*单\s*位\s*[：:]\s*(?:人民币\s*)?元\s*[）)])?\s*([\d,.]+)",
        r"调整\s*申购\s*(?:[（(]\s*含\s*定期定额投资\s*[）)])?\s*金额\s*"
        r"(?:[（(]\s*单\s*位\s*[：:]\s*人民币\s*元\s*[）)])?\s*([\d,.]+)",
        r"(?:累计申购|申购金额)[^。；]{0,180}?(?:不超过|不得超过|不应超过|上限调整为)\s*"
        r"(?:人民币\s*)?([\d,.]+)\s*(?:人民币\s*)?(万元|元)",
        r"超过\s*([\d,.]+)\s*(万元|元)[^。；]{0,50}?(?:申购|大额申购)",
        r"(?:金额累计限额|业务限额)为\s*([\d,.]+)\s*(?:人民币\s*)?(万元|元)",
    ]
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue
        if index < 2:
            return int(round(float(match.group(1).replace(",", ""))))
        return parse_cny_amount(match.group(1), match.group(2))
    return None


def extract_effective_date(text: str, published: date) -> date:
    patterns = [
        r"(?:调整|暂停|恢复)[^。；]{0,30}?起始日\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:起|（含)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return published


def detect_share_aggregation(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if re.search(
        r"A[类级].{0,60}C[类级].{0,35}(?:合并计算|合计金额)|A类、C类.{0,30}合计金额",
        compact,
    ):
        return "A/C combined"
    if re.search(
        r"分别计算|分开计算|单独计算(?:限额)?|单一基金份额|单一类别|A类人民币份额或C类人民币份额",
        compact,
    ):
        return "A/C separate"
    return None


def extract_future_transitions(
    text: str, source_url: str, published: date
) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    date_pattern = re.compile(
        r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:起|（含[^）]*）)"
    )
    matches = list(date_pattern.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 500)
        clause = text[match.start():end]
        clause = re.split(r"[。；]", clause, maxsplit=1)[0]
        if not re.search(r"恢复|调整|暂停", clause):
            continue
        amount = extract_global_amount(clause)
        if amount is None:
            continue
        transitions.append(
            {
                "effective_date": date(int(match.group(1)), int(match.group(2)), int(match.group(3))),
                "direct_amount_cny": extract_channel_amount(
                    clause, DIRECT_CHANNEL_PATTERN
                ),
                "agency_amount_cny": extract_channel_amount(clause, AGENCY_CHANNEL_PATTERN),
                "global_amount_cny": amount,
                "source_url": source_url,
                "published_date": published.isoformat(),
                "share_aggregation": detect_share_aggregation(clause),
                "all_channels_combined": "全部销售机构累计" in re.sub(r"\s+", "", clause),
                "confidence": "high",
            }
        )
    return transitions


def parse_quota_notice(
    text: str, published: date, source_url: str
) -> list[dict[str, Any]]:
    normalized = normalize_notice_text(text)
    direct = extract_channel_amount(
        normalized, DIRECT_CHANNEL_PATTERN
    )
    agency = extract_channel_amount(normalized, AGENCY_CHANNEL_PATTERN)
    global_amount = extract_global_amount(normalized)
    compact = re.sub(r"\s+", "", normalized)
    if direct is None and agency is None and global_amount is None:
        future_restore = re.search(
            r"恢复(?:办理)?大额申购.{0,40}(?:具体时间|时间).{0,20}另行公告",
            compact,
        )
        if (
            re.search(r"恢复(?:办理)?大额申购", compact)
            and not re.search(r"暂停接受.*?超过", compact)
            and not future_restore
        ):
            global_status = "unlimited"
        else:
            return []
    else:
        global_status = "limited"
    base = {
        "effective_date": extract_effective_date(normalized, published),
        "direct_amount_cny": direct,
        "agency_amount_cny": agency,
        "global_amount_cny": global_amount,
        "global_status": global_status,
        "source_url": source_url,
        "published_date": published.isoformat(),
        "share_aggregation": detect_share_aggregation(normalized),
        "all_channels_combined": bool(
            "全部销售机构累计" in compact
            or re.search(r"多家销售渠道.{0,60}累计计算", compact)
        ),
        "confidence": "high" if global_amount is not None or (direct is not None and agency is not None) else "medium",
    }
    transitions = [base]
    for transition in extract_future_transitions(normalized, source_url, published):
        if transition["effective_date"] != base["effective_date"]:
            transitions.append(transition)
    return transitions


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf exposes several parser-specific exceptions
        raise DataError(f"Could not extract announcement PDF text: {exc}") from exc








def normalize_benchmark_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]+", "", value.upper())


class ContractBenchmarkCatalog:
    def __init__(self, path: Path) -> None:
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"Could not load contract benchmark catalog: {exc}") from exc
        if payload.get("schema_version") != 1 or not isinstance(payload.get("entries"), list):
            raise DataError("Unsupported contract benchmark catalog schema")
        self.path = path
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        self.entries: list[dict[str, Any]] = []
        for entry in payload["entries"]:
            aliases = entry.get("aliases") or []
            required = {
                "id",
                "display_name",
                "market_scope",
                "market_label",
                "asset_class",
                "style_label",
                "structure",
                "excluded_target",
            }
            if not required.issubset(entry) or not aliases:
                raise DataError(f"Invalid contract benchmark catalog entry: {entry!r}")
            normalized_aliases = sorted(
                {normalize_benchmark_name(str(alias)) for alias in aliases if str(alias).strip()},
                key=len,
                reverse=True,
            )
            if not normalized_aliases:
                raise DataError(f"Contract benchmark {entry['id']} has no usable aliases")
            self.entries.append({**entry, "normalized_aliases": normalized_aliases})

    def match(self, value: str) -> list[dict[str, Any]]:
        normalized = normalize_benchmark_name(value)
        matches: dict[str, dict[str, Any]] = {}
        for entry in self.entries:
            if any(alias in normalized for alias in entry["normalized_aliases"]):
                matches[str(entry["id"])] = entry
        return list(matches.values())












def fetch_latest_legal_documents(
    client: HttpClient,
    code: str,
    as_of: date,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> tuple[LegalDocument | None, LegalDocument | None]:
    prospectuses: list[LegalDocument] = []
    summaries: list[LegalDocument] = []
    if snapshot is not None:
        if snapshot.code != code or snapshot.as_of != as_of:
            raise DataError("Announcement snapshot identity does not match legal-document request")
        page_items: list[list[AnnouncementRecord]] = [list(snapshot.items)]
    else:
        page_items = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        if snapshot is not None:
            records = page_items[0]
            total_pages = 1
        else:
            payload = _announcement_page(client, code, page)
            if page == 1:
                total_count = int(payload.get("TotalCount") or 0)
                page_size = int(payload.get("PageSize") or 100)
                total_pages = max(1, math.ceil(total_count / max(page_size, 1)))
            records = _parse_announcement_page(payload, code)
        for record in records:
            title = record.title
            published = record.published_date
            if published > as_of:
                continue
            announcement_id = record.announcement_id
            if not announcement_id:
                continue
            source_url = record.source_url
            if (
                "招募说明书" in title
                and "提示性公告" not in title
                and "摘要" not in title
            ):
                prospectuses.append(
                    LegalDocument(
                        announcement_id,
                        title,
                        published,
                        source_url,
                        "prospectus",
                    )
                )
            if "基金产品资料概要" in title and _is_rmb_product_summary(title):
                summaries.append(
                    LegalDocument(
                        announcement_id,
                        title,
                        published,
                        source_url,
                        "product_summary",
                    )
                )
        if prospectuses and summaries:
            break
        if not records:
            break
        page += 1
    prospectus = (
        max(prospectuses, key=lambda item: (item.published_date, item.announcement_id))
        if prospectuses
        else None
    )
    summary = (
        max(summaries, key=lambda item: (item.published_date, item.announcement_id))
        if summaries
        else None
    )
    return prospectus, summary


def extract_contract_benchmark_statement(text: str) -> str:
    compact = re.sub(
        r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])",
        "",
        text.replace("\u3000", " "),
    )
    compact = re.sub(r"\s+", " ", compact).strip()
    starts = list(
        re.finditer(
            r"(?:本基金(?:选择)?的?)?业绩比较基准(?:为|是|采用|：|:)\s*",
            compact,
        )
    )
    starts.extend(
        re.finditer(
            r"业绩比较基准\s*(?=(?:\d|经|标|纳|MSCI|摩根|彭博|伦敦|富时|恒生|中证|人民币|美元|[A-Z]))",
            compact,
            re.I,
        )
    )
    stop_re = re.compile(
        r"风险收益特征|业绩比较基准的选择理由|如果今后|若今后|在法律法规|"
        r"基金管理人可|本基金为|本基金选择|本基金设置|本基金采取"
    )
    candidates: list[str] = []
    for match in starts:
        tail = compact[match.end() : match.end() + 1200]
        stop = stop_re.search(tail)
        statement = tail[: stop.start() if stop else 800].strip(" ：:。；;")
        sentence_end = re.search(r"[。；;]", statement)
        if sentence_end:
            statement = statement[: sentence_end.start()].strip()
        normalized = normalize_benchmark_name(statement)
        if statement and any(
            token in normalized
            for token in ("指数", "价格", "利率", "INDEX", "PRICE", "VIX")
        ):
            candidates.append(statement)
    if not candidates:
        raise DataError("Could not locate the current performance benchmark statement")
    return min(candidates, key=lambda value: (0 if "%" in value else 1, len(value)))


def _benchmark_weight(statement: str, entry: dict[str, Any]) -> float:
    compact = re.sub(r"\s+", " ", statement)
    for alias in sorted(entry.get("aliases") or [], key=lambda value: len(str(value)), reverse=True):
        parts = [re.escape(part) for part in re.split(r"\s+", str(alias).strip()) if part]
        if not parts:
            continue
        alias_pattern = r"\s*".join(parts)
        for match in re.finditer(alias_pattern, compact, re.I):
            before = compact[max(0, match.start() - 45) : match.start()]
            after = compact[match.end() : match.end() + 90]
            before_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*[×*xX]\s*$", before)
            if before_match:
                return float(before_match.group(1))
            after_match = re.search(
                r"^[^+＋，,。；;]{0,55}?[×*xX]\s*(\d+(?:\.\d+)?)\s*%",
                after,
            )
            if after_match:
                return float(after_match.group(1))
    percentages = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s*%", compact)]
    if percentages:
        return max(percentages)
    return 100.0


def detect_product_structure(text: str, catalog_value: str) -> str:
    normalized = re.sub(r"\s+", "", text)
    if re.search(r"反向|做空|Inverse|Short", normalized, re.I):
        return "inverse"
    if re.search(
        r"(?:[2-9]|两|三)倍(?:做多|多头|杠杆)|杠杆指数|Leveraged|Ultra",
        normalized,
        re.I,
    ):
        return "leveraged"
    if re.search(r"波动率|VIX|Volatility", normalized, re.I):
        return "volatility"
    return catalog_value


def parse_contract_benchmark(
    text: str,
    fund: dict[str, Any],
    catalog: ContractBenchmarkCatalog,
) -> dict[str, Any]:
    statement = extract_contract_benchmark_statement(text)
    matches = catalog.match(statement)
    if not matches and "标的指数" in statement:
        matches = catalog.match(f"{fund['name']} {text[:3000]}")
    components = [
        {
            "benchmark_id": entry["id"],
            "benchmark_name": entry["display_name"],
            "weight_pct": round(_benchmark_weight(statement, entry), 2),
            "market_scope": entry["market_scope"],
            "market_label": entry["market_label"],
            "asset_class": entry["asset_class"],
            "style_label": entry["style_label"],
            "structure": entry["structure"],
            "excluded_target": bool(entry["excluded_target"]),
        }
        for entry in matches
    ]
    components.sort(key=lambda item: (-float(item["weight_pct"]), item["benchmark_id"]))
    single = components[0] if len(components) == 1 else None
    status = "recognized" if single else "composite" if components else "unrecognized"
    return {
        "status": status,
        "benchmark_text": statement,
        "benchmark_id": single["benchmark_id"] if single else None,
        "benchmark_name": (
            single["benchmark_name"]
            if single
            else " + ".join(item["benchmark_name"] for item in components)
            if components
            else "未识别"
        ),
        "benchmark_weight_pct": single["weight_pct"] if single else None,
        "market_scope": single["market_scope"] if single else "composite" if components else "unknown",
        "market_label": single["market_label"] if single else "复合市场" if components else "未识别",
        "asset_class": single["asset_class"] if single else "mixed" if components else "unknown",
        "style_label": single["style_label"] if single else "复合风格" if components else "未识别",
        "structure": detect_product_structure(
            f"{fund['name']} {statement}",
            str(single["structure"]) if single else "standard",
        ),
        "excluded_target": bool(components) and all(
            bool(item["excluded_target"]) for item in components
        ),
        "components": components,
    }


def unavailable_holding_cost(
    summary: LegalDocument | None, status: str = "unavailable"
) -> dict[str, Any]:
    return {
        "status": status,
        "annualized_pct": None,
        "measurement_date": None,
        "source_title": summary.title if summary else None,
        "source_published_date": summary.published_date.isoformat() if summary else None,
        "source_url": summary.source_url if summary else None,
    }


def unreadable_contract_benchmark(fund: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "unreadable",
        "benchmark_text": "未识别",
        "benchmark_id": None,
        "benchmark_name": "未识别",
        "benchmark_weight_pct": None,
        "market_scope": "unknown",
        "market_label": "未识别",
        "asset_class": "unknown",
        "style_label": "未识别",
        "structure": detect_product_structure(fund["name"], "standard"),
        "excluded_target": False,
        "components": [],
    }


def parse_holding_cost(
    text: str, summary: LegalDocument, as_of: date
) -> dict[str, Any]:
    if summary.published_date > as_of:
        raise DataError("Holding-cost source publication date is in the future")
    compact = re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()
    rate_match = re.search(
        r"基金运作综合费率\s*[（(]\s*年化\s*[）)]"
        r"(?:\s*(?:基金运作综合费率|\d+\s*/\s*\d+|[-–—])){0,3}"
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        compact,
    )
    if not rate_match:
        raise DataError("Could not locate annualized comprehensive operating expense")
    rate = float(rate_match.group(1))
    if not math.isfinite(rate) or rate < 0 or rate > 100:
        raise DataError("Annualized comprehensive operating expense is outside its valid range")
    date_match = re.search(
        r"(?:综合费率[^。]{0,80})?测算日期为\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        compact,
    )
    measurement_date = None
    if date_match:
        measured = date(*(int(value) for value in date_match.groups()))
        if measured > as_of:
            raise DataError("Holding-cost measurement date is in the future")
        measurement_date = measured.isoformat()
    return {
        "status": "parsed",
        "annualized_pct": round(rate, 2),
        "measurement_date": measurement_date,
        "source_title": summary.title,
        "source_published_date": summary.published_date.isoformat(),
        "source_url": summary.source_url,
    }


def resolve_contract_benchmark(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    document_cache: PeriodicReportCache,
    catalog: ContractBenchmarkCatalog,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    code = fund["code"]
    warnings: list[str] = []
    management_style = (
        "passive"
        if fund["fund_type"] == "指数型-海外股票" and "增强" not in fund["name"]
        else "active"
    )
    try:
        prospectus, summary = fetch_latest_legal_documents(
            client, code, as_of, snapshot=snapshot
        )
    except DataError as exc:
        profile = unreadable_contract_benchmark(fund)
        profile.update(
            {
                "management_style": management_style,
                "prospectus_title": None,
                "prospectus_published_date": None,
                "source_url": None,
                "product_summary_status": "unreadable",
                "product_summary_published_date": None,
                "product_summary_source_url": None,
                "catalog_fingerprint": catalog.fingerprint,
            }
        )
        warnings.extend(
            (
                f"合同基准告警 {code}：法律文件目录无法读取：{exc}",
                f"持有费率告警 {code}：法律文件目录无法读取：{exc}",
            )
        )
        return profile, unavailable_holding_cost(None), warnings

    if prospectus is not None:
        try:
            prospectus_text = document_cache.get_text(
                client, prospectus, fund["fund_page_url"]
            )
            profile = parse_contract_benchmark(prospectus_text, fund, catalog)
        except DataError as exc:
            profile = unreadable_contract_benchmark(fund)
            warnings.append(f"合同基准告警 {code}：{exc}")
    else:
        profile = unreadable_contract_benchmark(fund)
        warnings.append(f"合同基准告警 {code}：截至 {as_of} 没有可用的招募说明书。")

    summary_status = "missing"
    summary_url = summary.source_url if summary else None
    summary_published_date = summary.published_date.isoformat() if summary else None
    holding_cost = unavailable_holding_cost(summary)
    if summary is not None:
        try:
            summary_text = document_cache.get_text(
                client, summary, fund["fund_page_url"]
            )
        except DataError as exc:
            summary_status = "unreadable"
            warnings.append(f"产品概要告警 {code}：{exc}")
            warnings.append(f"持有费率告警 {code}：{exc}")
        else:
            try:
                holding_cost = parse_holding_cost(summary_text, summary, as_of)
            except DataError as exc:
                warnings.append(f"持有费率告警 {code}：{exc}")
            try:
                summary_profile = parse_contract_benchmark(summary_text, fund, catalog)
                prospectus_ids = {
                    item["benchmark_id"] for item in profile["components"]
                }
                summary_ids = {
                    item["benchmark_id"] for item in summary_profile["components"]
                }
                if profile["status"] in {"unreadable", "unrecognized"}:
                    summary_status = "unreadable"
                elif prospectus_ids != summary_ids:
                    summary_status = "conflict"
                    warnings.append(
                        f"产品概要告警 {code}：招募说明书与人民币产品概要的合同基准不一致。"
                    )
                else:
                    summary_status = "matched"
            except DataError as exc:
                summary_status = "unreadable"
                warnings.append(f"产品概要告警 {code}：{exc}")
    else:
        warnings.append(f"持有费率告警 {code}：截至 {as_of} 没有可用的人民币产品概要。")

    profile.update(
        {
            "management_style": management_style,
            "prospectus_title": prospectus.title if prospectus else None,
            "prospectus_published_date": (
                prospectus.published_date.isoformat() if prospectus else None
            ),
            "source_url": prospectus.source_url if prospectus else None,
            "product_summary_status": summary_status,
            "product_summary_published_date": summary_published_date,
            "product_summary_source_url": summary_url,
            "catalog_fingerprint": catalog.fingerprint,
        }
    )
    return profile, holding_cost, warnings




def normalize_instrument_name(value: str) -> str:
    return re.sub(
        r"[^\u4e00-\u9fffA-Z0-9]+", "", value.upper().replace("V AN", "VAN")
    )


class LookthroughResolver:
    def __init__(self, catalog_path: Path, cache_path: Path) -> None:
        try:
            catalog_bytes = catalog_path.read_bytes()
            catalog = json.loads(catalog_bytes.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"Could not load US-equity instrument catalog: {exc}") from exc
        self.entries = catalog.get("entries") or []
        self.catalog_fingerprint = hashlib.sha256(catalog_bytes).hexdigest()
        self.cache_path = cache_path
        self.cache: dict[str, dict[str, Any]] = {}
        if cache_path.exists():
            try:
                saved = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    saved.get("schema_version") == 2
                    and saved.get("catalog_fingerprint") == self.catalog_fingerprint
                ):
                    self.cache = saved.get("entries") or {}
            except (OSError, json.JSONDecodeError):
                self.cache = {}
        self.hits = 0
        self.misses = 0
        self._lock = Lock()

    @staticmethod
    def _usable(result: dict[str, Any], report_date: date) -> bool:
        category = result.get("category")
        if category in {"us_equity", "non_us_equity", "fixed_income", "commodity"}:
            return bool(result.get("source_url"))
        if category != "global_equity" or result.get("data_date") is None:
            return False
        observed = parse_date(str(result["data_date"]))
        return observed <= report_date and (report_date - observed).days <= 120

    def _from_catalog(self, normalized_name: str, report_date: date) -> dict[str, Any] | None:
        for entry in self.entries:
            aliases = [normalize_instrument_name(alias) for alias in entry.get("aliases") or []]
            if not any(alias and alias in normalized_name for alias in aliases):
                continue
            result = {
                "category": entry["category"],
                "us_equity_pct": float(entry["us_equity_pct"]),
                "data_date": entry.get("data_date"),
                "source_url": entry["source_url"],
                "source_name": entry.get("source_name"),
            }
            if self._usable(result, report_date):
                return result
        return None

    def resolve(self, fund_name: str, report_date: date) -> dict[str, Any] | None:
        normalized = normalize_instrument_name(fund_name)
        result = self._from_catalog(normalized, report_date)
        if result is None:
            with self._lock:
                self.misses += 1
            return None
        key = "|".join(
            (
                normalized,
                report_date.isoformat(),
                str(result.get("data_date") or "structural"),
                str(result["source_url"]),
            )
        )
        with self._lock:
            cached = self.cache.get(key)
            if cached is not None and self._usable(cached, report_date):
                self.hits += 1
                return cached
            self.misses += 1
            self.cache[key] = result
            self._save()
            return result

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            self.cache_path,
            {
                "schema_version": 2,
                "catalog_fingerprint": self.catalog_fingerprint,
                "entries": self.cache,
            },
        )

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses}


def clean_report_text(text: str) -> str:
    header = re.compile(
        r"(?m)^.*?[0-9〇零○ＯO一二三四五六七八九]{4}\s*年"
        r"(?:第\s*[1234一二三四]\s*季度|半年度|年度).*?报告\s*$\n^\s*\d+\s*$\n?"
    )
    text = header.sub("", text)
    text = re.sub(r"(?<=\d)\.\s+(?=\d{1,2}(?:\s|$))", ".", text)
    text = re.sub(
        r"(\d{1,3}(?:,\d{3})+\.\d)\s+(\d)(?=\s)", r"\1\2", text
    )
    text = re.sub(
        r"(\d{1,3}(?:,\d{3})+,\d{1,2})\s+(\d{1,2}\.\d{2})(?=\s)",
        r"\1\2",
        text,
    )
    return re.sub(r"(\d{1,3}(?:,\d{3})+)\s+(\.\d{2})(?=\s)", r"\1\2", text)


def parse_fund_investment_rows(text: str, code: str) -> list[dict[str, Any]]:
    headings = list(re.finditer(r"前十名基金投资明\s*细", text))
    if not headings:
        raise DataError(f"Could not locate top fund investments for fund {code}")
    heading = headings[-1]
    end = re.search(r"投资组合报告附注", text[heading.end() :])
    if not end:
        raise DataError(f"Could not locate end of top fund investments for fund {code}")
    table = text[heading.end() : heading.end() + end.start()]
    row_matches = list(re.finditer(r"(?m)^\s*(10|[1-9])\s+", table))
    rows: list[dict[str, Any]] = []
    for index, match in enumerate(row_matches):
        row_end = row_matches[index + 1].start() if index + 1 < len(row_matches) else len(table)
        body = re.sub(r"\s+", " ", table[match.end() : row_end]).strip()
        name_match = re.match(
            r"(.+?)\s+(?:ETF\s*基\s*金|指数基\s*金|开放式\s*基\s*金|基\s*金)\s+",
            body,
        )
        if not name_match:
            name_match = re.match(r"(.+?)\s+ETF\s+(?:交易型|契约型)", body)
        if not name_match:
            name_match = re.match(r"(.+?)\s+QDII\s+(?:交易型|契约型|开放式)", body)
        if not name_match:
            name_match = re.match(
                r"(.+?)\s+(?:债\s*券\s*型|股\s*票\s*型|混\s*合\s*型|商\s*品\s*型|权\s*益\s*类)\s+",
                body,
            )
        if not name_match:
            name_match = re.match(
                r"(.+?)\s+(?:债\s*券\s*型|股\s*票\s*型|混\s*合\s*型|商\s*品\s*型|权\s*益\s*类)"
                r"(?:指\s*数)?基\s*金\s+",
                body,
            )
        # Periodic reports commonly render unused rows as dash-only placeholders.
        # A page footer may follow the dashes (and include the word "基金"), so
        # identify the placeholder before the generic "基金" fallback below.
        if not name_match and re.match(r"^(?:-\s*){2,}", body):
            continue
        if not name_match and "基金" not in body:
            continue
        percentages = [
            float(value)
            for value in re.findall(r"(?<![\d,])(\d{1,3}\.\d{2})(?!\d)", body)
            if float(value) <= 100
        ]
        # A dash in the percentage column denotes a negligible holding
        # (the reports use it instead of a rounded 0.00).  Preserve the row
        # with a zero weight so the rest of the look-through scan remains
        # complete, while still failing on genuinely malformed rows.
        if not percentages and name_match and re.search(r"\d[\d,]*\.\d{2}\s+-", body):
            percentages = [0.0]
        if not name_match or not percentages:
            raise DataError(
                f"Could not parse top fund investment row {match.group(1)} for fund {code}"
            )
        fund_name = name_match.group(1)
        if re.search(r"商\s*品\s*型", body):
            reported_category = "commodity"
        elif re.search(r"债\s*券\s*型", body):
            reported_category = "fixed_income"
        else:
            reported_category = None
        if len(normalize_instrument_name(fund_name)) < 12:
            continuation = re.search(
                r"\d{1,3}\.\d{2}\s+([A-Z][A-Z0-9& ]+?ETF)(?:\s+[A-Z][a-z]|\s*$)",
                body,
            )
            if continuation:
                fund_name = f"{fund_name} {continuation.group(1)}"
        rows.append(
            {
                "rank": int(match.group(1)),
                "fund_name": fund_name,
                "weight_pct": percentages[0],
                "reported_category": reported_category,
            }
        )
    if not rows:
        raise DataError(f"No top fund investments were parsed for fund {code}")
    return rows


def parse_us_equity_report(text: str, code: str) -> dict[str, Any]:
    text = clean_report_text(text)
    direct_headings = list(re.finditer(
        r"(?:报告期末|期末)在各个国家（地区）证券市场的"
        r"(?:股票及存托\s*凭证|权益)投资分\s*布",
        text,
    ))
    if not direct_headings:
        raise DataError(f"Could not locate country equity distribution for fund {code}")
    direct_heading = direct_headings[-1]
    direct_segment = text[direct_heading.end() : direct_heading.end() + 1200]
    next_heading = re.search(r"\n\s*\d+(?:\.\d+)+\s*", direct_segment)
    if next_heading:
        direct_segment = direct_segment[: next_heading.start()]
    direct_match = re.search(
        r"美国\s+([\d,.]+)\s+(\d+(?:\.\d+)?)", re.sub(r"\s+", " ", direct_segment)
    )
    direct_us_pct = float(direct_match.group(2)) if direct_match else 0.0

    asset_headings = list(re.finditer(r"(?:报告期末|期末)基金资产组合情况", text))
    if not asset_headings:
        raise DataError(f"Could not locate asset allocation table for fund {code}")
    asset_heading = asset_headings[-1]
    asset_segment = re.sub(r"\s+", " ", text[asset_heading.end() : asset_heading.end() + 2500])
    fund_amount_match = re.search(r"(?:^|\s)2\s+基金投资\s+([\d,.]+)\s+\d+(?:\.\d+)?", asset_segment)
    no_fund_investment = bool(
        re.search(r"(?:^|\s)2\s+基金投资\s+(?:[－—-]\s*){1,2}(?:\s|$)", asset_segment)
    )
    if not fund_amount_match and not no_fund_investment:
        raise DataError(f"Could not parse fund investment amount for fund {code}")
    fund_amount = (
        float(fund_amount_match.group(1).replace(",", "")) if fund_amount_match else 0.0
    )

    holdings: list[dict[str, Any]] = []
    total_fund_pct = 0.0
    if fund_amount > 0:
        net_match = re.search(
            r"期末基金\s*资产\s*净值(.{0,1400}?)"
            r"5\.\s*期末基金\s*份额\s*净值",
            text,
            re.S,
        )
        net_values: list[float] = []
        if net_match:
            net_values = [
                float(value.replace(",", ""))
                for value in re.findall(r"(?<!\d)(\d[\d,]*\.\d{2})(?!\d)", net_match.group(1))
            ]
        else:
            # Midyear and annual reports disclose the aggregate in their balance sheet.
            balance_headings = list(re.finditer(r"\d+\.\d+\s+资产负债表", text))
            for balance_heading in reversed(balance_headings):
                balance_segment = text[balance_heading.end() : balance_heading.end() + 8000]
                balance_match = re.search(r"净资产合计\s+(\d[\d,]*\.\d{2})", balance_segment)
                if balance_match:
                    net_values = [float(balance_match.group(1).replace(",", ""))]
                    break
        if not net_values or sum(net_values) <= 0:
            raise DataError(f"Could not parse positive fund net assets for fund {code}")
        total_fund_pct = fund_amount / sum(net_values) * 100
        holdings = parse_fund_investment_rows(text, code)
    return {
        "direct_us_pct": round(direct_us_pct, 4),
        "fund_investment_pct": round(total_fund_pct, 4),
        "fund_holdings": holdings,
    }


def calculate_us_equity_exposure_base(
    parsed: dict[str, Any],
    report: PeriodicReport,
    resolver: LookthroughResolver,
) -> tuple[dict[str, Any], list[str]]:
    direct_us_pct = float(parsed["direct_us_pct"])
    lookthrough_confirmed = 0.0
    unresolved = 0.0
    components: list[dict[str, Any]] = []
    warnings: list[str] = []
    disclosed_weight = 0.0
    for holding in parsed["fund_holdings"]:
        weight = float(holding["weight_pct"])
        disclosed_weight += weight
        if holding.get("reported_category") in {"commodity", "fixed_income"}:
            resolved = {
                "category": holding["reported_category"],
                "us_equity_pct": 0.0,
                "data_date": report.report_date.isoformat(),
                "source_url": report.source_url,
            }
        else:
            resolved = resolver.resolve(holding["fund_name"], report.report_date)
        if resolved is None:
            contribution = 0.0
            possible_contribution = weight
            unresolved += weight
            if weight >= 0.005:
                warnings.append(
                    f"{holding['fund_name']} 无法按 {report.report_date} 的可用数据穿透，"
                    f"其 {weight:.2f}% 仓位仅计入可能上限。"
                )
            category = "unresolved"
            exposure_pct = None
            source_url = None
            data_date = None
        else:
            exposure_pct = float(resolved["us_equity_pct"])
            contribution = weight * exposure_pct / 100
            possible_contribution = contribution
            category = str(resolved["category"])
            source_url = resolved.get("source_url")
            data_date = resolved.get("data_date")
            lookthrough_confirmed += contribution
        components.append(
            {
                "fund_name": holding["fund_name"],
                "weight_pct": round(weight, 4),
                "category": category,
                "us_equity_pct": exposure_pct,
                "confirmed_contribution_pct": round(contribution, 4),
                "possible_contribution_pct": round(possible_contribution, 4),
                "data_date": data_date,
                "source_url": source_url,
            }
        )
    residual = max(0.0, float(parsed["fund_investment_pct"]) - disclosed_weight)
    if residual > 0.01:
        unresolved += residual
        warnings.append(
            f"前十大基金之外尚有 {residual:.2f}% 基金仓位未披露，仅计入可能上限。"
        )
        components.append(
            {
                "fund_name": "前十大之外未披露基金仓位",
                "weight_pct": round(residual, 4),
                "category": "unresolved_residual",
                "us_equity_pct": None,
                "confirmed_contribution_pct": 0.0,
                "possible_contribution_pct": round(residual, 4),
                "data_date": None,
                "source_url": report.source_url,
            }
        )
    confirmed = min(100.0, direct_us_pct + lookthrough_confirmed)
    possible = min(100.0, confirmed + unresolved)
    return (
        {
            "confirmed_pct": round(confirmed, 2),
            "possible_pct": round(possible, 2),
            "direct_us_pct": round(direct_us_pct, 2),
            "lookthrough_confirmed_pct": round(lookthrough_confirmed, 2),
            "unresolved_pct": round(unresolved, 2),
            "report_date": report.report_date.isoformat(),
            "published_date": report.published_date.isoformat(),
            "source_url": report.source_url,
            "components": components,
        },
        warnings,
    )


def apply_us_equity_threshold(
    exposure: dict[str, Any], threshold: float
) -> tuple[dict[str, Any], list[str]]:
    confirmed = float(exposure["confirmed_pct"])
    possible = float(exposure["possible_pct"])
    warnings: list[str] = []
    if confirmed >= threshold:
        status = "qualified"
    elif possible < threshold:
        status = "excluded"
    else:
        status = "ambiguous"
        warnings.append(
            f"美股占比区间 {confirmed:.2f}%-{possible:.2f}% 跨越 {threshold:g}% 阈值，"
            "按确认下限进入全球补充榜。"
        )
    return {**exposure, "status": status}, warnings


def calculate_us_equity_exposure(
    parsed: dict[str, Any],
    report: PeriodicReport,
    resolver: LookthroughResolver,
    threshold: float,
) -> tuple[dict[str, Any], list[str]]:
    exposure, warnings = calculate_us_equity_exposure_base(parsed, report, resolver)
    classified, threshold_warnings = apply_us_equity_threshold(exposure, threshold)
    return classified, [*warnings, *threshold_warnings]




def fetch_us_equity_exposure(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    report_cache: PeriodicReportCache,
    resolver: LookthroughResolver,
    threshold: float,
    exposure_cache: FundExposureResultCache | None = None,
    report: PeriodicReport | None = None,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if exposure_cache is not None:
        return exposure_cache.get(
            client, fund, as_of, report_cache, resolver, threshold, report=report
        )
    if report is None:
        report = fetch_latest_periodic_report(
            client, fund["code"], as_of, snapshot=snapshot
        )
    text = report_cache.get_text(client, report, fund["fund_page_url"])
    parsed = parse_us_equity_report(text, fund["code"])
    return calculate_us_equity_exposure(parsed, report, resolver, threshold)






def new_limit_state(status: str = "unknown", amount: int | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "amount_cny": amount,
        "effective_date": None,
        "source_url": None,
        "confidence": "low" if status == "unknown" else "medium",
    }


def apply_limit(
    state: dict[str, Any], status: str, amount: int | None, transition: dict[str, Any]
) -> None:
    state.update(
        {
            "status": status,
            "amount_cny": amount,
            "effective_date": transition["effective_date"].isoformat(),
            "source_url": transition["source_url"],
            "confidence": transition.get("confidence", "medium"),
        }
    )


def resolve_quota(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    document_cache: PeriodicReportCache | None = None,
    snapshot: FundAnnouncementSnapshot | None = None,
    notice_cache: QuotaNoticeParseCache | None = None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if fund["purchase_status"] == "open" and fund.get("page_agency_limit_cny") is None:
        direct = new_limit_state("unlimited")
        agency = new_limit_state("unlimited")
    else:
        direct = new_limit_state()
        agency = new_limit_state(
            "limited" if fund.get("page_agency_limit_cny") else "unknown",
            fund.get("page_agency_limit_cny"),
        )
        if agency["status"] == "limited":
            agency["source_url"] = fund["fund_page_url"]
            agency["confidence"] = "medium"

    aggregation = "not applicable"
    all_channels_combined = False
    applied_sources: set[str] = set()
    transitions: list[dict[str, Any]] = []
    latest_unparsed_notice_date: date | None = None
    notices = fetch_announcements(client, fund["code"], as_of, snapshot=snapshot)
    for notice in notices:
        try:
            if notice_cache is not None:
                if document_cache is None:
                    raise DataError("Quota notice cache requires the PDF document cache")
                parsed_transitions = notice_cache.get(
                    client, fund, notice, document_cache
                )
            elif document_cache is None:
                pdf = client.get_bytes(notice["url"], referer=fund["fund_page_url"])
                text = extract_pdf_text(pdf)
                parsed_transitions = parse_quota_notice(
                    text, notice["published"], notice["url"]
                )
            else:
                document = LegalDocument(
                    str(notice["id"]),
                    str(notice["title"]),
                    notice["published"],
                    str(notice["url"]),
                    "quota_notice",
                )
                text = document_cache.get_text(
                    client, document, fund["fund_page_url"]
                )
                parsed_transitions = parse_quota_notice(
                    text, notice["published"], notice["url"]
                )
            if not parsed_transitions:
                raise DataError("quota notice produced no effective limit transition")
            transitions.extend(parsed_transitions)
        except DataError as exc:
            warnings.append(f"{fund['code']} quota notice could not be parsed: {exc}")
            if (
                latest_unparsed_notice_date is None
                or notice["published"] > latest_unparsed_notice_date
            ):
                latest_unparsed_notice_date = notice["published"]

    if latest_unparsed_notice_date is not None:
        # A newer unreadable limit notice invalidates page state and older
        # transitions. A later readable notice can establish a fresh state.
        direct = new_limit_state()
        agency = new_limit_state()
        transitions = [
            item
            for item in transitions
            if parse_date(str(item["published_date"])) > latest_unparsed_notice_date
        ]

    transitions.sort(key=lambda item: (item["effective_date"], item["published_date"]))
    for transition in transitions:
        if transition["effective_date"] > as_of:
            continue
        if transition.get("share_aggregation"):
            aggregation = transition["share_aggregation"]
        if transition.get("all_channels_combined"):
            all_channels_combined = True
        global_amount = transition.get("global_amount_cny")
        direct_amount = transition.get("direct_amount_cny")
        agency_amount = transition.get("agency_amount_cny")
        global_status = transition.get("global_status", "limited")
        if direct_amount is not None:
            apply_limit(direct, "limited", direct_amount, transition)
            applied_sources.add(transition["source_url"])
        if agency_amount is not None:
            apply_limit(agency, "limited", agency_amount, transition)
            applied_sources.add(transition["source_url"])
        if direct_amount is None and agency_amount is None and global_status == "unlimited":
            apply_limit(direct, "unlimited", None, transition)
            apply_limit(agency, "unlimited", None, transition)
            applied_sources.add(transition["source_url"])
        elif global_amount is not None:
            if direct_amount is None:
                apply_limit(direct, "limited", global_amount, transition)
            if agency_amount is None:
                apply_limit(agency, "limited", global_amount, transition)
            applied_sources.add(transition["source_url"])

    if direct["status"] == "unknown" or agency["status"] == "unknown":
        warnings.append(
            f"{fund['code']} 的申购额度无法确定；引用前请核对关联公告。"
        )
    statuses = {direct["status"], agency["status"]}
    if "unknown" in statuses:
        quota_status = "unknown"
        confidence = "low"
    elif "limited" in statuses:
        quota_status = "limited"
        confidence = "high" if all(item["confidence"] == "high" for item in (direct, agency)) else "medium"
    else:
        quota_status = "unlimited"
        confidence = "medium"
    if all_channels_combined:
        channel_rule = "all sales channels combined"
    elif direct["amount_cny"] != agency["amount_cny"]:
        channel_rule = "direct and agency limits differ"
    else:
        channel_rule = "same fund-level limit"
    return (
        {
            "quota_status": quota_status,
            "quota_confidence": confidence,
            "direct_limit": direct,
            "agency_limit": agency,
            "share_class_rule": aggregation,
            "channel_rule": channel_rule,
            "quota_source_urls": sorted(applied_sources),
        },
        warnings,
    )




def write_json(path: Path, payload: dict[str, Any]) -> None:
    if payload.get("schema_version") == RANKING_SCHEMA_VERSION:
        render_json(path, payload)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


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


def all_ranking_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *payload.get("records", []),
        *((payload.get("global_supplement") or {}).get("records") or []),
        *((payload.get("nasdaq100_otc") or {}).get("records") or []),
    ]






def product_structure_tags(profile: dict[str, Any], fund_type: str) -> list[str]:
    labels = ["被动" if profile["management_style"] == "passive" else "主动"]
    type_label = {
        "QDII-REITs": "REIT",
        "QDII-FOF": "FOF",
        "指数型-海外股票": "指数",
    }.get(fund_type, "股票/混合")
    labels.append(type_label)
    if profile["style_label"] not in {"未识别", "复合风格"}:
        labels.append(str(profile["style_label"]))
    elif profile["status"] == "composite":
        labels.append("复合风格")
    special = {
        "leveraged": "杠杆",
        "inverse": "反向",
        "volatility": "波动率策略",
    }.get(str(profile["structure"]))
    if special and special not in labels:
        labels.append(special)
    return labels


def build_output_record(
    fund: dict[str, Any], rank: int, ranking_list: str, holder_report_date: str
) -> dict[str, Any]:
    record = {
        "rank": rank,
        "ranking_list": ranking_list,
        "routing_reason": fund["routing_reason"],
        "code": fund["code"],
        "name": fund["name"],
        "fund_type": fund["fund_type"],
        "management_style": fund["contract_benchmark"]["management_style"],
        "product_structure_tags": product_structure_tags(
            fund["contract_benchmark"], fund["fund_type"]
        ),
        "contract_benchmark": fund["contract_benchmark"],
        "holding_cost": fund["holding_cost"],
        "institution_holding_ratio_pct": fund["institution_holding_ratio_pct"],
        "holder_report_date": holder_report_date,
        "inception_date": fund["inception_date"],
        "scale_billion_cny": fund["scale_billion_cny"],
        "scale_report_date": fund["scale_report_date"],
        "purchase_status": fund["purchase_status"],
        "purchase_status_text": fund["purchase_status_text"],
        "fund_page_url": fund["fund_page_url"],
        "performance_source_url": fund["performance_source_url"],
        "nav_history_start_date": fund["nav_history_start_date"],
        "nav_history_end_date": fund["nav_history_end_date"],
        "one_year_return_pct": fund["one_year_return_pct"],
        "one_year_max_drawdown_pct": fund["one_year_max_drawdown_pct"],
        "one_year_performance_start_date": fund["one_year_performance_start_date"],
        "one_year_performance_end_date": fund["one_year_performance_end_date"],
        "two_year_return_pct": fund.get("two_year_return_pct"),
        "two_year_max_drawdown_pct": fund.get("two_year_max_drawdown_pct"),
        "two_year_performance_start_date": fund.get("two_year_performance_start_date"),
        "two_year_performance_end_date": fund.get("two_year_performance_end_date"),
        "three_year_return_pct": fund["three_year_return_pct"],
        "three_year_max_drawdown_pct": fund["three_year_max_drawdown_pct"],
        "three_year_performance_start_date": fund["three_year_performance_start_date"],
        "three_year_performance_end_date": fund["three_year_performance_end_date"],
        "three_year_boundary_shortfall_days": fund["three_year_boundary_shortfall_days"],
        "five_year_return_pct": fund["five_year_return_pct"],
        "five_year_performance_start_date": fund["five_year_performance_start_date"],
        "five_year_performance_end_date": fund["five_year_performance_end_date"],
        "ten_year_return_pct": fund["ten_year_return_pct"],
        "ten_year_performance_start_date": fund["ten_year_performance_start_date"],
        "ten_year_performance_end_date": fund["ten_year_performance_end_date"],
        "nasdaq100_fit": fund["nasdaq100_fit"],
        "nasdaq100_fit_2y": fund.get("nasdaq100_fit_2y"),
        "us_equity_exposure": fund["us_equity_exposure"],
        **{key: fund[key] for key in (
            "quota_status",
            "quota_confidence",
            "direct_limit",
            "agency_limit",
            "share_class_rule",
            "channel_rule",
            "quota_source_urls",
        )},
    }
    if ranking_list == "global_supplement":
        record["three_year_annualized_return_pct"] = round(
            float(fund["_three_year_annualized_return_pct"]), 2
        )
        score = fund.get("_return_drawdown_ratio")
        record["return_drawdown_ratio"] = None if score is None else round(float(score), 4)
    return record


def build_nasdaq100_otc_output_record(
    fund: dict[str, Any], rank: int, holder_report_date: str
) -> dict[str, Any]:
    """Build the independent name-based display ranking record."""
    unknown_limit = {
        "status": "unknown",
        "amount_cny": None,
        "effective_date": None,
        "source_url": None,
        "confidence": "not_evaluated",
    }
    contract_match_warning = None
    if not contract_mentions_nasdaq100(fund["contract_benchmark"]):
        contract_match_warning = "名称命中但合同基准未明确识别为纳斯达克100，本榜按名称口径保留。"
    return {
        "rank": rank,
        "ranking_list": "nasdaq100_otc",
        "routing_reason": "name_match",
        "name_match_rule": "纳斯达克100 / 纳指100 / NASDAQ 100 名称匹配",
        "code": fund["code"],
        "name": fund["name"],
        "fund_type": fund["fund_type"],
        "management_style": fund["contract_benchmark"]["management_style"],
        "product_structure_tags": product_structure_tags(
            fund["contract_benchmark"], fund["fund_type"]
        ),
        "contract_benchmark": fund["contract_benchmark"],
        "contract_name_match": contract_match_warning is None,
        "contract_name_match_warning": contract_match_warning,
        "holding_cost": fund["holding_cost"],
        "institution_holding_ratio_pct": fund.get("institution_holding_ratio_pct"),
        "holder_report_date": holder_report_date,
        "inception_date": fund.get("inception_date"),
        "scale_billion_cny": fund.get("scale_billion_cny"),
        "scale_report_date": fund.get("scale_report_date"),
        "purchase_status": fund.get("purchase_status", "unknown"),
        "purchase_status_text": fund.get("purchase_status_text", ""),
        "fund_page_url": fund["fund_page_url"],
        "performance_source_url": fund.get("performance_source_url"),
        "nav_history_start_date": fund.get("nav_history_start_date"),
        "nav_history_end_date": fund.get("nav_history_end_date"),
        "one_year_return_pct": fund.get("one_year_return_pct"),
        "one_year_max_drawdown_pct": fund.get("one_year_max_drawdown_pct"),
        "one_year_performance_start_date": fund.get("one_year_performance_start_date"),
        "one_year_performance_end_date": fund.get("one_year_performance_end_date"),
        "two_year_return_pct": fund.get("two_year_return_pct"),
        "two_year_max_drawdown_pct": fund.get("two_year_max_drawdown_pct"),
        "two_year_performance_start_date": fund.get("two_year_performance_start_date"),
        "two_year_performance_end_date": fund.get("two_year_performance_end_date"),
        "three_year_return_pct": fund.get("three_year_return_pct"),
        "three_year_max_drawdown_pct": fund.get("three_year_max_drawdown_pct"),
        "three_year_performance_start_date": fund.get("three_year_performance_start_date"),
        "three_year_performance_end_date": fund.get("three_year_performance_end_date"),
        "three_year_boundary_shortfall_days": fund.get("three_year_boundary_shortfall_days", 0),
        "five_year_return_pct": fund.get("five_year_return_pct"),
        "five_year_performance_start_date": fund.get("five_year_performance_start_date"),
        "five_year_performance_end_date": fund.get("five_year_performance_end_date"),
        "ten_year_return_pct": fund.get("ten_year_return_pct"),
        "ten_year_performance_start_date": fund.get("ten_year_performance_start_date"),
        "ten_year_performance_end_date": fund.get("ten_year_performance_end_date"),
        "nasdaq100_fit": fund.get("nasdaq100_fit"),
        "nasdaq100_fit_2y": fund.get("nasdaq100_fit_2y"),
        "nasdaq100_fit_2y_error": fund.get("nasdaq100_fit_2y_error"),
        "us_equity_exposure": None,
        "quota_status": "not_evaluated",
        "quota_confidence": "not_evaluated",
        "direct_limit": unknown_limit,
        "agency_limit": unknown_limit.copy(),
        "share_class_rule": "not evaluated",
        "channel_rule": "not evaluated",
        "quota_source_urls": [],
    }


def build_nasdaq100_otc_records(
    client: HttpClient,
    metadata: dict[str, dict[str, str]],
    holder_rows: list[list[str]],
    enriched_candidates: list[dict[str, Any]],
    as_of: date,
    holder_report_date: str,
    benchmark: Nasdaq100Benchmark,
    performance_cache: PerformanceResultCache,
    announcement_cache: AnnouncementIndexCache,
    contract_result_cache: ContractProfileResultCache,
    document_cache: PeriodicReportCache,
    contract_catalog: ContractBenchmarkCatalog,
    run_performance_cache: dict[str, tuple[dict[str, Any], list[str]]] | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    candidates = build_nasdaq100_otc_candidates(metadata, holder_rows)
    known = {item["code"]: item for item in enriched_candidates}
    missing = [item for item in candidates if item["code"] not in known]
    warnings: list[str] = []
    if missing:
        for candidate in missing:
            try:
                known[candidate["code"]] = {
                    **candidate,
                    **parse_fund_page(
                        client.get_text(
                            FUND_PAGE_URL.format(code=candidate["code"]),
                            referer=FUND_PAGE_URL.format(code=candidate["code"]),
                        ),
                        candidate["code"],
                    ),
                }
            except (DataError, OSError, ValueError) as exc:
                warnings.append(f"场外纳指100告警 {candidate['code']}：基金主页无法完整读取：{exc}")
                known[candidate["code"]] = {
                    **candidate,
                    "inception_date": None,
                    "scale_billion_cny": None,
                    "scale_report_date": None,
                    "purchase_status": "unknown",
                    "purchase_status_text": "数据不可用",
                    "fund_page_url": FUND_PAGE_URL.format(code=candidate["code"]),
                    "latest_nav_date": None,
                    "latest_nav_value": None,
                }

    performance_records: list[dict[str, Any]] = []
    for candidate in candidates:
        fund = known[candidate["code"]]
        try:
            if run_performance_cache is not None and fund["code"] in run_performance_cache:
                performance, performance_warnings = run_performance_cache[fund["code"]]
            else:
                performance, performance_warnings = performance_cache.get(
                    client, fund, as_of, benchmark
                )
                if run_performance_cache is not None:
                    run_performance_cache[fund["code"]] = (
                        performance,
                        performance_warnings,
                    )
            warnings.extend(
                f"场外纳指100告警 {fund['code']}：{warning}"
                for warning in performance_warnings
            )
        except (DataError, OSError, ValueError) as exc:
            warnings.append(f"场外纳指100告警 {fund['code']}：净值历史无法读取：{exc}")
            performance = {
                "performance_source_url": PERFORMANCE_DATA_URL.format(
                    code=fund["code"], cache_buster=as_of.strftime("%Y%m%d")
                ),
                "nav_history_start_date": None,
                "nav_history_end_date": None,
                **{
                    f"{prefix}_{field}": None
                    for prefix in ("one_year", "two_year", "three_year", "five_year", "ten_year")
                    for field in (
                        "return_pct",
                        "max_drawdown_pct",
                        "performance_start_date",
                        "performance_end_date",
                    )
                },
                "three_year_boundary_shortfall_days": 0,
                "nasdaq100_fit": None,
                "nasdaq100_fit_2y": None,
                "nasdaq100_fit_2y_error": str(exc),
            }
        try:
            snapshot = announcement_cache.get(client, fund["code"], as_of)
            profile, holding_cost, profile_warnings = contract_result_cache.get(
                client, fund, as_of, document_cache, contract_catalog, snapshot
            )
        except (DataError, OSError, ValueError) as exc:
            profile = unreadable_contract_benchmark(fund)
            profile["management_style"] = (
                "passive"
                if fund["fund_type"] == "指数型-海外股票" and "增强" not in fund["name"]
                else "active"
            )
            profile.update(
                {
                    "prospectus_title": None,
                    "prospectus_published_date": None,
                    "source_url": None,
                    "product_summary_status": "unreadable",
                    "product_summary_published_date": None,
                    "product_summary_source_url": None,
                    "catalog_fingerprint": contract_catalog.fingerprint,
                }
            )
            holding_cost = unavailable_holding_cost(None)
            profile_warnings = [f"场外纳指100告警 {fund['code']}：合同/费率无法读取：{exc}"]
        warnings.extend(profile_warnings)
        performance_records.append(
            {
                **fund,
                **performance,
                "contract_benchmark": profile,
                "holding_cost": holding_cost,
            }
        )

    performance_records.sort(key=nasdaq100_otc_sort_key)
    records = [
        build_nasdaq100_otc_output_record(fund, rank, holder_report_date)
        for rank, fund in enumerate(performance_records, start=1)
    ]
    missing_counts = {
        "two_year_return": sum(item.get("two_year_return_pct") is None for item in records),
        "holding_cost": sum(item["holding_cost"].get("annualized_pct") is None for item in records),
        "nasdaq100_fit_2y": sum(not isinstance(item.get("nasdaq100_fit_2y"), dict) for item in records),
    }
    return records, list(dict.fromkeys(warnings)), missing_counts


# Keep the historical formatting functions available to older callers while
# routing production writes through the renderer package and its atomic staging.
def write_csv(path: Path, payload: dict[str, Any]) -> None:
    render_csv(path, payload)


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    render_markdown(path, payload)


def write_html(path: Path, payload: dict[str, Any]) -> None:
    render_html(path, payload)


def build_payload(
    args: argparse.Namespace,
    client: HttpClient,
    run_metrics: RunMetrics | None = None,
) -> dict[str, Any]:
    metrics = run_metrics or RunMetrics()
    as_of = parse_date(args.as_of) if args.as_of else current_shanghai_date()
    exclusions: dict[str, dict[str, Any]] = {}

    def record_exclusion(reason: str, label: str, code: str) -> None:
        item = exclusions.setdefault(reason, {"reason": reason, "label": label, "codes": []})
        if code not in item["codes"]:
            item["codes"].append(code)

    with metrics.phase("candidate_discovery"):
        metadata = fetch_fund_metadata(client)
        periods = fetch_holder_periods(client)
        selected, warnings = select_holder_period(periods, args.allow_partial_holder_period)
        holder_rows = fetch_holder_rows(client, selected)
        candidates = build_holder_candidates(holder_rows, metadata, [])
        enriched = enrich_fund_pages(client, candidates)
        preliminary = filter_and_rank(
            enriched,
            args.min_scale,
            len(enriched),
            exclude_keywords=(),
            as_of=as_of,
            min_age_years=args.min_age_years,
        )

    cache_root = (args.cache_dir or (args.output_dir / "cache")).resolve()
    document_cache = PeriodicReportCache(cache_root / "announcement-pdfs")
    resolver = LookthroughResolver(
        args.us_equity_catalog.resolve(), cache_root / "us-equity-lookthrough.json"
    )
    contract_catalog = ContractBenchmarkCatalog(args.contract_benchmark_catalog.resolve())
    announcement_cache = AnnouncementIndexCache(cache_root / "announcement-indexes")
    contract_result_cache = ContractProfileResultCache(
        cache_root / "contract-profiles"
    )
    quota_notice_cache = QuotaNoticeParseCache(cache_root / "quota-notices")
    etf_holding_cost_cache = ExchangePremiumHoldingCostCache(
        cache_root / "exchange-premium-holding-costs"
    )
    benchmark_cache = Nasdaq100BenchmarkCache(
        cache_root / "benchmarks" / "nasdaq100-cny.json"
    )
    with metrics.phase("benchmark_update"):
        benchmark, benchmark_warnings = benchmark_cache.get(client, as_of)
    warnings.extend(benchmark_warnings)
    performance_cache = PerformanceResultCache(cache_root / "performance")
    exposure_cache = FundExposureResultCache(cache_root / "fund-us-equity-exposures")
    run_memo = RunMemo(as_of)

    with metrics.phase("performance_scan"):
        (
            performance_qualified,
            performance_warnings,
            performance_scanned_count,
            performance_rejections,
        ) = evaluate_performance_full_scan(
            client,
            preliminary,
            as_of,
            args.min_three_year_return_pct,
            performance_cache,
            benchmark,
            min_five_year_return_pct=args.min_five_year_return_pct,
            min_ten_year_return_pct=args.min_ten_year_return_pct,
            run_cache=run_memo.performance,
        )
    warnings.extend(performance_warnings)
    for code, failures in performance_rejections.items():
        for reason, label in failures:
            record_exclusion(reason, label, code)

    document_results: list[dict[str, Any] | None] = [None] * len(
        performance_qualified
    )

    def evaluate_documents(fund: dict[str, Any]) -> dict[str, Any]:
        snapshot = announcement_cache.get(client, fund["code"], as_of)
        profile, holding_cost, profile_warnings = contract_result_cache.get(
            client,
            fund,
            as_of,
            document_cache,
            contract_catalog,
            snapshot,
        )
        report = fetch_latest_periodic_report(
            client, fund["code"], as_of, snapshot=snapshot
        )
        exposure, exposure_warnings = fetch_us_equity_exposure(
            client,
            fund,
            as_of,
            document_cache,
            resolver,
            args.min_us_equity_pct,
            exposure_cache,
            report=report,
            snapshot=snapshot,
        )
        quota: dict[str, Any] | None = None
        quota_warnings: list[str] = []
        quota_error: str | None = None
        try:
            quota, quota_warnings = resolve_quota(
                client,
                fund,
                as_of,
                document_cache,
                snapshot=snapshot,
                notice_cache=quota_notice_cache,
            )
        except (DataError, OSError, ValueError) as exc:
            quota_error = str(exc)
        return {
            "profile": profile,
            "holding_cost": holding_cost,
            "profile_warnings": profile_warnings,
            "exposure": exposure,
            "exposure_warnings": exposure_warnings,
            "quota": quota,
            "quota_warnings": quota_warnings,
            "quota_error": quota_error,
        }

    with metrics.phase("document_scan"):
        document_results = evaluate_batch(
            performance_qualified,
            evaluate_documents,
            DOCUMENT_WORKERS,
        )

    classified_candidates: list[dict[str, Any]] = []
    for fund, result in zip(performance_qualified, document_results):
        if result is None:
            raise DataError(f"Documents were not evaluated for fund {fund['code']}")
        warnings.extend(result["profile_warnings"])
        classified_candidates.append(
            {
                **fund,
                "contract_benchmark": result["profile"],
                "holding_cost": result["holding_cost"],
                "_document_result": result,
            }
        )

    try:
        routed = route_candidates(
            classified_candidates,
            args.min_us_equity_pct,
            args.us_main_exclude_keywords,
            ranking_route,
        )
    except ValueError as exc:
        raise DataError(str(exc)) from exc
    warnings.extend(routed.warnings)
    us_routed_candidates = list(routed.us_main)
    global_routed_candidates = list(routed.global_supplement)

    us_quota = apply_quota_gate(
        us_routed_candidates, args.min_direct_limit_cny, direct_limit_qualifies
    )
    global_quota = apply_quota_gate(
        global_routed_candidates, args.min_direct_limit_cny, direct_limit_qualifies
    )
    warnings.extend(us_quota.warnings)
    warnings.extend(global_quota.warnings)
    for reason, label, code in (*us_quota.exclusions, *global_quota.exclusions):
        record_exclusion(reason, label, code)

    ranked = rank_candidates(
        us_quota.qualified,
        global_quota.qualified,
        args.top,
        us_main_sort_key,
        global_supplement_sort_key,
        calculate_return_drawdown_ratio,
    )
    us_quota_qualified = list(ranked.us_qualified)
    global_quota_qualified = list(ranked.global_qualified)
    us_ranked = list(ranked.us_ranked)
    global_ranked = list(ranked.global_ranked)
    for reason, label, code in ranked.exclusions:
        record_exclusion(reason, label, code)
    records = [
        build_output_record(fund, rank, "us_main", selected.report_date)
        for rank, fund in enumerate(us_ranked, start=1)
    ]
    global_records = [
        build_output_record(fund, rank, "global_supplement", selected.report_date)
        for rank, fund in enumerate(global_ranked, start=1)
    ]
    if not records:
        warnings.append("美国主榜当前没有符合全部条件的基金。")
    elif len(records) < args.top:
        warnings.append(f"美国主榜仅 {len(records)} 只基金符合全部条件，未放宽门槛。")
    if not global_records:
        warnings.append("全球补充榜当前没有符合全部条件的基金。")
    elif len(global_records) < args.top:
        warnings.append(
            f"全球补充榜仅 {len(global_records)} 只基金符合全部条件，未放宽门槛。"
        )
    if not records and not global_records:
        raise DataError("Both QDII ranking lists are empty after applying all filters")

    with metrics.phase("nasdaq100_otc"):
        nasdaq100_otc_records, nasdaq100_otc_warnings, nasdaq100_otc_missing = (
            build_nasdaq100_otc_records(
                client,
                metadata,
                holder_rows,
                enriched,
                as_of,
                selected.report_date,
                benchmark,
                performance_cache,
                announcement_cache,
                contract_result_cache,
                document_cache,
                contract_catalog,
                run_memo.performance,
            )
        )
    warnings.extend(nasdaq100_otc_warnings)

    with metrics.phase("exchange_premium"):
        configured_catalog = getattr(args, "us_equity_etf_catalog", None)
        premium_quote_rows: dict[str, dict[str, Any]] | None = None
        if configured_catalog:
            premium_catalog_path = Path(configured_catalog).resolve()
            premium_entries, _premium_catalog_fingerprint = load_exchange_premium_catalog(
                premium_catalog_path
            )
        else:
            premium_catalog_path = DEFAULT_US_EQUITY_ETF_CATALOG
            try:
                (
                    premium_entries,
                    premium_quote_rows,
                    _premium_catalog_fingerprint,
                ) = load_qdii_exchange_premium_catalog(client, metadata)
            except (DataError, OSError, ValueError) as exc:
                cached_catalog = _load_cached_qdii_exchange_premium_catalog(
                    cache_root / "exchange-premium.json", metadata
                )
                if cached_catalog is None:
                    raise
                premium_entries, _premium_catalog_fingerprint = cached_catalog
                premium_quote_rows = {}
                warnings.append(
                    f"场内溢价告警：QDII 场内目录刷新失败，使用上次目录和行情缓存：{exc}"
                )
        exchange_premium, premium_warnings = build_exchange_premium_snapshot(
            client,
            premium_catalog_path,
            cache_root / "exchange-premium.json",
            as_of,
            catalog_entries=premium_entries,
            quote_rows=premium_quote_rows,
            catalog_fingerprint=_premium_catalog_fingerprint,
        )
        premium_costs, premium_cost_warnings = build_exchange_premium_holding_costs(
            client,
            exchange_premium["records"],
            as_of,
            announcement_cache,
            document_cache,
            etf_holding_cost_cache,
        )
        attach_exchange_premium_holding_costs(exchange_premium, premium_costs)
    warnings.extend(premium_cost_warnings)
    warnings.extend(premium_warnings)

    us_ranking_method = (
        "nasdaq100_correlation desc, abs(nasdaq100_beta - 1) asc, "
        "us_equity_confirmed_pct desc, institution_holding_ratio_pct desc, "
        "three_year_return_pct desc, code asc"
    )
    global_ranking_method = (
        "three_year_return_drawdown_ratio desc, three_year_return_pct desc, "
        "three_year_max_drawdown_pct desc, institution_holding_ratio_pct desc, "
        "scale_billion_cny desc, code asc"
    )
    return {
        "schema_version": RANKING_SCHEMA_VERSION,
        "run_date": as_of.isoformat(),
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "holder_report_date": selected.report_date,
        "holder_period_fund_count": selected.fund_count,
        "filters": {
            "top": args.top,
            "min_scale_billion_cny": args.min_scale,
            "min_age_years": args.min_age_years,
            "min_three_year_return_pct": args.min_three_year_return_pct,
            "three_year_boundary_tolerance_days": THREE_YEAR_BOUNDARY_TOLERANCE_DAYS,
            "min_five_year_return_pct_if_available": args.min_five_year_return_pct,
            "min_ten_year_return_pct_if_available": args.min_ten_year_return_pct,
            "min_us_equity_pct": args.min_us_equity_pct,
            "min_direct_limit_cny_inclusive": args.min_direct_limit_cny,
            "base_candidates_total": len(preliminary),
            "performance_candidates_scanned": performance_scanned_count,
            "performance_qualified_count": len(performance_qualified),
            "contract_candidates_scanned": len(performance_qualified),
            "contract_metadata_resolved_count": sum(
                fund["contract_benchmark"]["status"] in {"recognized", "composite"}
                for fund in classified_candidates
            ),
            "us_equity_candidates_scanned": len(classified_candidates),
            "us_routed_count": len(us_routed_candidates),
            "global_routed_count": len(global_routed_candidates),
            "us_quota_candidates_scanned": len(us_routed_candidates),
            "us_quota_qualified_count": len(us_quota_qualified),
            "global_quota_candidates_scanned": len(global_routed_candidates),
            "global_quota_qualified_count": len(global_quota_qualified),
            "full_scan_completed": (
                performance_scanned_count == len(preliminary)
                and len(classified_candidates) == len(performance_qualified)
                and len(us_routed_candidates) + len(global_routed_candidates)
                == len(classified_candidates)
            ),
            "ranking_method": us_ranking_method,
            "global_supplement_ranking_method": global_ranking_method,
            "us_equity_method": "conservative confirmed lower bound determines routing unless a fund-name geography keyword keeps the fund out of the US main list; unresolved positions only increase possible upper bound",
            "contract_benchmark_method": "display-only latest prospectus metadata; benchmark identity, market, structure, weight, and parse status never affect eligibility or routing",
            "us_main_exclude_keywords": args.us_main_exclude_keywords,
            "global_exclude_keywords": [],
            "exclude_fund_types": sorted(EXCLUDED_FUND_TYPES),
            "exclude_asset_classes": ["bond", "commodity"],
            "share_class": "OTC RMB A or explicit RMB primary share without C/D marker",
            "purchasable_only": True,
            "nasdaq100_otc": {
                "selection_method": (
                    "从完整基金元数据按名称发现候选；不设置成立年限、收益、额度或申购状态硬门槛，"
                    "仅保留场外人民币 A/未标记主份额并排除独立 ETF"
                ),
                "name_match_rule": "纳斯达克100 / 纳指100 / NASDAQ 100 名称匹配",
                "name_match_pattern": NASDAQ100_OTC_NAME_RE.pattern,
                "share_class": "OTC RMB A or explicit RMB primary share without C/D/E/F/I marker",
                "exclude_standalone_etf": True,
                "purchasable_only": False,
                "top": None,
                "ranking_method": (
                    "two_year_return_pct desc, holding_cost.annualized_pct asc, "
                    "nasdaq100_fit_2y.tracking_error_pct asc, three_year_return_pct desc, "
                    "scale_billion_cny desc, code asc; missing values last"
                ),
                "candidate_count": len(nasdaq100_otc_records),
                "missing_fields": nasdaq100_otc_missing,
            },
        },
        "cache": {
            "nasdaq100_benchmark": benchmark_cache.stats(),
            "performance": performance_cache.stats(),
            "announcement_indexes": announcement_cache.stats(),
            "contract_profiles": contract_result_cache.stats(),
            "fund_us_equity_exposures": exposure_cache.stats(),
            "quota_notices": quota_notice_cache.stats(),
            "announcement_pdfs": document_cache.stats(),
            "underlying_exposures": resolver.stats(),
            "exchange_premium": {
                "fresh": exchange_premium["fresh_count"],
                "hits": exchange_premium["cache_hit_count"],
                "expected": exchange_premium["expected_count"],
            },
            "exchange_premium_holding_costs": etf_holding_cost_cache.stats(),
        },
        "benchmark": benchmark.metadata(),
        "exchange_premium": exchange_premium,
        "records": records,
        "global_supplement": {
            "ranking_method": global_ranking_method,
            "qualified_count": len(global_quota_qualified),
            "records": global_records,
        },
        "nasdaq100_otc": {
            "selection_method": (
                "从完整基金元数据按名称发现候选；不设置成立年限、收益、额度或申购状态硬门槛，"
                "仅保留场外人民币 A/未标记主份额并排除独立 ETF"
            ),
            "name_match_rule": "纳斯达克100 / 纳指100 / NASDAQ 100 名称匹配",
            "share_class_rule": "OTC RMB A or explicit RMB primary share without C/D/E/F/I marker",
            "exclude_standalone_etf": True,
            "purchasable_only": False,
            "ranking_method": (
                "two_year_return_pct desc, holding_cost.annualized_pct asc, "
                "nasdaq100_fit_2y.tracking_error_pct asc, three_year_return_pct desc, "
                "scale_billion_cny desc, code asc; missing values last"
            ),
            "candidate_count": len(nasdaq100_otc_records),
            "missing_fields": nasdaq100_otc_missing,
            "records": nasdaq100_otc_records,
        },
        "exclusion_summary": [
            {**item, "count": len(item["codes"])} for item in exclusions.values()
        ],
        "warnings": list(dict.fromkeys(warnings)),
        "sources": {
            "fund_list": FUND_LIST_URL,
            "holder_data": HOLDER_API_URL,
            "performance": PERFORMANCE_DATA_URL,
            "nasdaq100_total_return": NASDAQ100_HISTORY_PAGE_URL,
            "usd_cny": SAFE_USD_CNY_HISTORY_URL,
            "announcements": ANNOUNCEMENT_API_URL,
            "periodic_reports": ANNOUNCEMENT_API_URL,
            "legal_documents": ANNOUNCEMENT_API_URL,
            "us_equity_instrument_catalog": str(args.us_equity_catalog.resolve()),
            "contract_benchmark_catalog": str(args.contract_benchmark_catalog.resolve()),
            "us_equity_etf_catalog": (
                str(args.us_equity_etf_catalog.resolve())
                if getattr(args, "us_equity_etf_catalog", None)
                else exchange_premium_market_url()
            ),
            "exchange_etf_catalog": exchange_premium_market_url(),
            "exchange_premium": ETF_QUOTE_PAGE_URL,
        },
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=10, help="Maximum result count")
    parser.add_argument(
        "--min-scale",
        type=float,
        default=None,
        help="Optional strict minimum scale in CNY 100m; omitted by default",
    )
    parser.add_argument(
        "--min-age-years",
        type=int,
        default=3,
        help="Require inception strictly earlier than this many years before --as-of",
    )
    parser.add_argument(
        "--min-three-year-return-pct",
        type=float,
        default=DEFAULT_MIN_THREE_YEAR_RETURN_PCT,
        help="Minimum trailing three-year adjusted return percentage",
    )
    parser.add_argument(
        "--min-five-year-return-pct",
        type=float,
        default=DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
        help="Minimum five-year adjusted return when a complete five-year window exists",
    )
    parser.add_argument(
        "--min-ten-year-return-pct",
        type=float,
        default=DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
        help="Minimum ten-year adjusted return when a complete ten-year window exists",
    )
    parser.add_argument(
        "--min-us-equity-pct",
        type=float,
        default=50.0,
        help="Minimum confirmed US-equity exposure percentage",
    )
    parser.add_argument(
        "--min-direct-limit-cny",
        type=int,
        default=DEFAULT_MIN_DIRECT_LIMIT_CNY,
        help="Inclusive minimum manager direct-sale daily limit in CNY",
    )
    parser.add_argument(
        "--us-main-exclude-keywords",
        "--exclude-keywords",
        dest="us_main_exclude_keywords",
        nargs="*",
        default=DEFAULT_EXCLUDE_KEYWORDS,
        help="Fund-name keywords routed away from the US main list",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / "output" / "qdii-ranking",
        help="Output directory",
    )
    parser.add_argument(
        "--publish-dir",
        type=Path,
        default=Path.cwd() / "public",
        help="Static-site directory that receives index.html",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="Persistent cache directory (defaults to <output-dir>/cache)",
    )
    parser.add_argument(
        "--us-equity-catalog",
        type=Path,
        default=DEFAULT_US_EQUITY_CATALOG,
        help="Underlying instrument classification catalog",
    )
    parser.add_argument(
        "--contract-benchmark-catalog",
        type=Path,
        default=DEFAULT_CONTRACT_BENCHMARK_CATALOG,
        help="Contract benchmark classification catalog",
    )
    parser.add_argument(
        "--us-equity-etf-catalog",
        type=Path,
        default=None,
        help="Optional legacy static ETF catalog; the default discovers all listed QDII funds",
    )
    parser.add_argument("--as-of", help="Evaluation date in YYYY-MM-DD format")
    parser.add_argument(
        "--allow-partial-holder-period",
        action="store_true",
        help="Use the newest holder period even when coverage is below 95%%",
    )
    args = parser.parse_args(argv)
    if args.top <= 0:
        parser.error("--top must be positive")
    if args.min_scale is not None and args.min_scale < 0:
        parser.error("--min-scale must be non-negative")
    if args.min_age_years < 0:
        parser.error("--min-age-years must be non-negative")
    if args.min_five_year_return_pct < -100:
        parser.error("--min-five-year-return-pct must be at least -100")
    if args.min_ten_year_return_pct < -100:
        parser.error("--min-ten-year-return-pct must be at least -100")
    if not 0 <= args.min_us_equity_pct <= 100:
        parser.error("--min-us-equity-pct must be between 0 and 100")
    if args.min_direct_limit_cny < 0:
        parser.error("--min-direct-limit-cny must be non-negative")
    if args.as_of:
        try:
            parse_date(args.as_of)
        except ValueError:
            parser.error("--as-of must use YYYY-MM-DD")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    metrics = RunMetrics()
    client = HttpClient()
    started = time.perf_counter()
    output_dir = args.output_dir.resolve()
    publish_dir = args.publish_dir.resolve()
    try:
        payload = build_payload(args, client, metrics)
        with metrics.phase("artifact_rendering"):
            write_json(output_dir / "latest.json", payload)
            write_csv(output_dir / "latest.csv", payload)
            write_markdown(output_dir / "latest.md", payload)
            write_html(output_dir / "latest.html", payload)
            write_html(publish_dir / "index.html", payload)
            write_json(output_dir / "history" / f"{payload['run_date']}.json", payload)
    except (DataError, OSError, ValueError) as exc:
        try:
            write_json(
                output_dir / "run-metrics.json",
                {
                    "schema_version": 1,
                    "status": "failure",
                    "run_date": (
                        parse_date(args.as_of).isoformat()
                        if args.as_of
                        else current_shanghai_date().isoformat()
                    ),
                    "refresh_seconds": round(time.perf_counter() - started, 3),
                    **metrics.snapshot(),
                    "http": client.metrics(),
                    "error": str(exc),
                },
            )
        except (OSError, ValueError):
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    refresh_seconds = round(time.perf_counter() - started, 3)
    run_metrics = {
        "schema_version": 1,
        "status": "success",
        "run_date": payload["run_date"],
        "generated_at": payload.get("generated_at"),
        "refresh_seconds": refresh_seconds,
        **metrics.snapshot(),
        "http": client.metrics(),
        "cache": payload.get("cache", {}),
        "candidate_counts": {
            key: payload.get("filters", {}).get(key)
            for key in (
                "base_candidates_total",
                "performance_candidates_scanned",
                "performance_qualified_count",
                "contract_candidates_scanned",
                "us_equity_candidates_scanned",
                "us_quota_candidates_scanned",
                "global_quota_candidates_scanned",
            )
        },
        "nasdaq100_otc_candidate_count": len(
            payload.get("nasdaq100_otc", {}).get("records", [])
        ),
    }
    write_json(output_dir / "run-metrics.json", run_metrics)
    print(
        f"Wrote {len(payload['records'])} US records and "
        f"{len(payload['global_supplement']['records'])} global records and "
        f"{len(payload.get('nasdaq100_otc', {}).get('records', []))} OTC Nasdaq-100 records to {output_dir} "
        f"and static site to {publish_dir} in {refresh_seconds:.1f}s"
    )
    for warning in payload["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
