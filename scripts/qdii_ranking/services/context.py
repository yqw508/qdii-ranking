"""Run-scoped state and resources for the ranking pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..cache.announcements import AnnouncementIndexCache, PeriodicReportCache
from ..cache.benchmark import Nasdaq100BenchmarkCache
from ..cache.contracts import ContractProfileResultCache
from ..cache.exposure import FundExposureResultCache
from ..cache.performance import PerformanceResultCache
from ..cache.premium import ExchangePremiumHoldingCostCache
from ..cache.quota import QuotaNoticeParseCache
from ..models import Nasdaq100Benchmark
from ..sources.contracts import ContractBenchmarkCatalog
from ..sources.exposure import LookthroughResolver
from ..pipeline.core import RunMemo


@dataclass
class ExclusionCollector:
    items: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, reason: str, label: str, code: str) -> None:
        item = self.items.setdefault(
            reason, {"reason": reason, "label": label, "codes": []}
        )
        if code not in item["codes"]:
            item["codes"].append(code)

    def public_records(self) -> list[dict[str, Any]]:
        return [
            {**item, "count": len(item["codes"])} for item in self.items.values()
        ]


@dataclass(frozen=True)
class PipelineResources:
    cache_root: Any
    document_cache: PeriodicReportCache
    resolver: LookthroughResolver
    contract_catalog: ContractBenchmarkCatalog
    announcement_cache: AnnouncementIndexCache
    contract_result_cache: ContractProfileResultCache
    quota_notice_cache: QuotaNoticeParseCache
    etf_holding_cost_cache: ExchangePremiumHoldingCostCache
    benchmark_cache: Nasdaq100BenchmarkCache
    performance_cache: PerformanceResultCache
    exposure_cache: FundExposureResultCache
    benchmark: Nasdaq100Benchmark
    memo: RunMemo


@dataclass(frozen=True)
class DiscoveryResult:
    metadata: list[dict[str, Any]]
    selected_period: Any
    holder_rows: list[dict[str, Any]]
    enriched: list[dict[str, Any]]
    preliminary: list[dict[str, Any]]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PerformanceStage:
    qualified: tuple[dict[str, Any], ...]
    scanned_count: int
    warnings: tuple[str, ...]
    rejections: dict[str, list[tuple[str, str]]]


@dataclass(frozen=True)
class DocumentStage:
    classified: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RankingStage:
    us_routed: tuple[dict[str, Any], ...]
    global_routed: tuple[dict[str, Any], ...]
    us_qualified: tuple[dict[str, Any], ...]
    global_qualified: tuple[dict[str, Any], ...]
    records: tuple[dict[str, Any], ...]
    global_records: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class NasdaqStage:
    records: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    missing_fields: dict[str, int]


@dataclass(frozen=True)
class PremiumStage:
    snapshot: dict[str, Any]
    warnings: tuple[str, ...]
