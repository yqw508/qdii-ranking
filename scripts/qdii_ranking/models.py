"""Domain objects used while evaluating a ranking run.

These objects intentionally cover the stable boundaries first. Source-specific
metadata can still be represented as dictionaries until its parser is moved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import (
    ANNOUNCEMENT_PDF_URL,
    BENCHMARK_MAX_STALENESS_DAYS,
    BENCHMARK_WINDOW_YEARS,
    NASDAQ100_HISTORY_PAGE_URL,
    NASDAQ100_MIN_OBSERVATIONS,
    NASDAQ100_MIN_SPAN_DAYS,
    SAFE_USD_CNY_HISTORY_URL,
)


@dataclass(frozen=True)
class FundIdentity:
    code: str
    name: str
    fund_type: str


@dataclass(frozen=True)
class NavPoint:
    observed: date
    nav: float
    equity_return_pct: float | None
    unit_money: str


@dataclass(frozen=True)
class HolderPeriod:
    report_date: str
    fund_count: int
    period_key: str


@dataclass(frozen=True)
class PeriodicReport:
    announcement_id: str
    title: str
    report_date: date
    published_date: date
    source_url: str


@dataclass(frozen=True)
class LegalDocument:
    announcement_id: str
    title: str
    published_date: date
    source_url: str
    document_type: str


@dataclass(frozen=True)
class AnnouncementRecord:
    announcement_id: str
    title: str
    published_date: date

    @property
    def source_url(self) -> str:
        return ANNOUNCEMENT_PDF_URL.format(announcement_id=self.announcement_id)


@dataclass(frozen=True)
class FundAnnouncementSnapshot:
    code: str
    as_of: date
    items: tuple[AnnouncementRecord, ...]
    latest_page_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Nasdaq100Benchmark:
    xndx_levels: dict[date, float]
    usd_cny_rates: dict[date, float]

    def metadata(self) -> dict[str, Any]:
        xndx_dates = sorted(self.xndx_levels)
        fx_dates = sorted(self.usd_cny_rates)
        return {
            "symbol": "XNDX",
            "name": "NASDAQ-100 Total Return",
            "return_type": "gross_total_return",
            "currency": "CNY",
            "window_years": BENCHMARK_WINDOW_YEARS,
            "frequency": "weekly",
            "max_source_staleness_days": BENCHMARK_MAX_STALENESS_DAYS,
            "min_observations": NASDAQ100_MIN_OBSERVATIONS,
            "min_span_days": NASDAQ100_MIN_SPAN_DAYS,
            "index_source_url": NASDAQ100_HISTORY_PAGE_URL,
            "fx_source_url": SAFE_USD_CNY_HISTORY_URL,
            "index_start_date": xndx_dates[0].isoformat(),
            "index_latest_date": xndx_dates[-1].isoformat(),
            "fx_start_date": fx_dates[0].isoformat(),
            "fx_latest_date": fx_dates[-1].isoformat(),
        }


@dataclass(frozen=True)
class PerformanceResult:
    values: dict[str, Any]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WarningItem:
    message: str
    code: str | None = None
    blocking: bool = False


@dataclass
class RunContext:
    """Mutable run-scoped state shared by pipeline stages."""

    as_of: date
    warnings: list[str] = field(default_factory=list)
    performance_results: dict[str, PerformanceResult] = field(default_factory=dict)
    fund_page_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    announcement_results: dict[str, Any] = field(default_factory=dict)

    def add_warnings(self, warnings: list[str] | tuple[str, ...]) -> None:
        self.warnings.extend(warnings)
