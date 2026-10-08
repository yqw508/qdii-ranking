"""Independent stages of the ranking refresh pipeline."""

from __future__ import annotations

import argparse
import re
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
from ..freshness import require_fresh
from ..runtime import is_older_than_years
from ..config import FUND_LIST_URL, FUND_PAGE_URL
from ..sources.fund import parse_fund_page
from ..ranking import (
    build_holder_candidates,
    calculate_return_drawdown_ratio,
    direct_limit_qualifies,
    evaluate_performance_full_scan,
    filter_and_rank,
    global_supplement_sort_key,
    ranking_route,
    us_main_sort_key,
    disappeared_ranked_candidates,
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
        audit = args._audit
        candidates = build_holder_candidates([], metadata, [])
        for fund in candidates:
            audit.register(fund)
        candidate_codes = {f["code"] for f in candidates}
        previous = {r["code"]: r for r in audit.baseline["records"]}
        for code, record in previous.items():
            if code not in metadata:
                audit.register(record)
                audit.event(code, "discovery", "blocked", "metadata_missing", "完整基金目录缺失", sources=[FUND_LIST_URL])
                raise DataError(f"{code}: previously ranked fund missing from metadata")
            if code not in candidate_codes:
                audit.register(metadata[code])
                audit.event(code, "discovery", "excluded", "type_or_share_ineligible", "类型或份额不符合范围",
                            values=metadata[code], sources=[FUND_LIST_URL])
        periods = fetch_holder_periods(client)
        selected, warnings = select_holder_period(periods, args.allow_partial_holder_period)
        holder_rows = fetch_holder_rows(client, selected)
        candidates = build_holder_candidates(holder_rows, metadata, [])
        enriched, preliminary = [], []
        def evaluate_page(fund):
            url = FUND_PAGE_URL.format(code=fund["code"])
            try:
                page = client.get_text(url, referer=url)
                currency = re.search(r'var currency = "([^"]+)"', page)
                if currency and currency[1] in ("美元", "港币", "港元", "USD", "HKD"):
                    return {**fund, "fund_page_url": url, "_early_exclusion": ("foreign_currency_share", "非人民币份额")}
                if "认购期：" in page and re.search(r"成\s*立\s*日</span>[：:]\s*--", page):
                    return {**fund, "fund_page_url": url, "_early_exclusion": ("not_incepted", "募集期尚未成立")}
                return {**fund, **parse_fund_page(page, fund["code"])}
            except (DataError, OSError, ValueError) as exc:
                audit.event(fund["code"], "fund_page", "blocked", "fund_page_unresolved", str(exc), sources=[url])
                raise
        audit.stage = "fund_page"
        enriched = evaluate_batch(candidates, evaluate_page, 12)
        for fund in enriched:
            code = fund["code"]
            audit.event(code, "preliminary", "evaluated", "fund_page", "已读取基金资料",
                        values={k: fund.get(k) for k in ("purchase_status", "inception_date", "institution_holding_ratio_pct", "scale_billion_cny")},
                        thresholds={"min_age_years": args.min_age_years}, sources=[fund["fund_page_url"]], data_date=selected.report_date)
            if fund.get("_early_exclusion"):
                reason, label = fund["_early_exclusion"]
                audit.event(code, "preliminary", "excluded", reason, label, sources=[fund["fund_page_url"]])
                continue
            if fund["purchase_status"] == "unknown":
                audit.event(code, "preliminary", "blocked", "purchase_unknown", "申购状态无法确定")
                raise DataError(f"{code}: purchase status is unknown")
            if fund["purchase_status"] == "suspended":
                audit.event(code, "preliminary", "excluded", "purchase_suspended", "暂停申购")
            elif not is_older_than_years(fund["inception_date"], as_of, args.min_age_years):
                audit.event(code, "preliminary", "excluded", "inception_too_recent", "成立未严格超过年限门槛")
            elif args.min_scale is not None and fund["scale_billion_cny"] <= args.min_scale:
                audit.event(code, "preliminary", "excluded", "scale_below_threshold", "未达到自定义规模门槛")
            else:
                preliminary.append(fund)
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
            audit=args._audit,
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
    def evaluator(fund):
        try:
            result = _evaluate_documents(args, client, as_of, resources, fund)
            args._audit.event(fund["code"], "documents", "evaluated", "documents_resolved", "合同、持仓与额度评估完成",
                              values={"exposure": result["exposure"], "quota": result["quota"], "quota_error": result["quota_error"]},
                              thresholds={"min_us_equity_pct": args.min_us_equity_pct, "min_direct_limit_cny": args.min_direct_limit_cny},
                              sources=[result["exposure"].get("source_url", "")])
            return result
        except (DataError, OSError, ValueError) as exc:
            args._audit.event(fund["code"], "documents", "blocked", "documents_unresolved", str(exc))
            raise
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
    for fund in (*us_quota.qualified, *global_quota.qualified):
        if fund.get("institution_holding_ratio_pct") is None:
            args._audit.event(fund["code"], "ranking", "blocked", "holder_missing", "资格通过但机构持仓缺失", data_date=holder_report_date)
            raise DataError(f"{fund['code']}: qualified candidate is missing institution holding data for {holder_report_date}")
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
        records, warnings, missing, comparison_window = build_nasdaq100_otc_records(
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
    return NasdaqStage(tuple(records), tuple(warnings), missing, comparison_window)


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
