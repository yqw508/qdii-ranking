"""Public record and independent Nasdaq-100 OTC ranking assembly."""

from __future__ import annotations

from datetime import date
from typing import Any

from .cache.announcements import AnnouncementIndexCache, PeriodicReportCache
from .cache.contracts import ContractProfileResultCache
from .cache.performance import PerformanceResultCache
from .config import FUND_PAGE_URL, PERFORMANCE_DATA_URL
from .errors import DataError
from .models import Nasdaq100Benchmark
from .runtime import HttpClient
from .ranking import contract_mentions_nasdaq100, nasdaq100_otc_sort_key
from .services.nasdaq100 import apply_common_window
from .sources.candidates import build_nasdaq100_otc_candidates
from .sources.contracts import (
    ContractBenchmarkCatalog,
    unavailable_holding_cost,
    unreadable_contract_benchmark,
)
from .sources.fund import parse_fund_page


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
        "common_period_return_pct": fund.get("common_period_return_pct"),
        "common_period_max_drawdown_pct": fund.get("common_period_max_drawdown_pct"),
        "common_period_performance_start_date": fund.get(
            "common_period_performance_start_date"
        ),
        "common_period_performance_end_date": fund.get(
            "common_period_performance_end_date"
        ),
        "nasdaq100_fit_common_period": fund.get("nasdaq100_fit_common_period"),
        "common_period_error": fund.get("common_period_error"),
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
) -> tuple[list[dict[str, Any]], list[str], dict[str, int], dict[str, Any]]:
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

    comparison_window, common_warnings = apply_common_window(
        performance_records,
        {
            item["code"]: performance_cache.loaded_points(item["code"])
            for item in performance_records
        },
        as_of,
        benchmark,
    )
    warnings.extend(common_warnings)
    performance_records.sort(key=nasdaq100_otc_sort_key)
    records = [
        build_nasdaq100_otc_output_record(fund, rank, holder_report_date)
        for rank, fund in enumerate(performance_records, start=1)
    ]
    missing_counts = {
        "two_year_return": sum(item.get("two_year_return_pct") is None for item in records),
        "holding_cost": sum(item["holding_cost"].get("annualized_pct") is None for item in records),
        "nasdaq100_fit_2y": sum(not isinstance(item.get("nasdaq100_fit_2y"), dict) for item in records),
        "common_period_return": sum(
            item.get("common_period_return_pct") is None for item in records
        ),
        "common_period_max_drawdown": sum(
            item.get("common_period_max_drawdown_pct") is None for item in records
        ),
        "nasdaq100_fit_common_period": sum(
            not isinstance(item.get("nasdaq100_fit_common_period"), dict)
            for item in records
        ),
        "inception_date": sum(item.get("inception_date") is None for item in records),
    }
    return records, list(dict.fromkeys(warnings)), missing_counts, comparison_window
