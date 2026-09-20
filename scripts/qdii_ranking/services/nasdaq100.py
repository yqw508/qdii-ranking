"""Common-window evaluation for the OTC Nasdaq-100 display ranking."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..config import (
    NASDAQ100_COMMON_MAX_BOUNDARY_DELAY_DAYS,
    NASDAQ100_COMMON_MIN_AGE_YEARS,
    NASDAQ100_COMMON_MIN_COVERAGE_RATIO,
    NASDAQ100_COMMON_MIN_OBSERVATIONS,
    NASDAQ100_COMMON_MIN_SPAN_DAYS,
)
from ..errors import DataError
from ..models import Nasdaq100Benchmark
from ..sources.performance import (
    calculate_nasdaq100_fit_for_period,
    calculate_period_performance,
    parse_date,
    years_ago,
)

COMMON_FIELDS = {
    "common_period_return_pct": None,
    "common_period_max_drawdown_pct": None,
    "common_period_performance_start_date": None,
    "common_period_performance_end_date": None,
    "nasdaq100_fit_common_period": None,
    "common_period_error": None,
}


def _unavailable_window(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "minimum_anchor_age_years": NASDAQ100_COMMON_MIN_AGE_YEARS,
        "max_boundary_delay_days": NASDAQ100_COMMON_MAX_BOUNDARY_DELAY_DAYS,
        "anchor_inception_date": None,
        "anchor_funds": [],
        "start_date": None,
        "end_date": None,
        "comparable_count": 0,
        "error": reason,
    }


def _fresh_points(
    record: dict[str, Any],
    points: list[dict[str, Any]] | None,
    as_of: date,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not points:
        return None, "本次运行没有可用的复权净值历史。"
    available = [point for point in points if point["date"] <= as_of]
    if not available:
        return None, "排名日及之前没有可用净值。"
    lag_days = (as_of - available[-1]["date"]).days
    if lag_days > NASDAQ100_COMMON_MAX_BOUNDARY_DELAY_DAYS:
        return None, f"最新净值距排名日 {lag_days} 天，超过 7 天上限。"
    return available, None


def _best_shared_date(
    date_sets: list[set[date]],
    candidate_dates: set[date],
    *,
    latest: bool,
) -> date | None:
    if not candidate_dates:
        return None
    coverage = {
        candidate: sum(candidate in dates for dates in date_sets)
        for candidate in candidate_dates
    }
    best_coverage = max(coverage.values())
    best_dates = [
        candidate for candidate, count in coverage.items() if count == best_coverage
    ]
    return max(best_dates) if latest else min(best_dates)


def apply_common_window(
    records: list[dict[str, Any]],
    points_by_code: dict[str, list[dict[str, Any]] | None],
    as_of: date,
    benchmark: Nasdaq100Benchmark,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    cutoff = years_ago(as_of, NASDAQ100_COMMON_MIN_AGE_YEARS)
    mature_records: list[tuple[dict[str, Any], date]] = []
    fresh_records: list[tuple[dict[str, Any], date, list[dict[str, Any]]]] = []
    for record in records:
        record.update(COMMON_FIELDS)
        raw_inception = record.get("inception_date")
        if not raw_inception:
            record["common_period_error"] = "成立日不可用，无法参与公共区间排名。"
            continue
        inception = parse_date(str(raw_inception))
        if inception > cutoff:
            record["common_period_error"] = "成立未满一个自然年，暂不参与公共区间排名。"
            continue
        mature_records.append((record, inception))
        points, error = _fresh_points(record, points_by_code.get(record["code"]), as_of)
        if error is not None or points is None:
            record["common_period_error"] = error
            warnings.append(f"场外纳指100公共区间告警 {record['code']}：{error}")
            continue
        fresh_records.append((record, inception, points))

    if not mature_records:
        reason = "没有成立满一年的场外纳指100候选。"
        warnings.append(f"场外纳指100公共区间告警：{reason}")
        return _unavailable_window(reason), warnings

    anchor_inception = max(item[1] for item in mature_records)
    anchors = [item for item in mature_records if item[1] == anchor_inception]
    anchor_funds = [
        {"code": record["code"], "name": record["name"]}
        for record, _ in anchors
    ]
    limit = anchor_inception + timedelta(days=NASDAQ100_COMMON_MAX_BOUNDARY_DELAY_DAYS)
    start_date_sets = [
        {
            point["date"]
            for point in points
            if anchor_inception <= point["date"] <= limit
        }
        for _, _, points in fresh_records
    ]
    fresh_anchor_codes = {
        record["code"]
        for record, record_inception, _ in fresh_records
        if record_inception == anchor_inception
    }
    anchor_dates = {
        point["date"]
        for record, _ in anchors
        if record["code"] in fresh_anchor_codes
        for point in (points_by_code.get(record["code"]) or [])
        if anchor_inception <= point["date"] <= limit
    }
    candidate_start_dates = anchor_dates or set().union(*start_date_sets)
    start_date = _best_shared_date(
        start_date_sets, candidate_start_dates, latest=False
    )
    if start_date is None:
        start_date = anchor_inception
    comparable = [
        item
        for item in fresh_records
        if any(point["date"] == start_date for point in item[2])
    ]
    for record, _, _ in fresh_records:
        if not any(item[0] is record for item in comparable):
            record["common_period_error"] = f"缺少公共起点 {start_date} 的净值。"
            warnings.append(
                f"场外纳指100公共区间告警 {record['code']}："
                f"{record['common_period_error']}"
            )
    end_floor = as_of - timedelta(days=NASDAQ100_COMMON_MAX_BOUNDARY_DELAY_DAYS)
    end_date_sets = [
        {point["date"] for point in points if end_floor <= point["date"] <= as_of}
        for _, _, points in comparable
    ]
    candidate_end_dates = set().union(*end_date_sets) if end_date_sets else set()
    end_date = _best_shared_date(
        end_date_sets, candidate_end_dates, latest=True
    )
    if end_date is None:
        reason = "可比较基金在排名日前 7 天内没有共同净值日期。"
        for record, _, _ in comparable:
            record["common_period_error"] = reason
        warnings.append(f"场外纳指100公共区间告警：{reason}")
        return {
            **_unavailable_window(reason),
            "anchor_inception_date": anchor_inception.isoformat(),
            "anchor_funds": anchor_funds,
            "start_date": start_date.isoformat(),
        }, warnings

    end_comparable = [
        item
        for item, dates in zip(comparable, end_date_sets)
        if end_date in dates
    ]
    for record, _, _ in comparable:
        if not any(item[0] is record for item in end_comparable):
            record["common_period_error"] = f"缺少公共结束日 {end_date} 的净值。"
            warnings.append(
                f"场外纳指100公共区间告警 {record['code']}："
                f"{record['common_period_error']}"
            )
    completed = 0
    for record, _, points in end_comparable:
        try:
            performance = calculate_period_performance(
                points, record["code"], start_date, end_date
            )
            fit = calculate_nasdaq100_fit_for_period(
                points,
                record["code"],
                benchmark,
                start_date,
                end_date,
                min_observations=NASDAQ100_COMMON_MIN_OBSERVATIONS,
                min_span_days=NASDAQ100_COMMON_MIN_SPAN_DAYS,
                min_coverage_ratio=NASDAQ100_COMMON_MIN_COVERAGE_RATIO,
            )
        except DataError as exc:
            record["common_period_error"] = str(exc)
            warnings.append(f"场外纳指100公共区间告警 {record['code']}：{exc}")
            continue
        record.update(
            {
                "common_period_return_pct": performance["return_pct"],
                "common_period_max_drawdown_pct": performance["max_drawdown_pct"],
                "common_period_performance_start_date": performance["start_date"],
                "common_period_performance_end_date": performance["end_date"],
                "nasdaq100_fit_common_period": fit,
                "common_period_error": None,
            }
        )
        completed += 1

    return {
        "status": "available" if completed else "unavailable",
        "minimum_anchor_age_years": NASDAQ100_COMMON_MIN_AGE_YEARS,
        "max_boundary_delay_days": NASDAQ100_COMMON_MAX_BOUNDARY_DELAY_DAYS,
        "anchor_inception_date": anchor_inception.isoformat(),
        "anchor_funds": anchor_funds,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "comparable_count": completed,
        "error": None if completed else "公共窗口指标均无法计算。",
    }, warnings


__all__ = ["apply_common_window"]
