"""Independent stages of the ranking refresh pipeline."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

from ..assemblers import build_nasdaq100_otc_records, build_output_record
from ..cache.announcements import AnnouncementIndexCache, PeriodicReportCache
from ..cache.benchmark import Nasdaq100BenchmarkCache
from ..cache.contracts import ContractProfileResultCache
from ..cache.exposure import FundExposureResultCache
from ..cache.performance import PerformanceResultCache
from ..cache.premium import ExchangePremiumHoldingCostCache
from ..cache.quota import QuotaNoticeParseCache
from ..config import DEFAULT_US_EQUITY_ETF_CATALOG, DOCUMENT_WORKERS
from ..errors import DataError
from ..ranking import (
    build_holder_candidates,
    calculate_return_drawdown_ratio,
    direct_limit_qualifies,
    evaluate_performance_full_scan,
    filter_and_rank,
    global_supplement_sort_key,
    ranking_route,
    us_main_sort_key,
)
from ..runtime import HttpClient, RunMetrics
from ..sources.announcements import fetch_latest_periodic_report
from ..sources.contracts import ContractBenchmarkCatalog
from ..sources.exposure import LookthroughResolver, fetch_us_equity_exposure
from ..sources.fund import enrich_fund_pages, fetch_fund_metadata
from ..sources.holder import fetch_holder_periods, fetch_holder_rows, select_holder_period
from ..sources.premium import (
    _load_cached_qdii_exchange_premium_catalog,
    attach_exchange_premium_holding_costs,
    build_exchange_premium_holding_costs,
    build_exchange_premium_snapshot,
    exchange_premium_market_url,
    load_exchange_premium_catalog,
    load_qdii_exchange_premium_catalog,
)
from ..sources.quota import resolve_quota
from .context import (
    DiscoveryResult,
    DocumentStage,
    ExclusionCollector,
    NasdaqStage,
    PerformanceStage,
    PipelineResources,
    PremiumStage,
    RankingStage,
)
from ..pipeline.core import (
    RunMemo,
    apply_quota_gate,
    evaluate_batch,
    rank_candidates,
    route_candidates,
)


def premium_catalog_source() -> str:
    return exchange_premium_market_url()


def discover_candidates(
    args: argparse.Namespace, client: HttpClient, as_of: date, metrics: RunMetrics
) -> DiscoveryResult:
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
    return DiscoveryResult(
        metadata,
        selected,
        holder_rows,
        enriched,
        preliminary,
        tuple(warnings),
    )


def initialize_resources(
    args: argparse.Namespace,
    client: HttpClient,
    as_of: date,
    metrics: RunMetrics,
) -> tuple[PipelineResources, tuple[str, ...]]:
    cache_root = (args.cache_dir or (args.output_dir / "cache")).resolve()
    document_cache = PeriodicReportCache(cache_root / "announcement-pdfs")
    resolver = LookthroughResolver(
        args.us_equity_catalog.resolve(), cache_root / "us-equity-lookthrough.json"
    )
    announcement_cache = AnnouncementIndexCache(cache_root / "announcement-indexes")
    benchmark_cache = Nasdaq100BenchmarkCache(
        cache_root / "benchmarks" / "nasdaq100-cny.json"
    )
    with metrics.phase("benchmark_update"):
        benchmark, warnings = benchmark_cache.get(client, as_of)
    return PipelineResources(
        cache_root=cache_root,
        document_cache=document_cache,
        resolver=resolver,
        contract_catalog=ContractBenchmarkCatalog(
            args.contract_benchmark_catalog.resolve()
        ),
        announcement_cache=announcement_cache,
        contract_result_cache=ContractProfileResultCache(cache_root / "contract-profiles"),
        quota_notice_cache=QuotaNoticeParseCache(cache_root / "quota-notices"),
        etf_holding_cost_cache=ExchangePremiumHoldingCostCache(
            cache_root / "exchange-premium-holding-costs"
        ),
        benchmark_cache=benchmark_cache,
        performance_cache=PerformanceResultCache(cache_root / "performance"),
        exposure_cache=FundExposureResultCache(cache_root / "fund-us-equity-exposures"),
        benchmark=benchmark,
        memo=RunMemo(as_of),
    ), tuple(warnings)


def scan_performance(
    args: argparse.Namespace,
    client: HttpClient,
    as_of: date,
    metrics: RunMetrics,
    resources: PipelineResources,
    candidates: list[dict[str, Any]],
) -> PerformanceStage:
    with metrics.phase("performance_scan"):
        qualified, warnings, scanned, rejections = evaluate_performance_full_scan(
            client,
            candidates,
            as_of,
            args.min_three_year_return_pct,
            resources.performance_cache,
            resources.benchmark,
            min_five_year_return_pct=args.min_five_year_return_pct,
            min_ten_year_return_pct=args.min_ten_year_return_pct,
            run_cache=resources.memo.performance,
        )
    return PerformanceStage(tuple(qualified), scanned, tuple(warnings), rejections)


def _evaluate_documents(
    args: argparse.Namespace,
    client: HttpClient,
    as_of: date,
    resources: PipelineResources,
    fund: dict[str, Any],
) -> dict[str, Any]:
    snapshot = resources.announcement_cache.get(client, fund["code"], as_of)
    profile, holding_cost, profile_warnings = resources.contract_result_cache.get(
        client,
        fund,
        as_of,
        resources.document_cache,
        resources.contract_catalog,
        snapshot,
    )
    report = fetch_latest_periodic_report(
        client, fund["code"], as_of, snapshot=snapshot
    )
    exposure, exposure_warnings = fetch_us_equity_exposure(
        client,
        fund,
        as_of,
        resources.document_cache,
        resources.resolver,
        args.min_us_equity_pct,
        resources.exposure_cache,
        report=report,
        snapshot=snapshot,
    )
    quota = None
    quota_warnings: list[str] = []
    quota_error = None
    try:
        quota, quota_warnings = resolve_quota(
            client,
            fund,
            as_of,
            resources.document_cache,
            snapshot=snapshot,
            notice_cache=resources.quota_notice_cache,
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


def scan_documents(
    args: argparse.Namespace,
    client: HttpClient,
    as_of: date,
    metrics: RunMetrics,
    resources: PipelineResources,
    funds: tuple[dict[str, Any], ...],
) -> DocumentStage:
    evaluator = lambda fund: _evaluate_documents(args, client, as_of, resources, fund)
    with metrics.phase("document_scan"):
        results = evaluate_batch(funds, evaluator, DOCUMENT_WORKERS)
    classified: list[dict[str, Any]] = []
    warnings: list[str] = []
    for fund, result in zip(funds, results):
        if result is None:
            raise DataError(f"Documents were not evaluated for fund {fund['code']}")
        warnings.extend(result["profile_warnings"])
        classified.append(
            {
                **fund,
                "contract_benchmark": result["profile"],
                "holding_cost": result["holding_cost"],
                "_document_result": result,
            }
        )
    return DocumentStage(tuple(classified), tuple(warnings))


def route_and_rank(
    args: argparse.Namespace,
    funds: tuple[dict[str, Any], ...],
    holder_report_date: str,
    exclusions: ExclusionCollector,
) -> RankingStage:
    try:
        routed = route_candidates(
            funds,
            args.min_us_equity_pct,
            args.us_main_exclude_keywords,
            ranking_route,
        )
    except ValueError as exc:
        raise DataError(str(exc)) from exc
    us_quota = apply_quota_gate(
        routed.us_main, args.min_direct_limit_cny, direct_limit_qualifies
    )
    global_quota = apply_quota_gate(
        routed.global_supplement, args.min_direct_limit_cny, direct_limit_qualifies
    )
    for reason, label, code in (*us_quota.exclusions, *global_quota.exclusions):
        exclusions.add(reason, label, code)
    ranked = rank_candidates(
        us_quota.qualified,
        global_quota.qualified,
        args.top,
        us_main_sort_key,
        global_supplement_sort_key,
        calculate_return_drawdown_ratio,
    )
    for reason, label, code in ranked.exclusions:
        exclusions.add(reason, label, code)
    records = tuple(
        build_output_record(fund, rank, "us_main", holder_report_date)
        for rank, fund in enumerate(ranked.us_ranked, start=1)
    )
    global_records = tuple(
        build_output_record(fund, rank, "global_supplement", holder_report_date)
        for rank, fund in enumerate(ranked.global_ranked, start=1)
    )
    warnings = [*routed.warnings, *us_quota.warnings, *global_quota.warnings]
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
    return RankingStage(
        routed.us_main,
        routed.global_supplement,
        ranked.us_qualified,
        ranked.global_qualified,
        records,
        global_records,
        tuple(warnings),
    )


def scan_nasdaq_otc(
    client: HttpClient,
    discovery: DiscoveryResult,
    as_of: date,
    resources: PipelineResources,
    metrics: RunMetrics,
) -> NasdaqStage:
    with metrics.phase("nasdaq100_otc"):
        records, warnings, missing = build_nasdaq100_otc_records(
            client,
            discovery.metadata,
            discovery.holder_rows,
            discovery.enriched,
            as_of,
            discovery.selected_period.report_date,
            resources.benchmark,
            resources.performance_cache,
            resources.announcement_cache,
            resources.contract_result_cache,
            resources.document_cache,
            resources.contract_catalog,
            resources.memo.performance,
        )
    return NasdaqStage(tuple(records), tuple(warnings), missing)


def scan_exchange_premium(
    args: argparse.Namespace,
    client: HttpClient,
    metadata: list[dict[str, Any]],
    as_of: date,
    resources: PipelineResources,
    metrics: RunMetrics,
) -> PremiumStage:
    warnings: list[str] = []
    with metrics.phase("exchange_premium"):
        configured_catalog = getattr(args, "us_equity_etf_catalog", None)
        quote_rows = None
        if configured_catalog:
            catalog_path = Path(configured_catalog).resolve()
            entries, fingerprint = load_exchange_premium_catalog(catalog_path)
        else:
            catalog_path = DEFAULT_US_EQUITY_ETF_CATALOG
            try:
                entries, quote_rows, fingerprint = load_qdii_exchange_premium_catalog(
                    client, metadata
                )
            except (DataError, OSError, ValueError) as exc:
                cached = _load_cached_qdii_exchange_premium_catalog(
                    resources.cache_root / "exchange-premium.json", metadata
                )
                if cached is None:
                    raise
                entries, fingerprint = cached
                quote_rows = {}
                warnings.append(
                    f"场内溢价告警：QDII 场内目录刷新失败，使用上次目录和行情缓存：{exc}"
                )
        snapshot, snapshot_warnings = build_exchange_premium_snapshot(
            client,
            catalog_path,
            resources.cache_root / "exchange-premium.json",
            as_of,
            catalog_entries=entries,
            quote_rows=quote_rows,
            catalog_fingerprint=fingerprint,
        )
        costs, cost_warnings = build_exchange_premium_holding_costs(
            client,
            snapshot["records"],
            as_of,
            resources.announcement_cache,
            resources.document_cache,
            resources.etf_holding_cost_cache,
        )
        attach_exchange_premium_holding_costs(snapshot, costs)
    warnings.extend(cost_warnings)
    warnings.extend(snapshot_warnings)
    return PremiumStage(snapshot, tuple(warnings))
