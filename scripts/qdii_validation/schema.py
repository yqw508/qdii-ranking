"""Schema and public record validation."""

import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any
from .otc_shares import validate_share_evidence

from .common import (
    EXPECTED_FILTERS,
    EXPECTED_GLOBAL_RANKING_METHOD,
    EXPECTED_NASDAQ100_OTC_RANKING_METHOD,
    EXPECTED_RANKING_METHOD,
    EXPECTED_US_MAIN_EXCLUDE_KEYWORDS,
    NASDAQ100_MIN_OBSERVATIONS,
    NASDAQ100_MIN_SPAN_DAYS,
    NASDAQ100_OTC_NAME_RE,
    ROUTING_REASON_BELOW_US_THRESHOLD,
    ROUTING_REASON_CONFIRMED_US,
    ROUTING_REASON_GEOGRAPHY_OVERRIDE,
    ValidationError,
    as_number,
    mailer,
    parse_date,
    require,
    validate_three_year_window,
    years_ago,
)

def load_payload(path: Path) -> dict[str, Any]:
    require(path.is_file(), f"Missing artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Could not read {path}: {exc}") from exc
    require(isinstance(payload, dict), "latest.json must contain an object")
    return payload


def validate_filters(filters: Any) -> None:
    require(isinstance(filters, dict), "filters must be an object")
    for key, expected in EXPECTED_FILTERS.items():
        require(filters.get(key) == expected, f"Unexpected filter {key}: {filters.get(key)!r}")
    require(
        set(filters.get("us_main_exclude_keywords", []))
        == EXPECTED_US_MAIN_EXCLUDE_KEYWORDS,
        "US-main fund-name exclusion keywords changed",
    )
    require(
        filters.get("global_exclude_keywords") == [],
        "Global supplement must not exclude fund-name geography keywords",
    )
    require("exclude_keywords" not in filters, "Legacy exclude_keywords filter is still emitted")
    require(filters.get("purchasable_only") is True, "purchasable_only must be true")
    require(filters.get("full_scan_completed") is True, "Full scan was not completed")
    require(
        filters.get("ranking_method") == EXPECTED_RANKING_METHOD,
        "Ranking method is not the Nasdaq-100 correlation rule",
    )
    require(
        filters.get("global_supplement_ranking_method")
        == EXPECTED_GLOBAL_RANKING_METHOD,
        "Global supplement ranking method is unexpected",
    )
    require(
        filters.get("performance_candidates_scanned")
        == filters.get("base_candidates_total"),
        "Performance scan count does not match the base candidate count",
    )
    require(
        filters.get("contract_candidates_scanned")
        == filters.get("performance_qualified_count"),
        "Contract scan count does not match the performance-qualified count",
    )
    require(
        filters.get("us_equity_candidates_scanned")
        == filters.get("performance_qualified_count"),
        "Contract benchmark metadata filtered a performance-qualified candidate",
    )
    require(
        filters.get("us_equity_candidates_scanned")
        == filters.get("us_routed_count") + filters.get("global_routed_count"),
        "US-equity scan count does not close against the routed counts",
    )
    require(
        filters.get("us_quota_candidates_scanned")
        == filters.get("us_routed_count"),
        "US quota scan count does not match the US-routed count",
    )
    require(
        filters.get("global_quota_candidates_scanned")
        == filters.get("global_routed_count"),
        "Global quota scan count does not match the global-routed count",
    )
    require(
        filters.get("exclude_asset_classes") == ["bond", "commodity"],
        "Unexpected global asset exclusions",
    )
    require(
        set(filters.get("exclude_fund_types", []))
        == {"QDII-纯债", "QDII-混合债", "QDII-商品"},
        "Unexpected excluded fund types",
    )


def validate_exclusion_summary(value: Any) -> None:
    require(isinstance(value, list), "exclusion_summary must be a list")
    reasons: set[str] = set()
    for item in value:
        require(isinstance(item, dict), "exclusion_summary items must be objects")
        reason = item.get("reason")
        label = item.get("label")
        codes = item.get("codes")
        require(isinstance(reason, str) and reason, "Exclusion reason is missing")
        require(
            reason != "excluded_target_market",
            "Contract benchmark metadata must not exclude a candidate",
        )
        require(reason not in reasons, f"Duplicate exclusion reason: {reason}")
        reasons.add(reason)
        require(isinstance(label, str) and label, f"Exclusion label is missing for {reason}")
        require(isinstance(codes, list), f"Exclusion codes must be a list for {reason}")
        require(
            all(isinstance(code, str) and re.fullmatch(r"\d{6}", code) for code in codes),
            f"Exclusion codes are invalid for {reason}",
        )
        require(len(codes) == len(set(codes)), f"Duplicate exclusion codes for {reason}")
        require(item.get("count") == len(codes), f"Exclusion count differs for {reason}")


def validate_benchmark(benchmark: Any, run_date: date) -> None:
    require(isinstance(benchmark, dict), "benchmark must be an object")
    require(benchmark.get("symbol") == "XNDX", "Unexpected benchmark symbol")
    require(
        benchmark.get("return_type") == "gross_total_return",
        "Unexpected benchmark return type",
    )
    require(benchmark.get("currency") == "CNY", "Benchmark must be CNY converted")
    require(benchmark.get("window_years") == 3, "Unexpected benchmark window")
    require(benchmark.get("frequency") == "weekly", "Unexpected benchmark frequency")
    require(
        benchmark.get("max_source_staleness_days") == 7,
        "Unexpected benchmark staleness rule",
    )
    require(
        benchmark.get("min_observations") == NASDAQ100_MIN_OBSERVATIONS,
        "Unexpected benchmark observation rule",
    )
    require(
        benchmark.get("min_span_days") == NASDAQ100_MIN_SPAN_DAYS,
        "Unexpected benchmark span rule",
    )
    for field in ("index_source_url", "fx_source_url"):
        require(bool(benchmark.get(field)), f"Benchmark {field} is missing")
    for prefix in ("index", "fx"):
        start = parse_date(benchmark[f"{prefix}_start_date"])
        latest = parse_date(benchmark[f"{prefix}_latest_date"])
        require(start <= latest <= run_date, f"Benchmark {prefix} dates are invalid")
        require(
            (run_date - latest).days <= 7,
            f"Benchmark {prefix} source is stale",
        )


def validate_limit(
    limit: Any, code: str, channel: str, allow_unknown: bool = False
) -> None:
    require(isinstance(limit, dict), f"{code} {channel} limit must be an object")
    status = limit.get("status")
    allowed = {"limited", "unlimited", "suspended"}
    if allow_unknown:
        allowed.add("unknown")
    require(
        status in allowed,
        f"{code} {channel} limit is unresolved",
    )
    if status == "limited":
        amount = limit.get("amount_cny")
        require(
            isinstance(amount, int) and amount > 0,
            f"{code} {channel} limit has no positive amount",
        )
        require(bool(limit.get("source_url")), f"{code} {channel} limit has no source")


def validate_contract_benchmark(
    contract: Any, record: dict[str, Any], run_date: date
) -> None:
    code = record["code"]
    require(isinstance(contract, dict), f"{code} has no contract benchmark")
    for field in ("benchmark_text", "benchmark_name", "market_scope", "market_label"):
        require(bool(contract.get(field)), f"{code} contract benchmark {field} is missing")
    status = contract.get("status")
    require(
        status in {"recognized", "composite", "unrecognized", "unreadable"},
        f"{code} contract benchmark status is invalid",
    )
    components = contract.get("components")
    require(isinstance(components, list), f"{code} contract components are invalid")
    if status == "recognized":
        require(len(components) == 1, f"{code} recognized contract must have one component")
        weight = as_number(contract.get("benchmark_weight_pct"), f"{code} benchmark weight")
        require(0 <= weight <= 100, f"{code} contract benchmark weight is invalid")
        require(bool(contract.get("benchmark_id")), f"{code} recognized benchmark has no id")
    elif status == "composite":
        require(len(components) >= 2, f"{code} composite contract has too few components")
        require(contract.get("benchmark_id") is None, f"{code} composite benchmark id must be null")
    else:
        require(not components, f"{code} unresolved contract has benchmark components")
        require(contract.get("benchmark_weight_pct") is None, f"{code} unresolved benchmark weight must be null")
    for component in components:
        require(isinstance(component, dict), f"{code} contract component is invalid")
        require(bool(component.get("benchmark_id")), f"{code} contract component id is missing")
        weight = as_number(component.get("weight_pct"), f"{code} component weight")
        require(0 <= weight <= 100, f"{code} contract component weight is invalid")
        require(
            isinstance(component.get("excluded_target"), bool),
            f"{code} contract component target flag is invalid",
        )
    require(
        contract.get("structure") in {"standard", "leveraged", "inverse", "volatility"},
        f"{code} contract benchmark structure is invalid",
    )
    require(
        isinstance(contract.get("excluded_target"), bool),
        f"{code} contract target flag is invalid",
    )
    require(
        contract.get("management_style") in {"active", "passive"}
        and record.get("management_style") == contract.get("management_style"),
        f"{code} management style is invalid",
    )
    require(
        contract.get("product_summary_status")
        in {"matched", "missing", "unreadable", "conflict"},
        f"{code} product summary status is invalid",
    )
    if contract.get("prospectus_published_date") is not None:
        published = parse_date(str(contract["prospectus_published_date"]))
        require(published <= run_date, f"{code} uses a future prospectus")
    require(
        re.fullmatch(r"[0-9a-f]{64}", str(contract.get("catalog_fingerprint"))) is not None,
        f"{code} contract benchmark catalog fingerprint is invalid",
    )
    tags = record.get("product_structure_tags")
    require(
        isinstance(tags, list) and tags and all(isinstance(tag, str) and tag for tag in tags),
        f"{code} product structure tags are invalid",
    )


def validate_holding_cost(
    cost: Any, code: str, run_date: date, *, allow_stale: bool = False
) -> None:
    require(isinstance(cost, dict), f"{code} holding cost must be an object")
    statuses = {"parsed", "unavailable"}
    if allow_stale:
        statuses.add("stale")
    require(cost.get("status") in statuses, f"{code} holding cost status is invalid")
    if cost["status"] in {"parsed", "stale"}:
        value = as_number(cost.get("annualized_pct"), f"{code} holding cost")
        require(0 <= value <= 100, f"{code} holding cost is outside [0, 100]")
        require(
            isinstance(cost.get("source_title"), str) and cost["source_title"].strip(),
            f"{code} holding cost has no source title",
        )
        require(
            str(cost.get("source_url") or "").startswith("https://"),
            f"{code} holding cost has no valid source",
        )
        require(
            isinstance(cost.get("source_published_date"), str),
            f"{code} holding cost has no source date",
        )
    else:
        require(cost.get("annualized_pct") is None, f"{code} unavailable holding cost must be null")
        source_values = (
            cost.get("source_title"),
            cost.get("source_published_date"),
            cost.get("source_url"),
        )
        require(
            all(value is None for value in source_values)
            or (
                isinstance(source_values[0], str)
                and bool(source_values[0].strip())
                and isinstance(source_values[1], str)
                and str(source_values[2] or "").startswith("https://")
            ),
            f"{code} unavailable holding-cost source is incomplete",
        )
    for field in ("measurement_date", "source_published_date"):
        if cost.get(field) is not None:
            require(parse_date(str(cost[field])) <= run_date, f"{code} holding cost uses a future date")


def validate_nasdaq_fit(record: dict[str, Any], run_date: date) -> None:
    code = record["code"]
    fit = record.get("nasdaq100_fit")
    require(isinstance(fit, dict), f"{code} has no Nasdaq-100 fit")
    correlation = as_number(fit.get("correlation"), f"{code} correlation")
    beta = as_number(fit.get("beta"), f"{code} beta")
    tracking_error = as_number(fit.get("tracking_error_pct"), f"{code} tracking error")
    require(-1 <= correlation <= 1, f"{code} correlation is outside [-1, 1]")
    require(tracking_error >= 0, f"{code} tracking error is negative")
    require(
        correlation == round(correlation, 4) and beta == round(beta, 4),
        f"{code} correlation or beta exceeds four decimal places",
    )
    require(
        tracking_error == round(tracking_error, 2),
        f"{code} tracking error exceeds two decimal places",
    )
    observations = fit.get("observations")
    require(
        isinstance(observations, int) and observations >= NASDAQ100_MIN_OBSERVATIONS,
        f"{code} has insufficient Nasdaq-100 observations",
    )
    fit_start = parse_date(str(fit.get("start_date")))
    fit_end = parse_date(str(fit.get("end_date")))
    require(fit_start <= fit_end <= run_date, f"{code} Nasdaq-100 fit dates are invalid")
    require(
        (fit_end - fit_start).days >= NASDAQ100_MIN_SPAN_DAYS,
        f"{code} Nasdaq-100 fit span is too short",
    )


def _validate_base_record(
    payload: dict[str, Any],
    record: dict[str, Any],
    expected_rank: int,
    ranking_list: str,
    run_date: date,
    cutoff: date,
) -> tuple[str, str, dict[str, Any], float]:
    code = record.get("code")
    require(
        isinstance(code, str) and re.fullmatch(r"\d{6}", code) is not None,
        f"Record {expected_rank} has an invalid fund code",
    )
    require(record.get("rank") == expected_rank, f"{code} has a non-contiguous rank")
    require(record.get("ranking_list") == ranking_list, f"{code} is in the wrong ranking list")
    name = record.get("name")
    require(isinstance(name, str) and name, f"{code} has no fund name")
    require(
        str(record.get("fund_type", "")).startswith("QDII")
        or record.get("fund_type") == "指数型-海外股票",
        f"{code} is outside the QDII and overseas-index candidate scope",
    )
    require(
        record.get("fund_type") not in {"QDII-纯债", "QDII-混合债", "QDII-商品"},
        f"{code} has an excluded fund type",
    )
    require(record.get("purchase_status") in {"open", "limited"}, f"{code} is not currently purchasable")
    require(as_number(record.get("scale_billion_cny"), f"{code} scale") >= 0, f"{code} has an invalid scale")
    require(parse_date(record["inception_date"]) < cutoff, f"{code} is not strictly older than three years")
    require(
        as_number(record.get("three_year_return_pct"), f"{code} three-year return")
        >= EXPECTED_FILTERS["min_three_year_return_pct"],
        f"{code} does not meet the three-year return threshold",
    )
    for field in ("one_year_return_pct", "one_year_max_drawdown_pct", "three_year_max_drawdown_pct"):
        as_number(record.get(field), f"{code} {field}")
    nav_start = parse_date(str(record.get("nav_history_start_date")))
    nav_end = parse_date(str(record.get("nav_history_end_date")))
    require(nav_start <= nav_end <= run_date, f"{code} NAV history dates are invalid")
    validate_three_year_window(record, run_date)
    if record["three_year_boundary_shortfall_days"]:
        expected_warning = (
            f"三年边界容差 {code}：成立日 {record['inception_date']}；"
            f"{mailer.format_three_year_boundary(record)}。"
        )
        require(expected_warning in payload.get("warnings", []), f"{code} boundary warning is missing")
    for prefix, years in (("five_year", 5), ("ten_year", 10)):
        _validate_long_window(record, code, prefix, years, run_date)
    validate_contract_benchmark(record.get("contract_benchmark"), record, run_date)
    validate_holding_cost(record.get("holding_cost"), code, run_date)
    exposure = record.get("us_equity_exposure")
    require(isinstance(exposure, dict), f"{code} has no US-equity exposure")
    confirmed = as_number(exposure.get("confirmed_pct"), f"{code} confirmed exposure")
    possible = as_number(exposure.get("possible_pct"), f"{code} possible exposure")
    unresolved = as_number(exposure.get("unresolved_pct"), f"{code} unresolved exposure")
    require(0 <= confirmed <= possible <= 100, f"{code} has an invalid exposure interval")
    require(unresolved >= 0, f"{code} has a negative unresolved exposure")
    require(bool(exposure.get("source_url")), f"{code} exposure has no source")
    return code, name, exposure, confirmed


def _validate_long_window(
    record: dict[str, Any], code: str, prefix: str, years: int, run_date: date
) -> None:
    value = record.get(f"{prefix}_return_pct")
    start_value = record.get(f"{prefix}_performance_start_date")
    end_value = record.get(f"{prefix}_performance_end_date")
    if value is None:
        require(start_value is None and end_value is None, f"{code} incomplete {prefix} window has dates")
        return
    numeric_value = as_number(value, f"{code} {prefix} return")
    start = parse_date(str(start_value))
    end = parse_date(str(end_value))
    require(start <= end <= run_date, f"{code} {prefix} dates are invalid")
    require(start <= years_ago(end, years), f"{code} {prefix} window is incomplete")
    threshold_key = f"min_{prefix}_return_pct_if_available"
    require(
        numeric_value >= EXPECTED_FILTERS[threshold_key],
        f"{code} does not meet the conditional {prefix} return threshold",
    )


def _validate_routing(
    record: dict[str, Any],
    ranking_list: str,
    run_date: date,
    code: str,
    name: str,
    exposure: dict[str, Any],
    confirmed: float,
) -> None:
    routing_reason = record.get("routing_reason")
    has_keyword = any(keyword in name for keyword in EXPECTED_US_MAIN_EXCLUDE_KEYWORDS)
    if ranking_list == "us_main":
        require(routing_reason == ROUTING_REASON_CONFIRMED_US, f"{code} has an invalid US-main routing reason")
        require(not has_keyword, f"{code} has a geography keyword and cannot enter the US main list")
        require(confirmed >= EXPECTED_FILTERS["min_us_equity_pct"], f"{code} does not meet the confirmed US-equity threshold")
        require(exposure.get("status") == "qualified", f"{code} exposure is not qualified")
        validate_nasdaq_fit(record, run_date)
        return
    if routing_reason == ROUTING_REASON_GEOGRAPHY_OVERRIDE:
        require(has_keyword, f"{code} geography-override routing has no matching name keyword")
    elif routing_reason == ROUTING_REASON_BELOW_US_THRESHOLD:
        require(not has_keyword, f"{code} geography keyword requires geography-override routing")
        require(confirmed < EXPECTED_FILTERS["min_us_equity_pct"], f"{code} global record meets the US-main exposure threshold without an override")
    else:
        raise ValidationError(f"{code} has an invalid global routing reason")
    annualized = as_number(record.get("three_year_annualized_return_pct"), f"{code} annualized return")
    start = parse_date(record["three_year_performance_start_date"])
    end = parse_date(record["three_year_performance_end_date"])
    expected = ((1 + float(record["three_year_return_pct"]) / 100) ** (365 / (end - start).days) - 1) * 100
    require(math.isclose(annualized, round(expected, 2), abs_tol=1e-9), f"{code} annualized return differs from its three-year return")
    drawdown = abs(float(record["three_year_max_drawdown_pct"]))
    score = record.get("return_drawdown_ratio")
    if drawdown == 0:
        require(score is None, f"{code} zero-drawdown ratio must be null")
    else:
        require(
            math.isclose(as_number(score, f"{code} return/drawdown ratio"), round(expected / drawdown, 4), abs_tol=1e-9),
            f"{code} return/drawdown ratio differs",
        )


def _validate_quota(record: dict[str, Any], code: str) -> None:
    validate_limit(record.get("direct_limit"), code, "direct")
    validate_limit(record.get("agency_limit"), code, "agency", allow_unknown=True)
    direct = record["direct_limit"]
    require(
        direct["status"] == "unlimited"
        or int(direct.get("amount_cny") or 0) >= EXPECTED_FILTERS["min_direct_limit_cny_inclusive"],
        f"{code} does not meet the inclusive direct-sale limit threshold",
    )
    sources = record.get("quota_source_urls")
    require(isinstance(sources, list) and all(isinstance(source, str) and source for source in sources), f"{code} quota source list is invalid")
    if record.get("quota_status") == "limited":
        require(sources, f"{code} limited quota has no announcement source")
    for channel in ("direct_limit", "agency_limit"):
        source_url = record[channel].get("source_url")
        if source_url and source_url != record.get("fund_page_url"):
            require(source_url in sources, f"{code} {channel} source is absent from quota_source_urls")


def _record_sort_key(item: dict[str, Any], ranking_list: str) -> tuple[Any, ...]:
    if ranking_list == "us_main":
        return (
            -float(item["nasdaq100_fit"]["correlation"]),
            abs(float(item["nasdaq100_fit"]["beta"]) - 1),
            -float(item["us_equity_exposure"]["confirmed_pct"]),
            -float(item["institution_holding_ratio_pct"]),
            -float(item["three_year_return_pct"]),
            item["code"],
        )
    return (
        float("-inf") if item["return_drawdown_ratio"] is None else -float(item["return_drawdown_ratio"]),
        -float(item["three_year_return_pct"]),
        -float(item["three_year_max_drawdown_pct"]),
        -float(item["institution_holding_ratio_pct"]),
        -float(item["scale_billion_cny"]),
        item["code"],
    )


def validate_records(
    payload: dict[str, Any], records: Any, ranking_list: str
) -> list[dict[str, Any]]:
    require(isinstance(records, list), f"{ranking_list} records must be a list")
    require(len(records) <= EXPECTED_FILTERS["top"], f"{ranking_list} contains more than ten records")
    run_date = parse_date(payload["run_date"])
    cutoff = years_ago(run_date, EXPECTED_FILTERS["min_age_years"])
    codes: list[str] = []
    for expected_rank, record in enumerate(records, start=1):
        require(isinstance(record, dict), f"Record {expected_rank} must be an object")
        code, name, exposure, confirmed = _validate_base_record(
            payload, record, expected_rank, ranking_list, run_date, cutoff
        )
        codes.append(code)
        _validate_routing(record, ranking_list, run_date, code, name, exposure, confirmed)
        _validate_quota(record, code)
    require(len(codes) == len(set(codes)), "Ranking contains duplicate fund codes")
    expected_order = sorted(records, key=lambda item: _record_sort_key(item, ranking_list))
    require(
        [item["code"] for item in records] == [item["code"] for item in expected_order],
        f"{ranking_list} order does not match its sort rule",
    )
    return records



def validate_nasdaq100_otc_section(section: Any, run_date: date) -> list[dict[str, Any]]:
    require(isinstance(section, dict), "nasdaq100_otc must be an object")
    records = section.get("records")
    require(isinstance(records, list), "nasdaq100_otc records must be a list")
    require(section.get("candidate_count") == len(records), "OTC Nasdaq-100 candidate count differs")
    require(len({item.get("code") for item in records}) == len(records), "OTC Nasdaq-100 codes are duplicated")
    require(section.get("ranking_method") == EXPECTED_NASDAQ100_OTC_RANKING_METHOD, "OTC Nasdaq-100 ranking method differs")
    window = section.get("comparison_window")
    require(isinstance(window, dict), "OTC Nasdaq-100 comparison window is missing")
    require(window.get("status") in {"available", "unavailable"}, "OTC Nasdaq-100 comparison window status is invalid")
    require(window.get("minimum_anchor_age_years") == 1, "OTC Nasdaq-100 anchor age differs")
    require(window.get("max_boundary_delay_days") == 7, "OTC Nasdaq-100 boundary delay differs")
    require(isinstance(window.get("anchor_funds"), list), "OTC Nasdaq-100 anchor funds are invalid")
    window_start = window_end = None
    if window.get("status") == "available":
        window_start = parse_date(window.get("start_date"))
        window_end = parse_date(window.get("end_date"))
        require(window_start < window_end <= run_date, "OTC Nasdaq-100 comparison dates are invalid")
        require((run_date - window_end).days <= 7, "OTC Nasdaq-100 comparison end is stale")
        anchor_inception = parse_date(window.get("anchor_inception_date"))
        require(
            anchor_inception <= window_start
            and (window_start - anchor_inception).days <= 7,
            "OTC Nasdaq-100 comparison start differs from its anchor",
        )
    for expected_rank, record in enumerate(records, start=1):
        require(isinstance(record, dict), "OTC Nasdaq-100 record must be an object")
        code = record.get("code")
        name = record.get("name")
        require(isinstance(code, str) and re.fullmatch(r"\d{6}", code), f"{code} has an invalid fund code")
        require(record.get("rank") == expected_rank, f"{code} has a non-contiguous OTC Nasdaq-100 rank")
        require(record.get("ranking_list") == "nasdaq100_otc", f"{code} has an invalid OTC Nasdaq-100 list")
        require(isinstance(name, str) and NASDAQ100_OTC_NAME_RE.search(name), f"{code} does not match the OTC Nasdaq-100 name rule")
        require(
            str(record.get("fund_type", "")).startswith("QDII")
            or record.get("fund_type") == "指数型-海外股票",
            f"{code} is outside the QDII candidate scope",
        )
        require(record.get("purchase_status") in {"open", "limited", "suspended", "unknown"}, f"{code} has an invalid purchase status")
        validate_share_evidence(record, run_date)
        if "ETF" in name.upper():
            require("联接" in name or "LOF" in name.upper(), f"{code} is a standalone ETF")
        inception = record.get("inception_date")
        if inception is not None:
            require(parse_date(inception) <= run_date, f"{code} has a future inception date")
        for field in (
            "two_year_return_pct",
            "two_year_max_drawdown_pct",
            "three_year_return_pct",
            "common_period_return_pct",
            "common_period_max_drawdown_pct",
            "scale_billion_cny",
        ):
            if record.get(field) is not None:
                as_number(record[field], f"{code} {field}")
        cost = record.get("holding_cost")
        require(isinstance(cost, dict), f"{code} has no holding cost object")
        if cost.get("annualized_pct") is not None:
            as_number(cost["annualized_pct"], f"{code} holding cost")
        if record.get("contract_name_match") is False:
            require(bool(record.get("contract_name_match_warning")), f"{code} contract/name mismatch has no warning")
        fit = record.get("nasdaq100_fit_2y")
        if fit is not None:
            require(isinstance(fit, dict), f"{code} has an invalid two-year Nasdaq fit")
            as_number(fit.get("tracking_error_pct"), f"{code} two-year tracking error")
        common_fit = record.get("nasdaq100_fit_common_period")
        if record.get("common_period_return_pct") is None:
            require(bool(record.get("common_period_error")), f"{code} has no common-period error")
            require(common_fit is None, f"{code} has a fit without common-period return")
        else:
            require(window_start is not None and window_end is not None, f"{code} has metrics without a window")
            require(record.get("common_period_error") is None, f"{code} has contradictory common-period status")
            require(
                record.get("common_period_performance_start_date") == window["start_date"]
                and record.get("common_period_performance_end_date") == window["end_date"],
                f"{code} common-period dates differ from the section window",
            )
            require(isinstance(common_fit, dict), f"{code} has no common-period Nasdaq fit")
            for field in ("correlation", "beta", "tracking_error_pct", "observations"):
                as_number(common_fit.get(field), f"{code} common-period {field}")
            require(
                common_fit.get("start_date") >= window["start_date"]
                and common_fit.get("end_date") <= window["end_date"],
                f"{code} common-period fit dates exceed the window",
            )
    def key(item: dict[str, Any]) -> tuple[Any, ...]:
        fit = item.get("nasdaq100_fit_common_period") or {}
        return (
            math.inf if item.get("common_period_return_pct") is None else -float(item["common_period_return_pct"]),
            math.inf if item["holding_cost"].get("annualized_pct") is None else float(item["holding_cost"]["annualized_pct"]),
            math.inf if fit.get("tracking_error_pct") is None else float(fit["tracking_error_pct"]),
            math.inf if item.get("common_period_max_drawdown_pct") is None else -float(item["common_period_max_drawdown_pct"]),
            math.inf if item.get("scale_billion_cny") is None else -float(item["scale_billion_cny"]),
            item["code"],
        )
    require(records == sorted(records, key=key), "OTC Nasdaq-100 records are not sorted")
    missing = section.get("missing_fields")
    require(isinstance(missing, dict), "OTC Nasdaq-100 missing field counts are absent")
    expected_missing = {
        "common_period_return": sum(item.get("common_period_return_pct") is None for item in records),
        "common_period_max_drawdown": sum(item.get("common_period_max_drawdown_pct") is None for item in records),
        "nasdaq100_fit_common_period": sum(item.get("nasdaq100_fit_common_period") is None for item in records),
        "inception_date": sum(item.get("inception_date") is None for item in records),
    }
    for field, count in expected_missing.items():
        require(missing.get(field) == count, f"OTC Nasdaq-100 missing count differs for {field}")
    require(
        window.get("comparable_count") == len(records) - expected_missing["common_period_return"],
        "OTC Nasdaq-100 comparable count differs",
    )
    return records
