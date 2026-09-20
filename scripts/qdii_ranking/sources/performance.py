"""NAV-history parsing and performance calculations."""

from __future__ import annotations

import bisect
import json
import math
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

from send_qdii_email import format_three_year_boundary

from ..config import (
    BENCHMARK_MAX_STALENESS_DAYS,
    BENCHMARK_WINDOW_YEARS,
    NASDAQ100_MIN_OBSERVATIONS,
    NASDAQ100_MIN_SPAN_DAYS,
    NASDAQ100_TWO_YEAR_MIN_OBSERVATIONS,
    NASDAQ100_TWO_YEAR_MIN_SPAN_DAYS,
    NASDAQ100_TWO_YEAR_WINDOW_YEARS,
    PERFORMANCE_DATA_URL,
    THREE_YEAR_BOUNDARY_TOLERANCE_DAYS,
)
from ..errors import DataError

SHANGHAI_TZ = timezone(timedelta(hours=8))


def parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def is_older_than_years(inception_date: str, as_of: date, years: int) -> bool:
    return parse_date(inception_date) < years_ago(as_of, years)


def parse_performance_page(payload: str, code: str) -> list[dict[str, Any]]:
    match = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", payload, re.S)
    if not match:
        raise DataError(f"Could not locate net-worth trend for fund {code}")
    try:
        raw_points = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise DataError(f"Could not parse net-worth trend for fund {code}: {exc}") from exc

    points_by_date: dict[date, dict[str, Any]] = {}
    for item in raw_points:
        try:
            observed = datetime.fromtimestamp(float(item["x"]) / 1000, SHANGHAI_TZ).date()
            nav = float(item["y"])
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise DataError(f"Invalid net-worth point for fund {code}: {exc}") from exc
        if not math.isfinite(nav) or nav <= 0:
            raise DataError(f"Invalid net asset value for fund {code} on {observed}")
        equity_return = item.get("equityReturn")
        try:
            equity_return_pct = (
                float(equity_return) if equity_return not in (None, "") else None
            )
        except (TypeError, ValueError) as exc:
            raise DataError(f"Invalid daily return for fund {code} on {observed}") from exc
        points_by_date[observed] = {
            "date": observed,
            "nav": nav,
            "equity_return_pct": equity_return_pct,
            "unit_money": str(item.get("unitMoney") or ""),
        }
    if not points_by_date:
        raise DataError(f"No net-worth history was returned for fund {code}")
    return [points_by_date[key] for key in sorted(points_by_date)]


def adjusted_daily_factor(
    previous: dict[str, Any], current: dict[str, Any], code: str
) -> float:
    unit_money = current["unit_money"]
    if unit_money:
        dividend = re.search(r"派现金\s*([\d.]+)\s*元", unit_money)
        if dividend:
            factor = (current["nav"] + float(dividend.group(1))) / previous["nav"]
        elif current["equity_return_pct"] is not None:
            factor = 1 + current["equity_return_pct"] / 100
        else:
            raise DataError(
                f"Could not adjust net-worth event for fund {code} on {current['date']}"
            )
    else:
        factor = current["nav"] / previous["nav"]
    if not math.isfinite(factor) or factor <= 0:
        raise DataError(f"Invalid adjusted return factor for fund {code} on {current['date']}")
    return factor


def build_adjusted_wealth_series(
    points: list[dict[str, Any]], code: str, as_of: date
) -> list[tuple[date, float]]:
    available = [point for point in points if point["date"] <= as_of]
    if not available:
        raise DataError(f"No net-worth history on or before {as_of} for fund {code}")
    wealth = 1.0
    series = [(available[0]["date"], wealth)]
    for previous, current in zip(available, available[1:]):
        wealth *= adjusted_daily_factor(previous, current, code)
        if not math.isfinite(wealth) or wealth <= 0:
            raise DataError(f"Invalid adjusted wealth for fund {code} on {current['date']}")
        series.append((current["date"], wealth))
    return series


def latest_series_value(
    series: dict[date, float], ordered_dates: list[date], observed: date
) -> tuple[date, float] | None:
    index = bisect.bisect_right(ordered_dates, observed) - 1
    if index < 0:
        return None
    source_date = ordered_dates[index]
    if (observed - source_date).days > BENCHMARK_MAX_STALENESS_DAYS:
        return None
    return source_date, series[source_date]


def calculate_nasdaq100_fit(
    points: list[dict[str, Any]],
    code: str,
    as_of: date,
    benchmark: Nasdaq100Benchmark,
    *,
    window_years: int = BENCHMARK_WINDOW_YEARS,
    min_observations: int = NASDAQ100_MIN_OBSERVATIONS,
    min_span_days: int = NASDAQ100_MIN_SPAN_DAYS,
) -> dict[str, Any]:
    wealth_series = build_adjusted_wealth_series(points, code, as_of)
    end_date = wealth_series[-1][0]
    target_start = years_ago(end_date, window_years)
    return _calculate_nasdaq100_fit_from_wealth(
        wealth_series,
        code,
        benchmark,
        target_start,
        end_date,
        min_observations,
        min_span_days,
    )


def _calculate_nasdaq100_fit_from_wealth(
    wealth_series: list[tuple[date, float]],
    code: str,
    benchmark: Nasdaq100Benchmark,
    start_date: date,
    end_date: date,
    min_observations: int,
    min_span_days: int,
    min_coverage_ratio: float | None = None,
) -> dict[str, Any]:
    requested_start_date = start_date
    weekly: dict[date, tuple[date, float]] = {}
    for observed, wealth in wealth_series:
        if observed < start_date or observed > end_date:
            continue
        week_start = observed - timedelta(days=observed.weekday())
        weekly[week_start] = (observed, wealth)

    xndx_dates = sorted(benchmark.xndx_levels)
    fx_dates = sorted(benchmark.usd_cny_rates)
    aligned: list[tuple[date, date, float, float]] = []
    for week_start in sorted(weekly):
        observed, wealth = weekly[week_start]
        xndx = latest_series_value(benchmark.xndx_levels, xndx_dates, observed)
        fx = latest_series_value(benchmark.usd_cny_rates, fx_dates, observed)
        if xndx is None or fx is None:
            continue
        benchmark_level = xndx[1] * fx[1]
        if not math.isfinite(benchmark_level) or benchmark_level <= 0:
            raise DataError(f"Invalid CNY XNDX benchmark level for {code} on {observed}")
        aligned.append((week_start, observed, wealth, benchmark_level))

    fund_returns: list[float] = []
    benchmark_returns: list[float] = []
    interval_dates: list[tuple[date, date]] = []
    for previous, current in zip(aligned, aligned[1:]):
        if (current[0] - previous[0]).days != 7:
            continue
        fund_return = current[2] / previous[2] - 1
        benchmark_return = current[3] / previous[3] - 1
        if not math.isfinite(fund_return) or not math.isfinite(benchmark_return):
            raise DataError(f"Non-finite weekly return for fund {code}")
        fund_returns.append(fund_return)
        benchmark_returns.append(benchmark_return)
        interval_dates.append((previous[1], current[1]))

    observations = len(fund_returns)
    if observations < min_observations:
        raise DataError(
            f"Fund {code} has only {observations} valid Nasdaq-100 weekly observations; "
            f"requires {min_observations}"
        )
    start_date = interval_dates[0][0]
    fit_end_date = interval_dates[-1][1]
    span_days = (fit_end_date - start_date).days
    if span_days < min_span_days:
        raise DataError(
            f"Fund {code} Nasdaq-100 fit spans only {span_days} days; "
            f"requires {min_span_days}"
        )
    if min_coverage_ratio is not None:
        expected_observations = max(
            1, (end_date - requested_start_date).days // 7
        )
        coverage_ratio = observations / expected_observations
        if coverage_ratio < min_coverage_ratio:
            raise DataError(
                f"Fund {code} Nasdaq-100 fit covers only {coverage_ratio:.1%} of "
                f"the common weekly window; requires {min_coverage_ratio:.0%}"
            )

    fund_mean = statistics.mean(fund_returns)
    benchmark_mean = statistics.mean(benchmark_returns)
    covariance = sum(
        (fund_return - fund_mean) * (benchmark_return - benchmark_mean)
        for fund_return, benchmark_return in zip(fund_returns, benchmark_returns)
    ) / (observations - 1)
    fund_variance = statistics.variance(fund_returns)
    benchmark_variance = statistics.variance(benchmark_returns)
    if fund_variance <= 0 or benchmark_variance <= 0:
        raise DataError(f"Fund {code} Nasdaq-100 fit has zero return variance")
    correlation = covariance / math.sqrt(fund_variance * benchmark_variance)
    correlation = min(1.0, max(-1.0, correlation))
    beta = covariance / benchmark_variance
    tracking_error_pct = statistics.stdev(
        fund_return - benchmark_return
        for fund_return, benchmark_return in zip(fund_returns, benchmark_returns)
    ) * math.sqrt(52) * 100
    if not all(math.isfinite(value) for value in (correlation, beta, tracking_error_pct)):
        raise DataError(f"Fund {code} Nasdaq-100 fit contains a non-finite metric")
    return {
        "correlation": round(correlation, 4),
        "beta": round(beta, 4),
        "tracking_error_pct": round(tracking_error_pct, 2),
        "observations": observations,
        "start_date": start_date.isoformat(),
        "end_date": fit_end_date.isoformat(),
    }


def calculate_period_performance(
    points: list[dict[str, Any]], code: str, start_date: date, end_date: date
) -> dict[str, Any]:
    """Calculate adjusted cumulative return and drawdown on exact endpoints."""
    window = [point for point in points if start_date <= point["date"] <= end_date]
    if not window or window[0]["date"] != start_date or window[-1]["date"] != end_date:
        raise DataError(
            f"Fund {code} lacks exact NAV endpoints for {start_date} to {end_date}"
        )
    wealth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for previous, current in zip(window, window[1:]):
        wealth *= adjusted_daily_factor(previous, current, code)
        peak = max(peak, wealth)
        max_drawdown = min(max_drawdown, wealth / peak - 1)
    return {
        "return_pct": round((wealth - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def calculate_nasdaq100_fit_for_period(
    points: list[dict[str, Any]],
    code: str,
    benchmark: Nasdaq100Benchmark,
    start_date: date,
    end_date: date,
    *,
    min_observations: int,
    min_span_days: int,
    min_coverage_ratio: float,
) -> dict[str, Any]:
    wealth_series = build_adjusted_wealth_series(points, code, end_date)
    return _calculate_nasdaq100_fit_from_wealth(
        wealth_series,
        code,
        benchmark,
        start_date,
        end_date,
        min_observations,
        min_span_days,
        min_coverage_ratio,
    )


def calculate_trailing_performance(
    points: list[dict[str, Any]], code: str, as_of: date, years: int,
    *, inception_date: str | None = None,
) -> dict[str, Any] | None:
    available = [point for point in points if point["date"] <= as_of]
    if not available:
        raise DataError(f"No net-worth history on or before {as_of} for fund {code}")
    end_point = available[-1]
    target_start = years_ago(end_point["date"], years)
    anchor_indexes = [
        index for index, point in enumerate(available) if point["date"] <= target_start
    ]
    shortfall_days = 0
    if anchor_indexes:
        anchor_index = anchor_indexes[-1]
    else:
        shortfall_days = (available[0]["date"] - target_start).days
        if not (
            years == 3
            and inception_date is not None
            and parse_date(inception_date) == available[0]["date"]
            and is_older_than_years(inception_date, as_of, 3)
            and 1 <= shortfall_days <= THREE_YEAR_BOUNDARY_TOLERANCE_DAYS
        ):
            return None
        anchor_index = 0
    window = available[anchor_index:]
    wealth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for previous, current in zip(window, window[1:]):
        wealth *= adjusted_daily_factor(previous, current, code)
        peak = max(peak, wealth)
        max_drawdown = min(max_drawdown, wealth / peak - 1)
    return {
        "return_pct": round((wealth - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "start_date": window[0]["date"].isoformat(),
        "end_date": window[-1]["date"].isoformat(),
        "boundary_shortfall_days": shortfall_days,
    }


def calculate_performance_from_points(
    points: list[dict[str, Any]],
    code: str,
    as_of: date,
    source_url: str,
    benchmark: Nasdaq100Benchmark | None = None,
    *, inception_date: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    output: dict[str, Any] = {
        "performance_source_url": source_url,
        "three_year_boundary_shortfall_days": 0,
    }
    warnings: list[str] = []
    output["nav_history_start_date"] = points[0]["date"].isoformat()
    output["nav_history_end_date"] = max(
        (point["date"] for point in points if point["date"] <= as_of),
        default=points[0]["date"],
    ).isoformat()
    for years, prefix, label in (
        (1, "one_year", "近一年"),
        (2, "two_year", "近两年"),
        (3, "three_year", "近三年"),
        (5, "five_year", "近五年"),
        (10, "ten_year", "近十年"),
    ):
        performance = calculate_trailing_performance(
            points, code, as_of, years, inception_date=inception_date
        )
        if performance is None:
            output.update(
                {
                    f"{prefix}_return_pct": None,
                    f"{prefix}_max_drawdown_pct": None,
                    f"{prefix}_performance_start_date": None,
                    f"{prefix}_performance_end_date": None,
                }
            )
            if years == 3:
                warnings.append(
                    f"{code} 的净值历史不足 {years} 年，{label}涨幅和最大回撤无法计算。"
                )
            continue
        output.update(
            {
                f"{prefix}_return_pct": performance["return_pct"],
                f"{prefix}_max_drawdown_pct": performance["max_drawdown_pct"],
                f"{prefix}_performance_start_date": performance["start_date"],
                f"{prefix}_performance_end_date": performance["end_date"],
            }
        )
        if years == 3:
            output["three_year_boundary_shortfall_days"] = performance["boundary_shortfall_days"]
            if performance["boundary_shortfall_days"]:
                warnings.append(
                    f"三年边界容差 {code}：成立日 {inception_date}；"
                    f"{format_three_year_boundary(output)}。"
                )
    if benchmark is not None:
        try:
            output["nasdaq100_fit"] = calculate_nasdaq100_fit(
                points, code, as_of, benchmark
            )
            output["nasdaq100_fit_error"] = None
        except DataError as exc:
            output["nasdaq100_fit"] = None
            output["nasdaq100_fit_error"] = str(exc)
        try:
            output["nasdaq100_fit_2y"] = calculate_nasdaq100_fit(
                points,
                code,
                as_of,
                benchmark,
                window_years=NASDAQ100_TWO_YEAR_WINDOW_YEARS,
                min_observations=NASDAQ100_TWO_YEAR_MIN_OBSERVATIONS,
                min_span_days=NASDAQ100_TWO_YEAR_MIN_SPAN_DAYS,
            )
            output["nasdaq100_fit_2y_error"] = None
        except DataError as exc:
            output["nasdaq100_fit_2y"] = None
            output["nasdaq100_fit_2y_error"] = str(exc)
    return output, warnings


def fetch_trailing_performance(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    benchmark: Nasdaq100Benchmark | None = None,
) -> tuple[dict[str, Any], list[str]]:
    code = fund["code"]
    url = PERFORMANCE_DATA_URL.format(code=code, cache_buster=as_of.strftime("%Y%m%d"))
    payload = client.get_text(url, referer=fund["fund_page_url"])
    return calculate_performance_from_points(
        parse_performance_page(payload, code), code, as_of, url, benchmark,
        inception_date=fund.get("inception_date"),
    )
