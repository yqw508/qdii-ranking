"""Run-scoped orchestration and the complete ranking pipeline."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from .assemblers import build_nasdaq100_otc_records, build_output_record
from .cache.announcements import AnnouncementIndexCache, PeriodicReportCache
from .cache.benchmark import Nasdaq100BenchmarkCache
from .cache.contracts import ContractProfileResultCache
from .cache.exposure import FundExposureResultCache
from .cache.performance import PerformanceResultCache
from .cache.premium import ExchangePremiumHoldingCostCache
from .cache.quota import QuotaNoticeParseCache
from .config import (
    ANNOUNCEMENT_API_URL,
    DEFAULT_US_EQUITY_ETF_CATALOG,
    DOCUMENT_WORKERS,
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
from .errors import DataError
from .ranking import (
    build_holder_candidates,
    calculate_return_drawdown_ratio,
    direct_limit_qualifies,
    evaluate_performance_full_scan,
    filter_and_rank,
    global_supplement_sort_key,
    ranking_route,
    us_main_sort_key,
)
from .runtime import (
    HttpClient,
    RunMetrics,
    SHANGHAI_TZ,
    current_shanghai_date,
    parse_date,
)
from .sources.announcements import fetch_latest_periodic_report
from .sources.contracts import ContractBenchmarkCatalog
from .sources.exposure import LookthroughResolver, fetch_us_equity_exposure
from .sources.fund import enrich_fund_pages, fetch_fund_metadata
from .sources.holder import fetch_holder_periods, fetch_holder_rows, select_holder_period
from .sources.premium import (
    _load_cached_qdii_exchange_premium_catalog,
    attach_exchange_premium_holding_costs,
    build_exchange_premium_holding_costs,
    build_exchange_premium_snapshot,
    exchange_premium_market_url,
    load_exchange_premium_catalog,
    load_qdii_exchange_premium_catalog,
)
from .sources.quota import resolve_quota


PerformanceTuple = tuple[dict[str, Any], list[str]]


@dataclass
class RunMemo:
    as_of: date
    performance: dict[str, PerformanceTuple] = field(default_factory=dict)
    fund_pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    announcements: dict[str, Any] = field(default_factory=dict)

    def get_performance(self, code: str) -> PerformanceTuple | None:
        return self.performance.get(code)

    def put_performance(self, code: str, result: PerformanceTuple) -> PerformanceTuple:
        self.performance[code] = result
        return result


T = TypeVar("T")
R = TypeVar("R")


def evaluate_batch(
    items: Sequence[T],
    evaluator: Callable[[T], R],
    max_workers: int,
) -> list[R]:
    """Evaluate a stage concurrently while preserving input order.

    Keeping scheduling in one helper prevents each pipeline stage from
    implementing a subtly different completion/order/error policy.
    """
    if not items:
        return []
    results: list[R | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as executor:
        futures = {
            executor.submit(evaluator, item): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [result for result in results if result is not None]


@dataclass(frozen=True)
class RoutedCandidates:
    us_main: tuple[dict[str, Any], ...]
    global_supplement: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


def route_candidates(
    candidates: Sequence[dict[str, Any]],
    min_us_equity_pct: float,
    exclude_keywords: Sequence[str],
    route: Callable[[str, dict[str, Any], float, Sequence[str]], tuple[str, str]],
) -> RoutedCandidates:
    us_main: list[dict[str, Any]] = []
    global_supplement: list[dict[str, Any]] = []
    warnings: list[str] = []
    for fund in candidates:
        result = fund["_document_result"]
        exposure = result["exposure"]
        warnings.extend(f"{fund['code']} {warning}" for warning in result["exposure_warnings"])
        ranking_list, routing_reason = route(
            fund["name"], exposure, min_us_equity_pct, exclude_keywords
        )
        routed = {
            **fund,
            "us_equity_exposure": exposure,
            "routing_reason": routing_reason,
        }
        if ranking_list == "us_main":
            if not isinstance(fund.get("nasdaq100_fit"), dict):
                detail = fund.get("nasdaq100_fit_error") or "unknown calculation error"
                raise ValueError(
                    f"Nasdaq-100 fit is unavailable for US-main fund {fund['code']}: {detail}"
                )
            us_main.append(routed)
        else:
            global_supplement.append(routed)
    return RoutedCandidates(tuple(us_main), tuple(global_supplement), tuple(warnings))


@dataclass(frozen=True)
class QuotaGateResult:
    qualified: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    exclusions: tuple[tuple[str, str, str], ...]


def apply_quota_gate(
    candidates: Sequence[dict[str, Any]],
    min_direct_limit_cny: int,
    limit_qualifies: Callable[[dict[str, Any], int], bool],
) -> QuotaGateResult:
    qualified: list[dict[str, Any]] = []
    warnings: list[str] = []
    exclusions: list[tuple[str, str, str]] = []
    for fund in candidates:
        result = fund["_document_result"]
        quota = result["quota"]
        quota_warnings = result["quota_warnings"]
        quota_error = result["quota_error"]
        if quota_error is not None or quota is None:
            warnings.append(f"额度剔除 {fund['code']}：{quota_error or '额度结果缺失'}")
            exclusions.append(("quota_unresolved", "申购额度无法可靠解析", fund["code"]))
            continue
        warnings.extend(quota_warnings)
        if any("quota notice could not be parsed" in warning for warning in quota_warnings):
            warnings.append(f"额度剔除 {fund['code']}：存在无法解析的有效期内额度公告。")
            exclusions.append(("quota_unresolved", "申购额度无法可靠解析", fund["code"]))
            continue
        direct = quota["direct_limit"]
        if direct.get("status") == "unknown":
            warnings.append(f"额度剔除 {fund['code']}：直销额度无法可靠解析。")
            exclusions.append(("quota_unresolved", "申购额度无法可靠解析", fund["code"]))
            continue
        if not limit_qualifies(direct, min_direct_limit_cny):
            exclusions.append(
                (
                    "direct_limit_below_threshold",
                    f"直销额度低于 {min_direct_limit_cny:,} 元",
                    fund["code"],
                )
            )
            continue
        qualified.append({**fund, **quota})
    return QuotaGateResult(tuple(qualified), tuple(warnings), tuple(exclusions))


@dataclass(frozen=True)
class RankedCandidates:
    us_qualified: tuple[dict[str, Any], ...]
    global_qualified: tuple[dict[str, Any], ...]
    us_ranked: tuple[dict[str, Any], ...]
    global_ranked: tuple[dict[str, Any], ...]
    exclusions: tuple[tuple[str, str, str], ...]


def rank_candidates(
    us_candidates: Sequence[dict[str, Any]],
    global_candidates: Sequence[dict[str, Any]],
    top: int,
    us_sort_key: Callable[[dict[str, Any]], tuple[Any, ...]],
    global_sort_key: Callable[[dict[str, Any]], tuple[Any, ...]],
    return_drawdown: Callable[[dict[str, Any]], tuple[float, float]],
) -> RankedCandidates:
    global_scored: list[dict[str, Any]] = []
    for fund in global_candidates:
        score, annualized = return_drawdown(fund)
        global_scored.append(
            {
                **fund,
                "_return_drawdown_ratio": score,
                "_three_year_annualized_return_pct": annualized,
            }
        )
    us_qualified = tuple(us_candidates)
    global_qualified = tuple(global_scored)
    us_ranked = tuple(sorted(us_qualified, key=us_sort_key)[:top])
    global_ranked = tuple(sorted(global_qualified, key=global_sort_key)[:top])
    exclusions: list[tuple[str, str, str]] = []
    us_ranked_codes = {fund["code"] for fund in us_ranked}
    global_ranked_codes = {fund["code"] for fund in global_ranked}
    exclusions.extend(
        ("ranking_cap", f"超过每榜前 {top} 只上限", fund["code"])
        for fund in us_qualified
        if fund["code"] not in us_ranked_codes
    )
    exclusions.extend(
        ("ranking_cap", f"超过每榜前 {top} 只上限", fund["code"])
        for fund in global_qualified
        if fund["code"] not in global_ranked_codes
    )
    return RankedCandidates(
        us_qualified,
        global_qualified,
        us_ranked,
        global_ranked,
        tuple(exclusions),
    )


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
