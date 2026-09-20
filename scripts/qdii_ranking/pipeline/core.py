"""Pure orchestration helpers shared by ranking pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Callable, Sequence, TypeVar


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

