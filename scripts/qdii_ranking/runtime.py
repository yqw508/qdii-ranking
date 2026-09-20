"""Run-scoped metrics, dates, and the ranking HTTP client."""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Any, Iterator

from .config import (
    ANNOUNCEMENT_API_URL,
    ETF_LOF_NAV_API_URL,
    ETF_MARKET_LIST_API_URL,
    ETF_QUOTE_API_URL,
    FUND_LIST_URL,
    HOLDER_API_URL,
    USER_AGENT,
)
from .errors import DataError
from .transport import HttpTransport


SHANGHAI_TZ = timezone(timedelta(hours=8))


class RunMetrics:
    def __init__(self) -> None:
        self.phase_seconds: dict[str, float] = {}
        self.counters: dict[str, int] = {}
        self._lock = Lock()

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            with self._lock:
                self.phase_seconds[name] = round(
                    self.phase_seconds.get(name, 0.0) + elapsed, 3
                )

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + amount

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "phase_seconds": dict(self.phase_seconds),
                "counters": dict(self.counters),
            }


def http_category(url: str) -> str:
    if "pingzhongdata" in url:
        return "nav_history"
    if url.startswith(ANNOUNCEMENT_API_URL):
        return "announcement_index"
    if url.startswith("https://pdf.dfcfw.com/"):
        return "announcement_pdf"
    if url.startswith(HOLDER_API_URL):
        return "holder_data"
    if "indexes.nasdaq.com" in url or "safe.gov.cn" in url:
        return "benchmark"
    if url.startswith((ETF_QUOTE_API_URL, ETF_MARKET_LIST_API_URL, ETF_LOF_NAV_API_URL)):
        return "exchange_premium"
    if url.startswith("https://fund.eastmoney.com/") and url.endswith(".html"):
        return "fund_page"
    if url == FUND_LIST_URL:
        return "fund_list"
    return "other"


class HttpClient(HttpTransport):
    """Compatibility client backed by the shared transport implementation."""

    _category = staticmethod(http_category)

    def __init__(self, retries: int = 4, timeout: int = 30) -> None:
        super().__init__(
            retries=retries,
            timeout=timeout,
            user_agent=USER_AGENT,
            category_resolver=http_category,
            error_type=DataError,
        )

    def metrics(self) -> dict[str, dict[str, float | int]]:
        return self.metrics_snapshot()


def parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def current_shanghai_date() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def is_older_than_years(inception_date: str, as_of: date, years: int) -> bool:
    return parse_date(inception_date) < years_ago(as_of, years)


__all__ = [
    "HttpClient",
    "RunMetrics",
    "SHANGHAI_TZ",
    "current_shanghai_date",
    "http_category",
    "is_older_than_years",
    "parse_date",
    "years_ago",
]
