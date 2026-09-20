"""Public payload assembly for the ranking pipeline."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import Any

from ..config import (
    ANNOUNCEMENT_API_URL,
    ETF_QUOTE_PAGE_URL,
    EXCLUDED_FUND_TYPES,
    FUND_LIST_URL,
    HOLDER_API_URL,
    NASDAQ100_HISTORY_PAGE_URL,
    NASDAQ100_OTC_NAME_RE,
    PERFORMANCE_DATA_URL,
    RANKING_SCHEMA_VERSION,
    SAFE_USD_CNY_HISTORY_URL,
    THREE_YEAR_BOUNDARY_TOLERANCE_DAYS,
)
from ..runtime import SHANGHAI_TZ
from ..services.ranking import premium_catalog_source
from ..services.context import (
    DiscoveryResult,
    DocumentStage,
    ExclusionCollector,
    NasdaqStage,
    PerformanceStage,
    PipelineResources,
    PremiumStage,
    RankingStage,
)


US_RANKING_METHOD = (
    "nasdaq100_correlation desc, abs(nasdaq100_beta - 1) asc, "
    "us_equity_confirmed_pct desc, institution_holding_ratio_pct desc, "
    "three_year_return_pct desc, code asc"
)
GLOBAL_RANKING_METHOD = (
    "three_year_return_drawdown_ratio desc, three_year_return_pct desc, "
    "three_year_max_drawdown_pct desc, institution_holding_ratio_pct desc, "
    "scale_billion_cny desc, code asc"
)
NASDAQ_RANKING_METHOD = (
    "two_year_return_pct desc, holding_cost.annualized_pct asc, "
    "nasdaq100_fit_2y.tracking_error_pct asc, three_year_return_pct desc, "
    "scale_billion_cny desc, code asc; missing values last"
)
NASDAQ_SELECTION_METHOD = (
    "从完整基金元数据按名称发现候选；不设置成立年限、收益、额度或申购状态硬门槛，"
    "仅保留场外人民币 A/未标记主份额并排除独立 ETF"
)


def _filters(
    args: argparse.Namespace,
    discovery: DiscoveryResult,
    performance: PerformanceStage,
    documents: DocumentStage,
    ranking: RankingStage,
    nasdaq: NasdaqStage,
) -> dict[str, Any]:
    return {
        "top": args.top,
        "min_scale_billion_cny": args.min_scale,
        "min_age_years": args.min_age_years,
        "min_three_year_return_pct": args.min_three_year_return_pct,
        "three_year_boundary_tolerance_days": THREE_YEAR_BOUNDARY_TOLERANCE_DAYS,
        "min_five_year_return_pct_if_available": args.min_five_year_return_pct,
        "min_ten_year_return_pct_if_available": args.min_ten_year_return_pct,
        "min_us_equity_pct": args.min_us_equity_pct,
        "min_direct_limit_cny_inclusive": args.min_direct_limit_cny,
        "base_candidates_total": len(discovery.preliminary),
        "performance_candidates_scanned": performance.scanned_count,
        "performance_qualified_count": len(performance.qualified),
        "contract_candidates_scanned": len(performance.qualified),
        "contract_metadata_resolved_count": sum(
            fund["contract_benchmark"]["status"] in {"recognized", "composite"}
            for fund in documents.classified
        ),
        "us_equity_candidates_scanned": len(documents.classified),
        "us_routed_count": len(ranking.us_routed),
        "global_routed_count": len(ranking.global_routed),
        "us_quota_candidates_scanned": len(ranking.us_routed),
        "us_quota_qualified_count": len(ranking.us_qualified),
        "global_quota_candidates_scanned": len(ranking.global_routed),
        "global_quota_qualified_count": len(ranking.global_qualified),
        "full_scan_completed": (
            performance.scanned_count == len(discovery.preliminary)
            and len(documents.classified) == len(performance.qualified)
            and len(ranking.us_routed) + len(ranking.global_routed)
            == len(documents.classified)
        ),
        "ranking_method": US_RANKING_METHOD,
        "global_supplement_ranking_method": GLOBAL_RANKING_METHOD,
        "us_equity_method": "conservative confirmed lower bound determines routing unless a fund-name geography keyword keeps the fund out of the US main list; unresolved positions only increase possible upper bound",
        "contract_benchmark_method": "display-only latest prospectus metadata; benchmark identity, market, structure, weight, and parse status never affect eligibility or routing",
        "us_main_exclude_keywords": args.us_main_exclude_keywords,
        "global_exclude_keywords": [],
        "exclude_fund_types": sorted(EXCLUDED_FUND_TYPES),
        "exclude_asset_classes": ["bond", "commodity"],
        "share_class": "OTC RMB A or explicit RMB primary share without C/D marker",
        "purchasable_only": True,
        "nasdaq100_otc": {
            "selection_method": NASDAQ_SELECTION_METHOD,
            "name_match_rule": "纳斯达克100 / 纳指100 / NASDAQ 100 名称匹配",
            "name_match_pattern": NASDAQ100_OTC_NAME_RE.pattern,
            "share_class": "OTC RMB A or explicit RMB primary share without C/D/E/F/I marker",
            "exclude_standalone_etf": True,
            "purchasable_only": False,
            "top": None,
            "ranking_method": NASDAQ_RANKING_METHOD,
            "candidate_count": len(nasdaq.records),
            "missing_fields": nasdaq.missing_fields,
        },
    }


def _cache_stats(resources: PipelineResources, premium: PremiumStage) -> dict[str, Any]:
    snapshot = premium.snapshot
    return {
        "nasdaq100_benchmark": resources.benchmark_cache.stats(),
        "performance": resources.performance_cache.stats(),
        "announcement_indexes": resources.announcement_cache.stats(),
        "contract_profiles": resources.contract_result_cache.stats(),
        "fund_us_equity_exposures": resources.exposure_cache.stats(),
        "quota_notices": resources.quota_notice_cache.stats(),
        "announcement_pdfs": resources.document_cache.stats(),
        "underlying_exposures": resources.resolver.stats(),
        "exchange_premium": {
            "fresh": snapshot["fresh_count"],
            "hits": snapshot["cache_hit_count"],
            "expected": snapshot["expected_count"],
        },
        "exchange_premium_holding_costs": resources.etf_holding_cost_cache.stats(),
    }


def _sources(args: argparse.Namespace) -> dict[str, str]:
    configured = getattr(args, "us_equity_etf_catalog", None)
    return {
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
            str(configured.resolve()) if configured else premium_catalog_source()
        ),
        "exchange_etf_catalog": premium_catalog_source(),
        "exchange_premium": ETF_QUOTE_PAGE_URL,
    }


def assemble_payload(
    args: argparse.Namespace,
    as_of: date,
    discovery: DiscoveryResult,
    resources: PipelineResources,
    performance: PerformanceStage,
    documents: DocumentStage,
    ranking: RankingStage,
    nasdaq: NasdaqStage,
    premium: PremiumStage,
    exclusions: ExclusionCollector,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": RANKING_SCHEMA_VERSION,
        "run_date": as_of.isoformat(),
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "holder_report_date": discovery.selected_period.report_date,
        "holder_period_fund_count": discovery.selected_period.fund_count,
        "filters": _filters(args, discovery, performance, documents, ranking, nasdaq),
        "cache": _cache_stats(resources, premium),
        "benchmark": resources.benchmark.metadata(),
        "exchange_premium": premium.snapshot,
        "records": list(ranking.records),
        "global_supplement": {
            "ranking_method": GLOBAL_RANKING_METHOD,
            "qualified_count": len(ranking.global_qualified),
            "records": list(ranking.global_records),
        },
        "nasdaq100_otc": {
            "selection_method": NASDAQ_SELECTION_METHOD,
            "name_match_rule": "纳斯达克100 / 纳指100 / NASDAQ 100 名称匹配",
            "share_class_rule": "OTC RMB A or explicit RMB primary share without C/D/E/F/I marker",
            "exclude_standalone_etf": True,
            "purchasable_only": False,
            "ranking_method": NASDAQ_RANKING_METHOD,
            "candidate_count": len(nasdaq.records),
            "missing_fields": nasdaq.missing_fields,
            "records": list(nasdaq.records),
        },
        "exclusion_summary": exclusions.public_records(),
        "warnings": list(dict.fromkeys(warnings)),
        "sources": _sources(args),
    }
