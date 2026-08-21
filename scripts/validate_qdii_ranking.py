#!/usr/bin/env python3
"""Validate generated QDII ranking artifacts and an optional deployment."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import send_qdii_email as mailer


SHANGHAI_TZ = timezone(timedelta(hours=8))
EXPECTED_FILTERS = {
    "top": 10,
    "min_scale_billion_cny": 3.0,
    "min_age_years": 3,
    "min_three_year_return_pct": 50.0,
    "min_us_equity_pct": 50.0,
    "min_direct_limit_cny_inclusive": 200,
}
EXPECTED_EXCLUDE_KEYWORDS = {"亚洲", "中国", "港"}
EXPECTED_RANKING_METHOD = (
    "nasdaq100_correlation desc, abs(nasdaq100_beta - 1) asc, "
    "us_equity_confirmed_pct desc, institution_holding_ratio_pct desc, "
    "three_year_return_pct desc, code asc"
)
EXPECTED_GLOBAL_RANKING_METHOD = (
    "three_year_return_drawdown_ratio desc, three_year_return_pct desc, "
    "three_year_max_drawdown_pct desc, institution_holding_ratio_pct desc, "
    "scale_billion_cny desc, code asc"
)
NASDAQ100_MIN_OBSERVATIONS = 140
NASDAQ100_MIN_SPAN_DAYS = 1000
MARKDOWN_ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|\s*\[(.+)\s+(\d{6})\]\([^)]+\)\s*\|"
)


class ValidationError(RuntimeError):
    """Raised when an artifact cannot be published safely."""


class RankingHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.codes: list[str] = []
        self.lists: list[str] = []
        self.blocks: dict[str, str] = {}
        self.all_text: list[str] = []
        self._current_code: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "details" and "fund-item" in classes:
            code = attributes.get("data-code")
            if not code or self._current_code is not None:
                raise ValidationError("HTML contains an invalid nested fund record")
            self._current_code = code
            self.lists.append(attributes.get("data-list") or "")
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "details" and self._current_code is not None:
            text = " ".join(" ".join(self._current_text).split())
            self.codes.append(self._current_code)
            self.blocks[self._current_code] = text
            self._current_code = None
            self._current_text = []

    def handle_data(self, data: str) -> None:
        self.all_text.append(data)
        if self._current_code is not None:
            self._current_text.append(data)


def current_shanghai_date() -> str:
    return datetime.now(SHANGHAI_TZ).date().isoformat()


def parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def as_number(value: Any, label: str) -> float:
    require(isinstance(value, (int, float)), f"{label} must be numeric")
    number = float(value)
    require(math.isfinite(number), f"{label} must be finite")
    return number


def format_percentage(value: Any, show_sign: bool = False) -> str:
    number = as_number(value, "percentage")
    return f"{number:+.2f}%" if show_sign else f"{number:.2f}%"


def format_correlation(value: Any) -> str:
    return f"{as_number(value, 'correlation') * 100:.1f}%"


def format_beta(value: Any) -> str:
    return f"{as_number(value, 'beta'):.2f}"


def format_limit(limit: dict[str, Any]) -> str:
    status = limit.get("status")
    if status == "unlimited":
        return "正常开放"
    if status == "suspended":
        return "暂停申购"
    amount = limit.get("amount_cny")
    if status == "unknown" or amount is None:
        return "待核实"
    amount = int(amount)
    if amount >= 10000 and amount % 10000 == 0:
        return f"{amount // 10000:,}万元"
    return f"{amount:,}元"


def is_reportable_warning(warning: str) -> bool:
    if warning.startswith("跳过未完整披露的持有人报告期"):
        return True
    if "无法按 " in warning and "仓位仅计入可能上限" in warning:
        return True
    if "前十大基金之外尚有" in warning and "仅计入可能上限" in warning:
        return True
    if "美股占比区间" in warning and "按确认下限进入全球补充榜" in warning:
        return True
    if warning.startswith("纳指100基准更新失败，使用完整缓存："):
        return True
    if warning.startswith(
        ("合同基准告警 ", "产品概要告警 ", "持有费率告警 ", "额度剔除 ")
    ):
        return True
    if "的申购额度无法确定；引用前请核对关联公告" in warning:
        return True
    if "quota notice could not be parsed" in warning:
        return True
    if warning.startswith(("美国主榜仅 ", "全球补充榜仅 ")):
        return True
    if warning in {
        "美国主榜当前没有符合全部条件的基金。",
        "全球补充榜当前没有符合全部条件的基金。",
    }:
        return True
    return False


def classify_warnings(warnings: Any) -> tuple[list[str], list[str]]:
    require(isinstance(warnings, list), "warnings must be a list")
    require(
        all(isinstance(warning, str) and warning.strip() for warning in warnings),
        "warnings must contain non-empty strings",
    )
    reportable = [warning for warning in warnings if is_reportable_warning(warning)]
    blocking = [warning for warning in warnings if not is_reportable_warning(warning)]
    return reportable, blocking


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
        set(filters.get("exclude_keywords", [])) == EXPECTED_EXCLUDE_KEYWORDS,
        "Fund-name exclusion keywords changed",
    )
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
        contract.get("structure") in {"standard", "leveraged", "inverse", "volatility"},
        f"{code} contract benchmark structure is invalid",
    )
    require(contract.get("excluded_target") is False, f"{code} targets an excluded market")
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


def validate_holding_cost(cost: Any, code: str, run_date: date) -> None:
    require(isinstance(cost, dict), f"{code} holding cost must be an object")
    require(cost.get("status") in {"parsed", "unavailable"}, f"{code} holding cost status is invalid")
    if cost["status"] == "parsed":
        value = as_number(cost.get("annualized_pct"), f"{code} holding cost")
        require(0 <= value <= 100, f"{code} holding cost is outside [0, 100]")
        require(bool(cost.get("source_url")), f"{code} holding cost has no source")
    else:
        require(cost.get("annualized_pct") is None, f"{code} unavailable holding cost must be null")
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


def validate_records(
    payload: dict[str, Any], records: Any, ranking_list: str
) -> list[dict[str, Any]]:
    require(isinstance(records, list), f"{ranking_list} records must be a list")
    require(
        len(records) <= EXPECTED_FILTERS["top"],
        f"{ranking_list} contains more than ten records",
    )
    run_date = parse_date(payload["run_date"])
    cutoff = years_ago(run_date, EXPECTED_FILTERS["min_age_years"])
    codes: list[str] = []

    for expected_rank, record in enumerate(records, start=1):
        require(isinstance(record, dict), f"Record {expected_rank} must be an object")
        code = record.get("code")
        require(
            isinstance(code, str) and re.fullmatch(r"\d{6}", code) is not None,
            f"Record {expected_rank} has an invalid fund code",
        )
        require(record.get("rank") == expected_rank, f"{code} has a non-contiguous rank")
        require(record.get("ranking_list") == ranking_list, f"{code} is in the wrong ranking list")
        codes.append(code)
        name = record.get("name")
        require(isinstance(name, str) and name, f"{code} has no fund name")
        require(
            not any(keyword in name for keyword in EXPECTED_EXCLUDE_KEYWORDS),
            f"{code} contains an excluded fund-name keyword",
        )
        require(
            record.get("fund_type")
            not in {"QDII-纯债", "QDII-混合债", "QDII-商品"},
            f"{code} has an excluded fund type",
        )
        require(
            record.get("purchase_status") in {"open", "limited"},
            f"{code} is not currently purchasable",
        )
        require(
            as_number(record.get("scale_billion_cny"), f"{code} scale")
            > EXPECTED_FILTERS["min_scale_billion_cny"],
            f"{code} does not meet the strict scale threshold",
        )
        require(
            parse_date(record["inception_date"]) < cutoff,
            f"{code} is not strictly older than three years",
        )
        require(
            as_number(record.get("three_year_return_pct"), f"{code} three-year return")
            >= EXPECTED_FILTERS["min_three_year_return_pct"],
            f"{code} does not meet the three-year return threshold",
        )
        for field in (
            "one_year_return_pct",
            "one_year_max_drawdown_pct",
            "three_year_max_drawdown_pct",
        ):
            as_number(record.get(field), f"{code} {field}")
        nav_start = parse_date(str(record.get("nav_history_start_date")))
        nav_end = parse_date(str(record.get("nav_history_end_date")))
        require(nav_start <= nav_end <= run_date, f"{code} NAV history dates are invalid")
        for prefix, years in (("five_year", 5), ("ten_year", 10)):
            value = record.get(f"{prefix}_return_pct")
            start_value = record.get(f"{prefix}_performance_start_date")
            end_value = record.get(f"{prefix}_performance_end_date")
            if value is None:
                require(
                    start_value is None and end_value is None,
                    f"{code} incomplete {prefix} window has dates",
                )
            else:
                as_number(value, f"{code} {prefix} return")
                start = parse_date(str(start_value))
                end = parse_date(str(end_value))
                require(start <= end <= run_date, f"{code} {prefix} dates are invalid")
                require(
                    start <= years_ago(end, years),
                    f"{code} {prefix} window is incomplete",
                )
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

        if ranking_list == "us_main":
            require(confirmed >= EXPECTED_FILTERS["min_us_equity_pct"], f"{code} does not meet the confirmed US-equity threshold")
            require(exposure.get("status") == "qualified", f"{code} exposure is not qualified")
            validate_nasdaq_fit(record, run_date)
        else:
            require(
                confirmed < EXPECTED_FILTERS["min_us_equity_pct"],
                f"{code} global record meets the US-main exposure threshold",
            )
            annualized = as_number(
                record.get("three_year_annualized_return_pct"), f"{code} annualized return"
            )
            start = parse_date(record["three_year_performance_start_date"])
            end = parse_date(record["three_year_performance_end_date"])
            expected_annualized = (
                (1 + float(record["three_year_return_pct"]) / 100)
                ** (365 / (end - start).days)
                - 1
            ) * 100
            require(
                math.isclose(annualized, round(expected_annualized, 2), abs_tol=1e-9),
                f"{code} annualized return differs from its three-year return",
            )
            drawdown = abs(float(record["three_year_max_drawdown_pct"]))
            score = record.get("return_drawdown_ratio")
            if drawdown == 0:
                require(score is None, f"{code} zero-drawdown ratio must be null")
            else:
                require(
                    math.isclose(
                        as_number(score, f"{code} return/drawdown ratio"),
                        round(expected_annualized / drawdown, 4),
                        abs_tol=1e-9,
                    ),
                    f"{code} return/drawdown ratio differs",
                )

        validate_limit(record.get("direct_limit"), code, "direct")
        validate_limit(record.get("agency_limit"), code, "agency", allow_unknown=True)
        direct = record["direct_limit"]
        require(
            direct["status"] == "unlimited"
            or int(direct.get("amount_cny") or 0)
            >= EXPECTED_FILTERS["min_direct_limit_cny_inclusive"],
            f"{code} does not meet the inclusive direct-sale limit threshold",
        )
        quota_sources = record.get("quota_source_urls")
        require(
            isinstance(quota_sources, list)
            and all(isinstance(source, str) and source for source in quota_sources),
            f"{code} quota source list is invalid",
        )
        if record.get("quota_status") == "limited":
            require(quota_sources, f"{code} limited quota has no announcement source")
        for channel in ("direct_limit", "agency_limit"):
            source_url = record[channel].get("source_url")
            if source_url and source_url != record.get("fund_page_url"):
                require(
                    source_url in quota_sources,
                    f"{code} {channel} source is absent from quota_source_urls",
                )

    require(len(codes) == len(set(codes)), "Ranking contains duplicate fund codes")
    if ranking_list == "us_main":
        expected_order = sorted(
            records,
            key=lambda item: (
                -float(item["nasdaq100_fit"]["correlation"]),
                abs(float(item["nasdaq100_fit"]["beta"]) - 1),
                -float(item["us_equity_exposure"]["confirmed_pct"]),
                -float(item["institution_holding_ratio_pct"]),
                -float(item["three_year_return_pct"]),
                item["code"],
            ),
        )
    else:
        expected_order = sorted(
            records,
            key=lambda item: (
                float("-inf")
                if item["return_drawdown_ratio"] is None
                else -float(item["return_drawdown_ratio"]),
                -float(item["three_year_return_pct"]),
                -float(item["three_year_max_drawdown_pct"]),
                -float(item["institution_holding_ratio_pct"]),
                -float(item["scale_billion_cny"]),
                item["code"],
            ),
        )
    require(
        [item["code"] for item in records] == [item["code"] for item in expected_order],
        f"{ranking_list} order does not match its sort rule",
    )
    return records


def validate_csv(path: Path, records: list[dict[str, Any]]) -> None:
    require(path.is_file(), f"Missing artifact: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except OSError as exc:
        raise ValidationError(f"Could not read {path}: {exc}") from exc
    require(len(rows) == len(records), "CSV record count differs from JSON")
    for record, row in zip(records, rows):
        code = record["code"]
        for field in ("ranking_list", "code", "name"):
            require(row.get(field) == str(record[field]), f"CSV {field} differs for {code}")
        require(row.get("rank") == str(record["rank"]), f"CSV rank differs for {code}")
        for field in (
            "institution_holding_ratio_pct",
            "scale_billion_cny",
            "one_year_return_pct",
            "one_year_max_drawdown_pct",
            "three_year_return_pct",
            "three_year_max_drawdown_pct",
        ):
            require(
                math.isclose(float(row[field]), float(record[field]), abs_tol=1e-9),
                f"CSV {field} differs for {code}",
            )
        for field in (
            "nav_history_start_date",
            "nav_history_end_date",
            "five_year_performance_start_date",
            "five_year_performance_end_date",
            "ten_year_performance_start_date",
            "ten_year_performance_end_date",
        ):
            require(row[field] == str(record.get(field) or ""), f"CSV {field} differs for {code}")
        for field in ("five_year_return_pct", "ten_year_return_pct"):
            expected = "" if record.get(field) is None else str(record[field])
            require(row[field] == expected, f"CSV {field} differs for {code}")
        contract = record["contract_benchmark"]
        contract_fields = {
            "contract_benchmark_status": "status",
            "contract_benchmark_name": "benchmark_name",
            "contract_benchmark_text": "benchmark_text",
            "contract_market_scope": "market_scope",
            "contract_market_label": "market_label",
            "contract_asset_class": "asset_class",
            "contract_style_label": "style_label",
            "contract_structure": "structure",
            "contract_source_url": "source_url",
            "product_summary_status": "product_summary_status",
        }
        for csv_field, json_field in contract_fields.items():
            require(
                row.get(csv_field) == str(contract.get(json_field) or ""),
                f"CSV {csv_field} differs for {code}",
            )
        require(
            json.loads(row["contract_benchmark_components"])
            == contract["components"],
            f"CSV contract components differ for {code}",
        )
        expected_weight = (
            "" if contract["benchmark_weight_pct"] is None else str(contract["benchmark_weight_pct"])
        )
        require(
            row["contract_benchmark_weight_pct"] == expected_weight,
            f"CSV contract benchmark weight differs for {code}",
        )
        cost = record["holding_cost"]
        require(row["holding_cost_status"] == cost["status"], f"CSV holding cost status differs for {code}")
        expected_cost = "" if cost["annualized_pct"] is None else str(cost["annualized_pct"])
        require(row["holding_cost_annualized_pct"] == expected_cost, f"CSV holding cost differs for {code}")
        require(row["holding_cost_measurement_date"] == str(cost.get("measurement_date") or ""), f"CSV holding cost date differs for {code}")
        require(row["holding_cost_source_url"] == str(cost.get("source_url") or ""), f"CSV holding cost source differs for {code}")
        require(
            row.get("product_structure_tags")
            == " | ".join(record["product_structure_tags"]),
            f"CSV product structure tags differ for {code}",
        )
        fit = record["nasdaq100_fit"] or {}
        for csv_field, fit_field in (
            ("nasdaq100_correlation", "correlation"),
            ("nasdaq100_beta", "beta"),
            ("nasdaq100_tracking_error_pct", "tracking_error_pct"),
        ):
            expected = fit.get(fit_field)
            if expected is None:
                require(row[csv_field] == "", f"CSV {csv_field} must be blank for {code}")
            else:
                require(
                    math.isclose(float(row[csv_field]), float(expected), abs_tol=1e-9),
                    f"CSV {csv_field} differs for {code}",
                )
        require(
            row["nasdaq100_observations"] == str(fit.get("observations") or ""),
            f"CSV Nasdaq-100 observations differ for {code}",
        )
        require(
            row["nasdaq100_start_date"] == str(fit.get("start_date") or "")
            and row["nasdaq100_end_date"] == str(fit.get("end_date") or ""),
            f"CSV Nasdaq-100 dates differ for {code}",
        )
        exposure = record["us_equity_exposure"]
        for field in ("confirmed_pct", "possible_pct"):
            require(
                math.isclose(
                    float(row[f"us_equity_{field}"]),
                    float(exposure[field]),
                    abs_tol=1e-9,
                ),
                f"CSV US exposure {field} differs for {code}",
            )
        if record["ranking_list"] == "global_supplement":
            require(
                row["three_year_annualized_return_pct"]
                == str(record["three_year_annualized_return_pct"]),
                f"CSV annualized return differs for {code}",
            )
            expected_ratio = "" if record["return_drawdown_ratio"] is None else str(record["return_drawdown_ratio"])
            require(row["return_drawdown_ratio"] == expected_ratio, f"CSV return/drawdown ratio differs for {code}")
        require(row["direct_limit"] == format_limit(record["direct_limit"]), f"CSV direct limit differs for {code}")
        require(row["agency_limit"] == format_limit(record["agency_limit"]), f"CSV agency limit differs for {code}")
        require(
            row["quota_source_urls"] == " | ".join(record["quota_source_urls"]),
            f"CSV quota sources differ for {code}",
        )


def validate_markdown(path: Path, payload: dict[str, Any], records: list[dict[str, Any]]) -> None:
    require(path.is_file(), f"Missing artifact: {path}")
    document = path.read_text(encoding="utf-8")
    require(f"- 更新日期：{payload['run_date']}" in document, "Markdown run date differs")
    parsed: list[tuple[int, str, str, str]] = []
    for line in document.splitlines():
        match = MARKDOWN_ROW_RE.match(line)
        if match:
            parsed.append((int(match.group(1)), match.group(3), match.group(2), line))
    require(len(parsed) == len(records), "Markdown record count differs from JSON")
    for record, (rank, code, name, line) in zip(records, parsed):
        require(rank == record["rank"] and code == record["code"], f"Markdown order differs at {record['code']}")
        require(name == record["name"], f"Markdown name differs for {record['code']}")
        expected_values = [
            format_percentage(record["three_year_return_pct"], show_sign=True),
            mailer.format_optional_percentage(record["five_year_return_pct"]),
            mailer.format_optional_percentage(record["ten_year_return_pct"]),
            mailer.format_holding_cost(record["holding_cost"]),
            format_limit(record["direct_limit"]),
            format_limit(record["agency_limit"]),
            format_percentage(record["us_equity_exposure"]["confirmed_pct"]),
            format_percentage(record["us_equity_exposure"]["possible_pct"]),
        ]
        if record["ranking_list"] == "us_main":
            expected_values.extend(
                (
                    format_correlation(record["nasdaq100_fit"]["correlation"]),
                    format_beta(record["nasdaq100_fit"]["beta"]),
                )
            )
        else:
            ratio = "∞" if record["return_drawdown_ratio"] is None else f"{record['return_drawdown_ratio']:.2f}"
            expected_values.extend(
                (
                    record["contract_benchmark"]["benchmark_name"],
                    ratio,
                )
            )
        require(all(value in line for value in expected_values), f"Markdown metrics differ for {record['code']}")
        require(
            record["contract_benchmark"]["benchmark_name"] in document,
            f"Markdown contract benchmark differs for {record['code']}",
        )


def parse_html(document: str) -> RankingHtmlParser:
    parser = RankingHtmlParser()
    try:
        parser.feed(document)
        parser.close()
    except (ValidationError, ValueError) as exc:
        raise ValidationError(f"Could not parse ranking HTML: {exc}") from exc
    return parser


def validate_html_document(
    document: str, payload: dict[str, Any], records: list[dict[str, Any]], label: str
) -> None:
    parser = parse_html(document)
    require(payload["run_date"] in " ".join(parser.all_text), f"{label} run date differs")
    expected_codes = [record["code"] for record in records]
    require(parser.codes == expected_codes, f"{label} fund order differs from JSON")
    require(
        parser.lists == [record["ranking_list"] for record in records],
        f"{label} ranking-list assignments differ from JSON",
    )
    all_text = " ".join(parser.all_text)
    require("美国主榜" in all_text and "全球补充榜" in all_text, f"{label} tabs are missing")
    for record in records:
        code = record["code"]
        block = parser.blocks[code]
        expected_values = [
            record["name"],
            record["contract_benchmark"]["benchmark_name"],
            record["contract_benchmark"]["benchmark_text"],
            f"{float(record['scale_billion_cny']):.2f} 亿元",
            format_percentage(record["one_year_return_pct"], show_sign=True),
            format_percentage(record["one_year_max_drawdown_pct"]),
            format_percentage(record["three_year_return_pct"], show_sign=True),
            format_percentage(record["three_year_max_drawdown_pct"]),
            (
                format_percentage(record["five_year_return_pct"], show_sign=True)
                if record["five_year_return_pct"] is not None
                else f"--（净值始于 {record['nav_history_start_date']}）"
            ),
            (
                format_percentage(record["ten_year_return_pct"], show_sign=True)
                if record["ten_year_return_pct"] is not None
                else f"--（净值始于 {record['nav_history_start_date']}）"
            ),
            mailer.format_holding_cost(record["holding_cost"]),
            format_percentage(record["us_equity_exposure"]["confirmed_pct"]),
            format_percentage(record["us_equity_exposure"]["possible_pct"]),
            format_limit(record["direct_limit"]),
            format_limit(record["agency_limit"]),
            *record["product_structure_tags"],
        ]
        if record["ranking_list"] == "us_main":
            expected_values.extend(
                (
                    format_correlation(record["nasdaq100_fit"]["correlation"]),
                    format_beta(record["nasdaq100_fit"]["beta"]),
                    format_percentage(record["nasdaq100_fit"]["tracking_error_pct"]),
                    f"{record['nasdaq100_fit']['observations']} 周",
                )
            )
        else:
            ratio = "∞" if record["return_drawdown_ratio"] is None else f"{record['return_drawdown_ratio']:.2f}"
            expected_values.extend(
                (
                    format_percentage(record["three_year_annualized_return_pct"], show_sign=True),
                    ratio,
                )
            )
        require(all(value in block for value in expected_values), f"{label} metrics differ for {code}")


def validate_email_rendering(payload: dict[str, Any], records: list[dict[str, Any]]) -> None:
    page_url = f"https://example.test/?v={payload['run_date']}"
    plain = mailer.success_plain_text(payload, page_url)
    html_document = mailer.success_html(payload, page_url)
    for document, label in ((plain, "Plain email"), (html_document, "HTML email")):
        positions = [document.find(record["code"]) for record in records]
        require(all(position >= 0 for position in positions), f"{label} is missing a fund code")
        require(positions == sorted(positions), f"{label} fund order differs from JSON")
        for record in records:
            expected = [
                record["contract_benchmark"]["benchmark_name"],
                mailer.format_percentage(record["three_year_return_pct"], show_sign=True),
                mailer.format_optional_percentage(record["five_year_return_pct"]),
                mailer.format_optional_percentage(record["ten_year_return_pct"]),
                mailer.format_holding_cost(record["holding_cost"]),
                mailer.format_percentage(record["us_equity_exposure"]["confirmed_pct"]),
                mailer.format_percentage(record["us_equity_exposure"]["possible_pct"]),
                mailer.format_limit(record["direct_limit"]),
                mailer.format_limit(record["agency_limit"]),
            ]
            if record["ranking_list"] == "us_main":
                expected.extend(
                    (
                        mailer.format_correlation(record["nasdaq100_fit"]["correlation"]),
                        mailer.format_beta(record["nasdaq100_fit"]["beta"]),
                    )
                )
            else:
                expected.append(mailer.format_ratio(record["return_drawdown_ratio"]))
            require(
                all(value in document for value in expected),
                f"{label} metrics differ for {record['code']}",
            )


def validate_local_artifacts(
    output_dir: Path, publish_dir: Path, expected_date: str
) -> tuple[dict[str, Any], list[str]]:
    payload = load_payload(output_dir / "latest.json")
    require(payload.get("schema_version") == 9, "Unexpected JSON schema version")
    require(payload.get("run_date") == expected_date, "Ranking date is not today's Shanghai date")
    require(
        str(payload.get("generated_at", ""))[:10] == expected_date,
        "Generation timestamp does not match the ranking date",
    )
    validate_filters(payload.get("filters"))
    validate_exclusion_summary(payload.get("exclusion_summary"))
    validate_benchmark(payload.get("benchmark"), parse_date(expected_date))
    global_section = payload.get("global_supplement")
    require(isinstance(global_section, dict), "global_supplement must be an object")
    require(
        global_section.get("ranking_method") == EXPECTED_GLOBAL_RANKING_METHOD,
        "Global supplement ranking method differs from filters",
    )
    us_records = validate_records(payload, payload.get("records"), "us_main")
    global_records = validate_records(
        payload, global_section.get("records"), "global_supplement"
    )
    require(us_records or global_records, "Both ranking lists are empty")
    records = [*us_records, *global_records]
    codes = [record["code"] for record in records]
    require(len(codes) == len(set(codes)), "Fund codes are duplicated across ranking lists")
    reportable, blocking = classify_warnings(payload.get("warnings"))
    require(not blocking, "Blocking warnings: " + " | ".join(blocking))
    validate_csv(output_dir / "latest.csv", records)
    validate_markdown(output_dir / "latest.md", payload, records)
    validate_email_rendering(payload, records)

    generated_html_path = output_dir / "latest.html"
    published_html_path = publish_dir / "index.html"
    require(generated_html_path.is_file(), f"Missing artifact: {generated_html_path}")
    require(published_html_path.is_file(), f"Missing artifact: {published_html_path}")
    generated = generated_html_path.read_bytes()
    published = published_html_path.read_bytes()
    require(generated == published, "Generated and published HTML are not byte-identical")
    validate_html_document(generated.decode("utf-8"), payload, records, "HTML")
    return payload, reportable


def validate_deployment(
    url: str,
    payload: dict[str, Any],
    attempts: int,
    delay_seconds: float,
) -> None:
    records = [
        *payload["records"],
        *payload["global_supplement"]["records"],
    ]
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "qdii-ranking-deployment-validator/1.0"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                require(response.status == 200, f"Deployment returned HTTP {response.status}")
                document = response.read().decode("utf-8")
            validate_html_document(document, payload, records, "Deployed HTML")
            return
        except (
            OSError,
            UnicodeDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            ValidationError,
        ) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)
    raise ValidationError(f"Deployment verification failed after {attempts} attempts: {last_error}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("output/qdii-ranking"))
    parser.add_argument("--publish-dir", type=Path, default=Path("public"))
    parser.add_argument("--expected-date", default=current_shanghai_date())
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
        payload, warnings = validate_local_artifacts(
            args.output_dir.resolve(), args.publish_dir.resolve(), args.expected_date
        )
        for warning in warnings:
            print(f"REPORTABLE WARNING: {warning}", file=sys.stderr)
        if args.deployed_url:
            validate_deployment(
                args.deployed_url, payload, args.attempts, args.delay_seconds
            )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Validated {len(payload['records'])} US records and "
        f"{len(payload['global_supplement']['records'])} global records for "
        f"{payload['run_date']}"
        + (" and the deployed page" if args.deployed_url else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
