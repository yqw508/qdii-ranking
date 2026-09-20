"""Public output contracts shared by renderers and validators."""

from __future__ import annotations

from typing import Any, TypedDict

from .config import RANKING_SCHEMA_VERSION


class HoldingCostContract(TypedDict, total=False):
    status: str
    annualized_pct: float | None
    measurement_date: str | None
    source_title: str | None
    source_published_date: str | None
    source_url: str | None


class RankingRecordContract(TypedDict, total=False):
    rank: int
    ranking_list: str
    routing_reason: str
    code: str
    name: str
    fund_type: str
    contract_benchmark: dict[str, Any]
    holding_cost: HoldingCostContract
    two_year_return_pct: float | None
    two_year_max_drawdown_pct: float | None
    common_period_return_pct: float | None
    common_period_max_drawdown_pct: float | None
    common_period_performance_start_date: str | None
    common_period_performance_end_date: str | None
    nasdaq100_fit_common_period: dict[str, Any] | None
    common_period_error: str | None
    three_year_return_pct: float | None
    scale_billion_cny: float | None
    purchase_status: str


class RankingPayloadContract(TypedDict, total=False):
    schema_version: int
    run_date: str
    generated_at: str
    filters: dict[str, Any]
    records: list[RankingRecordContract]
    global_supplement: dict[str, Any]
    nasdaq100_otc: dict[str, Any]
    exchange_premium: dict[str, Any]
    exclusion_summary: list[dict[str, Any]]
    warnings: list[str]
    sources: dict[str, Any]


def ensure_schema(payload: dict[str, Any]) -> None:
    """Fail early when a renderer is handed a payload from another schema."""
    if payload.get("schema_version") != RANKING_SCHEMA_VERSION:
        raise ValueError(
            f"Expected ranking schema {RANKING_SCHEMA_VERSION}, "
            f"got {payload.get('schema_version')}"
        )
