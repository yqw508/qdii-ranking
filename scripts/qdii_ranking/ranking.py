"""Candidate filtering, routing, and deterministic ranking rules."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Iterable

from .cache.announcements import PeriodicReportCache
from .cache.exposure import FundExposureResultCache
from .cache.performance import PerformanceResultCache
from .config import (
    DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
    DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
    EXCLUDED_FUND_TYPES,
    NASDAQ100_OTC_NAME_RE,
    PERFORMANCE_WORKERS,
    ROUTING_REASON_BELOW_US_THRESHOLD,
    ROUTING_REASON_CONFIRMED_US,
    ROUTING_REASON_GEOGRAPHY_OVERRIDE,
    ROUTING_REASON_LABELS,
)
from .errors import DataError
from .models import Nasdaq100Benchmark
from .runtime import HttpClient, is_older_than_years, parse_date
from .sources.candidates import is_otc_share
from .sources.exposure import LookthroughResolver, fetch_us_equity_exposure
from .sources.performance import fetch_trailing_performance


def contract_mentions_nasdaq100(profile: dict[str, Any]) -> bool:
    searchable = json.dumps(profile, ensure_ascii=False)
    return bool(NASDAQ100_OTC_NAME_RE.search(searchable))


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
