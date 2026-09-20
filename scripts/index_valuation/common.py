#!/usr/bin/env python3
"""Build the standalone multi-asset valuation research page."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from threading import get_ident
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 3
CACHE_SCHEMA_VERSION = 3
WINDOW_MONTHS = 120
# Full Nasdaq scans intentionally fetch a small history buffer before the
# nominal 120-month window.  DQYDJ can lag the current ended month by one or
# more publication cycles; without the buffer a source that ends one month
# earlier leaves only 119 common months even though all sources have enough
# history to build the required window.
FULL_SCAN_BUFFER_MONTHS = 2
TAIL_DAYS = 100
NDXTMC_LIVE_START = date(2022, 3, 19)
NDXTMC_HISTORY_CHUNK_DAYS = 366
SHANGHAI_TZ = timezone(timedelta(hours=8))
DEFAULT_CATALOG = (
    Path(__file__).resolve().parents[2] / "references" / "index-valuation-catalog.json"
)
DEFAULT_PAGE_SCRIPT = Path(__file__).resolve().parents[1] / "valuation_page.js"
DEFAULT_OUTPUT_DIR = Path.cwd() / "output" / "index-valuation"
DEFAULT_PUBLISH_DIR = Path.cwd() / "public"
DEFAULT_CACHE_DIR = (
    Path.cwd() / "output" / "qdii-ranking" / "cache" / "index-valuation"
)
SNOWBALL_SOURCE_ID = "snowball"
GOLD_SOURCE_ID = "gold"
DQYDJ_SOURCE_ID = "dqydj"
NASDAQ_TICKERS = ("RSP", "EQWL", "EWU", "SPY")
NDXTMC_SOURCE_ID = "nasdaq-ndxtmc"
DIRECT_ASSET_IDS = ("nasdaq-100", "sp-500", "dax")
PROXY_ASSET_IDS = (
    "sp-500-equal-weight",
    "sp-100-equal-weight",
    "ftse-100-proxy",
    "nasdaq-100-technology",
)
RELATIVE_PROXY_ASSET_ID = "nasdaq-100-technology"
GOLD_ASSET_ID = "gold-dual-anchor"
EXPECTED_ASSET_IDS = DIRECT_ASSET_IDS + PROXY_ASSET_IDS + (GOLD_ASSET_ID,)
PERFORMANCE_TARGETS = {
    "hot_seconds": 10.0,
    "cold_seconds": 15.0,
    "hot_download_bytes": 300_000,
}
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
NASDAQ_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
NDXTMC_WORKBOOK_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
    "Referer": "https://indexes.nasdaqomx.com/Index/Overview/NDXTMC",
}
NDXTMC_HISTORY_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "https://indexes.nasdaqomx.com/Index/Overview/NDXTMC",
    "X-Requested-With": "XMLHttpRequest",
}
DQYDJ_HEADERS = {
    **BASE_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SNOWBALL_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://danjuanfunds.com/rn/value-center",
}
GOLD_HEADERS = {**DQYDJ_HEADERS}
SOURCE_RATING_LABELS = {
    "low": "偏低",
    "normal": "适中",
    "mid": "适中",
    "middle": "适中",
    "high": "偏高",
}


def source_cache_id(ticker: str) -> str:
    return f"nasdaq-{ticker.lower()}"


class ValuationError(RuntimeError):
    """Raised when valuation artifacts cannot be generated safely."""


@dataclass(frozen=True)
class FetchResponse:
    body: bytes
    elapsed_seconds: float
    attempts: int
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)


class HttpClient:
    def __init__(self, timeout: float = 30.0, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries

    def fetch(self, url: str, headers: dict[str, str]) -> FetchResponse:
        return self._request(url, headers, None)

    def post_form(
        self, url: str, fields: dict[str, str], headers: dict[str, str]
    ) -> FetchResponse:
        return self._request(url, headers, urllib.parse.urlencode(fields).encode("ascii"))

    def _request(
        self, url: str, headers: dict[str, str], body_data: bytes | None
    ) -> FetchResponse:
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                request = urllib.request.Request(url, headers=headers, data=body_data)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    status = int(response.status)
                    response_headers = {key.lower(): value for key, value in response.headers.items()}
                if status == 304:
                    body = b""
                elif status != 200 or not body:
                    raise ValuationError(f"HTTP {status} or empty response")
                return FetchResponse(
                    body=body,
                    elapsed_seconds=time.perf_counter() - started,
                    attempts=attempt,
                    status=status,
                    url=url,
                    headers=response_headers,
                )
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return FetchResponse(
                        body=b"",
                        elapsed_seconds=time.perf_counter() - started,
                        attempts=attempt,
                        status=304,
                        url=url,
                        headers={key.lower(): value for key, value in exc.headers.items()},
                    )
                last_error = exc
            except (OSError, urllib.error.URLError, ValuationError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(0.4 * attempt)
        raise ValuationError(f"request failed after {self.retries} attempts: {last_error}")


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.text: list[str] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def current_shanghai_time() -> datetime:
    return datetime.now(SHANGHAI_TZ).replace(microsecond=0)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{get_ident()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def month_start(month: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError(f"Invalid month: {month!r}")
    return datetime.strptime(month + "-01", "%Y-%m-%d").date()


def add_months(month: str, offset: int) -> str:
    value = month_start(month)
    ordinal = value.year * 12 + value.month - 1 + offset
    return f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}"


def last_complete_month(as_of: date) -> str:
    return (as_of.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def positive_number(value: Any, label: str) -> float:
    cleaned = re.sub(r"[^0-9.+-]", "", str(value or ""))
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise ValuationError(f"Invalid {label}: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValuationError(f"Invalid {label}: {value!r}")
    return number


def finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValuationError(f"Invalid {label}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValuationError(f"Invalid {label}: {value!r}")
    return number
