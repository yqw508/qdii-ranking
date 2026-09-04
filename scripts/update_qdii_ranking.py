#!/usr/bin/env python3
"""Build an on-demand ranking of purchasable RMB A-class QDII funds."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, get_ident
from typing import Any, Iterable

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - exercised by the runtime dependency check
    raise SystemExit(
        "Missing dependency: pypdf. Run this script with the Codex bundled Python runtime."
    ) from exc


FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
HOLDER_API_URL = "https://fund.eastmoney.com/data/FundDataPortfolio_Interface.aspx"
ANNOUNCEMENT_API_URL = "https://api.fund.eastmoney.com/f10/JJGG"
FUND_PAGE_URL = "https://fund.eastmoney.com/{code}.html"
PERFORMANCE_DATA_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js?v={cache_buster}"
ANNOUNCEMENT_PDF_URL = "https://pdf.dfcfw.com/pdf/H2_{announcement_id}_1.pdf"
NASDAQ100_HISTORY_PAGE_URL = "https://indexes.nasdaq.com/Index/History/XNDX"
NASDAQ100_HISTORY_DATA_URL = "https://indexes.nasdaq.com/Index/HistoryChartData"
SAFE_USD_CNY_HISTORY_URL = "https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do"
ETF_QUOTE_API_URL = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
ETF_MARKET_LIST_API_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
ETF_LOF_NAV_API_URL = "https://api.fund.eastmoney.com/f10/lsjz"
ETF_QUOTE_PAGE_URL = "https://quote.eastmoney.com/center/gridlist.html#fund_etf"
ETF_MARKET_LIST_PAGE_SIZE = 100
ETF_MARKET_LIST_FS = "m:1+t:9,m:0+t:10"
DEFAULT_EXCLUDE_KEYWORDS = ["亚洲", "中国", "港"]
DEFAULT_US_EQUITY_CATALOG = Path(__file__).resolve().parents[1] / "references" / "us-equity-instruments.json"
DEFAULT_CONTRACT_BENCHMARK_CATALOG = (
    Path(__file__).resolve().parents[1] / "references" / "contract-benchmarks.json"
)
DEFAULT_US_EQUITY_ETF_CATALOG = (
    Path(__file__).resolve().parents[1] / "references" / "us-equity-etfs.json"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
SHANGHAI_TZ = timezone(timedelta(hours=8))
PERFORMANCE_WORKERS = 10
DOCUMENT_WORKERS = 6
PERFORMANCE_CACHE_SCHEMA_VERSION = 5
ANNOUNCEMENT_INDEX_CACHE_SCHEMA_VERSION = 1
CONTRACT_RESULT_CACHE_SCHEMA_VERSION = 1
CONTRACT_RESULT_METHOD_VERSION = 1
QUOTA_NOTICE_CACHE_SCHEMA_VERSION = 1
QUOTA_NOTICE_METHOD_VERSION = 1
BENCHMARK_CACHE_SCHEMA_VERSION = 1
BENCHMARK_WINDOW_YEARS = 3
BENCHMARK_HISTORY_BUFFER_DAYS = 14
BENCHMARK_MAX_STALENESS_DAYS = 7
NASDAQ100_MIN_OBSERVATIONS = 140
NASDAQ100_MIN_SPAN_DAYS = 1000
FUND_EXPOSURE_CACHE_SCHEMA_VERSION = 1
US_EQUITY_METHOD_VERSION = 1
ETF_PREMIUM_CACHE_SCHEMA_VERSION = 1
ETF_PREMIUM_CATALOG_SCHEMA_VERSION = 1
ETF_HOLDING_COST_CACHE_SCHEMA_VERSION = 1
ETF_HOLDING_COST_METHOD_VERSION = 1
ETF_PREMIUM_DELAY_MINUTES = 15
ETF_PREMIUM_GROUP_ORDER = ("标普500", "纳指100", "美国50", "道琼斯", "行业主题")
DEFAULT_MIN_DIRECT_LIMIT_CNY = 200
DEFAULT_MIN_THREE_YEAR_RETURN_PCT = 30.0
DEFAULT_MIN_FIVE_YEAR_RETURN_PCT = 50.0
DEFAULT_MIN_TEN_YEAR_RETURN_PCT = 100.0
ROUTING_REASON_CONFIRMED_US = "confirmed_us_exposure"
ROUTING_REASON_BELOW_US_THRESHOLD = "us_exposure_below_threshold"
ROUTING_REASON_GEOGRAPHY_OVERRIDE = "us_main_name_geography_override"
ROUTING_REASON_LABELS = {
    ROUTING_REASON_CONFIRMED_US: "美股确认达标",
    ROUTING_REASON_BELOW_US_THRESHOLD: "美股确认不足",
    ROUTING_REASON_GEOGRAPHY_OVERRIDE: "地域名称分流",
}
EXCLUDED_FUND_TYPES = {"QDII-纯债", "QDII-混合债", "QDII-商品"}
NOTICE_TITLE_RE = re.compile(
    r"大额申购|申购.{0,20}(?:限额|业务上限)|(?:限额|业务上限).{0,20}申购|恢复.{0,12}申购"
)
REPORT_TITLE_EXCLUDE_RE = re.compile(r"摘要|提示性公告")
DIRECT_CHANNEL_PATTERN = (
    r"(?<!非)直销销售机构|(?<!非)直销机构|直销渠道|直销中心柜台|电子直销平台|网上直销平台"
)
AGENCY_CHANNEL_PATTERN = r"非直销销售机构|代销机构|代销渠道"


class DataError(RuntimeError):
    """Raised when source data is incomplete enough to invalidate the ranking."""


class RunMetrics:
    def __init__(self) -> None:
        self.phase_seconds: dict[str, float] = {}
        self.counters: dict[str, int] = {}
        self._lock = Lock()

    @contextmanager
    def phase(self, name: str):
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


class HttpClient:
    def __init__(self, retries: int = 4, timeout: int = 30) -> None:
        self.retries = retries
        self.timeout = timeout
        self._metrics: dict[str, dict[str, float | int]] = {}
        self._metrics_lock = Lock()

    @staticmethod
    def _category(url: str) -> str:
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

    def _record_call(self, category: str) -> None:
        with self._metrics_lock:
            item = self._metrics.setdefault(
                category,
                {
                    "calls": 0,
                    "attempts": 0,
                    "retries": 0,
                    "bytes": 0,
                    "not_modified": 0,
                    "seconds": 0.0,
                },
            )
            item["calls"] = int(item["calls"]) + 1

    def _record_attempt(
        self,
        category: str,
        elapsed: float,
        body_size: int = 0,
        not_modified: bool = False,
        retry: bool = False,
    ) -> None:
        with self._metrics_lock:
            item = self._metrics[category]
            item["attempts"] = int(item["attempts"]) + 1
            item["retries"] = int(item["retries"]) + int(retry)
            item["bytes"] = int(item["bytes"]) + body_size
            item["not_modified"] = int(item["not_modified"]) + int(not_modified)
            item["seconds"] = round(float(item["seconds"]) + elapsed, 3)

    def _request_bytes(
        self,
        url: str,
        referer: str | None = None,
        extra_headers: dict[str, str] | None = None,
        allow_not_modified: bool = False,
    ) -> tuple[int, bytes, dict[str, str]]:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, headers=headers)
        category = self._category(url)
        self._record_call(category)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    self._record_attempt(
                        category,
                        time.perf_counter() - started,
                        len(body),
                        retry=attempt > 0,
                    )
                    return response.status, body, dict(response.headers.items())
            except urllib.error.HTTPError as exc:
                if allow_not_modified and exc.code == 304:
                    self._record_attempt(
                        category,
                        time.perf_counter() - started,
                        not_modified=True,
                        retry=attempt > 0,
                    )
                    return 304, b"", dict(exc.headers.items())
                last_error = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
            self._record_attempt(
                category, time.perf_counter() - started, retry=attempt > 0
            )
            if attempt + 1 < self.retries:
                time.sleep(0.5 * (2**attempt))
        raise DataError(f"Failed to fetch {url}: {last_error}")

    def get_bytes(self, url: str, referer: str | None = None) -> bytes:
        return self._request_bytes(url, referer=referer)[1]

    def get_conditional_text(
        self,
        url: str,
        referer: str | None = None,
        last_modified: str | None = None,
        encoding: str = "utf-8-sig",
    ) -> tuple[int, str | None, str | None]:
        headers = {"If-Modified-Since": last_modified} if last_modified else None
        status, body, response_headers = self._request_bytes(
            url,
            referer=referer,
            extra_headers=headers,
            allow_not_modified=bool(last_modified),
        )
        response_last_modified = next(
            (
                value
                for key, value in response_headers.items()
                if key.lower() == "last-modified"
            ),
            None,
        )
        return (
            status,
            None if status == 304 else body.decode(encoding, errors="replace"),
            response_last_modified or last_modified,
        )

    def get_text(
        self, url: str, referer: str | None = None, encoding: str = "utf-8-sig"
    ) -> str:
        return self.get_bytes(url, referer=referer).decode(encoding, errors="replace")

    def get_json(self, url: str, referer: str | None = None) -> dict[str, Any]:
        try:
            return json.loads(self.get_text(url, referer=referer))
        except json.JSONDecodeError as exc:
            raise DataError(f"Invalid JSON from {url}: {exc}") from exc

    def post_form_json(
        self, url: str, fields: dict[str, str], referer: str | None = None
    ) -> Any:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(fields).encode("ascii"),
            headers=headers,
            method="POST",
        )
        category = self._category(url)
        self._record_call(category)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    result = json.loads(body.decode("utf-8-sig", errors="replace"))
                    self._record_attempt(
                        category,
                        time.perf_counter() - started,
                        len(body),
                        retry=attempt > 0,
                    )
                    return result
            except (
                json.JSONDecodeError,
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
            ) as exc:
                last_error = exc
                self._record_attempt(
                    category, time.perf_counter() - started, retry=attempt > 0
                )
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (2**attempt))
        raise DataError(f"Failed to fetch {url}: {last_error}")

    def metrics(self) -> dict[str, dict[str, float | int]]:
        with self._metrics_lock:
            return {category: dict(values) for category, values in self._metrics.items()}


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


def period_key(report_date: str) -> str:
    parsed = parse_date(report_date)
    if parsed.month == 6:
        quarter = 2
    elif parsed.month == 12:
        quarter = 4
    else:
        raise DataError(f"Unsupported holder report date: {report_date}")
    return f"{parsed.year}_{quarter}"


def extract_data_array(payload: str) -> list[list[str]]:
    match = re.search(r"data:(\[.*?\]),record:", payload, re.S)
    if not match:
        raise DataError("Could not locate data array in Eastmoney response")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise DataError(f"Could not parse Eastmoney data array: {exc}") from exc


def extract_page_count(payload: str) -> int:
    match = re.search(r'pages:"(\d+)"', payload)
    if not match:
        raise DataError("Could not locate page count in Eastmoney response")
    return int(match.group(1))


def fetch_fund_metadata(client: HttpClient) -> dict[str, dict[str, str]]:
    payload = client.get_text(FUND_LIST_URL, referer="https://fund.eastmoney.com/")
    payload = re.sub(r"^\ufeff?var r =\s*", "", payload).rstrip(";\r\n ")
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DataError(f"Could not parse fund list: {exc}") from exc
    return {
        row[0]: {"code": row[0], "name": row[2], "fund_type": row[3]}
        for row in rows
        if len(row) >= 4
    }


def is_qdii_fund_metadata(metadata: dict[str, Any] | None) -> bool:
    if not metadata:
        return False
    fund_type = str(metadata.get("fund_type", "")).strip()
    return fund_type.startswith("QDII") or fund_type == "指数型-海外股票"


def exchange_premium_market_url(
    page: int = 1, page_size: int = ETF_MARKET_LIST_PAGE_SIZE
) -> str:
    params = {
        "pn": str(page),
        "pz": str(page_size),
        "po": "0",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": ETF_MARKET_LIST_FS,
        "fields": "f2,f3,f6,f12,f13,f14,f18,f124,f297,f402,f441",
    }
    return f"{ETF_MARKET_LIST_API_URL}?{urllib.parse.urlencode(params)}"


def _parse_exchange_premium_market_page(
    payload: Any, page: int
) -> tuple[int, list[dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise DataError(f"ETF market list page {page} has no data object")
    data = payload["data"]
    try:
        total = int(data.get("total"))
    except (TypeError, ValueError) as exc:
        raise DataError(f"ETF market list page {page} has no valid total") from exc
    rows = data.get("diff")
    if not isinstance(rows, list):
        raise DataError(f"ETF market list page {page} has no record list")
    return total, [row for row in rows if isinstance(row, dict)]


def exchange_premium_lof_nav_url(code: str) -> str:
    params = {
        "fundCode": code,
        "pageIndex": "1",
        "pageSize": "1",
        "startDate": "",
        "endDate": "",
    }
    return f"{ETF_LOF_NAV_API_URL}?{urllib.parse.urlencode(params)}"


def parse_exchange_premium_lof_nav(
    payload: Any, code: str, as_of: date
) -> dict[str, Any]:
    rows = (
        ((payload.get("Data") or {}).get("LSJZList"))
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise DataError(f"LOF NAV response contains no record for {code}")
    try:
        observed = parse_date(str(rows[0]["FSRQ"]))
        nav = float(rows[0]["DWJZ"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"LOF NAV response is invalid for {code}") from exc
    if observed > as_of or not math.isfinite(nav) or nav <= 0:
        raise DataError(f"LOF NAV is outside its valid range for {code}")
    return {
        "reference_value_type": "nav",
        "reference_value_cny": round(nav, 4),
        "reference_value_date": observed.isoformat(),
        "reference_value_source_url": exchange_premium_lof_nav_url(code),
    }


def fetch_exchange_premium_lof_navs(
    client: HttpClient,
    entries: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    as_of: date,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    entry_by_code = {entry["code"]: entry for entry in entries}
    candidates: dict[str, dict[str, Any]] = {}
    for raw in rows:
        code = str(raw.get("f12", "")) if isinstance(raw, dict) else ""
        entry = entry_by_code.get(code)
        if (
            entry
            and raw.get("f441") in {None, "", "-"}
            and all(raw.get(field) not in {None, "", "-"} for field in ("f2", "f402", "f3", "f6"))
        ):
            candidates[code] = entry

    references: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    def fetch(code: str) -> tuple[str, dict[str, Any]]:
        url = exchange_premium_lof_nav_url(code)
        payload = client.get_json(
            url, referer=f"https://fundf10.eastmoney.com/jjjz_{code}.html"
        )
        return code, parse_exchange_premium_lof_nav(payload, code, as_of)

    with ThreadPoolExecutor(max_workers=min(PERFORMANCE_WORKERS, len(candidates) or 1)) as executor:
        futures = {executor.submit(fetch, code): code for code in candidates}
        for future in as_completed(futures):
            code = futures[future]
            try:
                resolved_code, reference = future.result()
                references[resolved_code] = reference
            except (DataError, OSError, ValueError) as exc:
                warnings.append(f"场内溢价告警 {code}：LOF 最新单位净值无法读取：{exc}")
    return references, warnings


def load_qdii_exchange_premium_catalog(
    client: HttpClient, metadata: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    """Load all listed funds, then retain the repository's QDII fund scope."""
    first_page = 1
    total, first_rows = _parse_exchange_premium_market_page(
        client.get_json(exchange_premium_market_url(first_page), referer=ETF_QUOTE_PAGE_URL),
        first_page,
    )
    if total <= 0:
        raise DataError("ETF market list returned no records")
    page_count = math.ceil(total / ETF_MARKET_LIST_PAGE_SIZE)
    all_rows = list(first_rows)
    for page in range(2, page_count + 1):
        page_total, rows = _parse_exchange_premium_market_page(
            client.get_json(exchange_premium_market_url(page), referer=ETF_QUOTE_PAGE_URL),
            page,
        )
        if page_total != total:
            raise DataError("ETF market list total changed during pagination")
        all_rows.extend(rows)
    if len(all_rows) != total:
        raise DataError(
            f"ETF market list is incomplete: expected {total}, got {len(all_rows)}"
        )

    entries: list[dict[str, Any]] = []
    quote_rows: dict[str, dict[str, Any]] = {}
    seen_codes: set[str] = set()
    for raw in all_rows:
        code = str(raw.get("f12", ""))
        market_id = raw.get("f13")
        fund = metadata.get(code)
        if (
            not re.fullmatch(r"\d{6}", code)
            or market_id not in {0, 1}
            or code in seen_codes
            or not is_qdii_fund_metadata(fund)
        ):
            continue
        seen_codes.add(code)
        fund_type = str(fund.get("fund_type", "")).strip()
        entries.append(
            {
                "code": code,
                "name": str(raw.get("f14") or fund.get("name") or code).strip(),
                "exchange": "SSE" if int(market_id) == 1 else "SZSE",
                "market_id": int(market_id),
                "category": "qdii",
                "benchmark_group": fund_type or "QDII",
                "fund_type": fund_type,
                "source_url": ETF_QUOTE_PAGE_URL,
            }
        )
        quote_rows[code] = raw
    if not entries:
        raise DataError("No listed QDII funds matched the fund metadata")
    entries.sort(key=lambda item: item["code"])
    fingerprint = hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return entries, quote_rows, fingerprint


def fetch_holder_periods(client: HttpClient) -> list[HolderPeriod]:
    params = {
        "dt": "11",
        "pi": "1",
        "pn": "100",
        "st": "desc",
        "sc": "reportdate",
        "mc": "hypzDetail",
    }
    url = f"{HOLDER_API_URL}?{urllib.parse.urlencode(params)}"
    payload = client.get_text(url, referer="https://fund.eastmoney.com/data/cyrjglist.html")
    periods: list[HolderPeriod] = []
    for row in extract_data_array(payload):
        if len(row) < 2 or not row[1]:
            continue
        try:
            periods.append(HolderPeriod(row[0], int(row[1]), period_key(row[0])))
        except (ValueError, DataError):
            continue
    if not periods:
        raise DataError("No holder report periods were returned")
    return periods


def select_holder_period(
    periods: list[HolderPeriod], allow_partial: bool = False, coverage: float = 0.95
) -> tuple[HolderPeriod, list[str]]:
    periods = sorted(periods, key=lambda item: item.report_date, reverse=True)
    warnings: list[str] = []
    if allow_partial:
        return periods[0], warnings
    for index, candidate in enumerate(periods):
        if index + 1 >= len(periods):
            return candidate, warnings
        previous = periods[index + 1]
        threshold = math.ceil(previous.fund_count * coverage)
        if candidate.fund_count >= threshold:
            return candidate, warnings
        warnings.append(
            f"跳过未完整披露的持有人报告期 {candidate.report_date}："
            f"仅 {candidate.fund_count} 只基金，低于上一完整报告期 "
            f"{previous.report_date}（{previous.fund_count} 只）的 {coverage:.0%}。"
        )
    raise DataError("No complete holder report period could be selected")


def fetch_holder_rows(
    client: HttpClient, period: HolderPeriod, workers: int = 16
) -> list[list[str]]:
    def url_for(page: int) -> str:
        params = {
            "dt": "10",
            "t": period.period_key,
            "pi": str(page),
            "pn": "50",
            "st": "desc",
            "sc": "jgbl",
            "mc": "returnJson",
        }
        return f"{HOLDER_API_URL}?{urllib.parse.urlencode(params)}"

    referer = f"https://fund.eastmoney.com/data/cyrjgdetail.html#t{period.period_key}"
    first = client.get_text(url_for(1), referer=referer)
    pages = extract_page_count(first)
    rows_by_page: dict[int, list[list[str]]] = {1: extract_data_array(first)}

    def fetch_page(page: int) -> tuple[int, list[list[str]]]:
        return page, extract_data_array(client.get_text(url_for(page), referer=referer))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch_page, page) for page in range(2, pages + 1)]
        for future in as_completed(futures):
            page, rows = future.result()
            rows_by_page[page] = rows
    if len(rows_by_page) != pages:
        raise DataError(f"Holder data is incomplete: {len(rows_by_page)}/{pages} pages")
    return [row for page in range(1, pages + 1) for row in rows_by_page[page]]


def is_rmb_a_share(meta: dict[str, str]) -> bool:
    name = meta["name"]
    if not (
        meta["fund_type"].startswith("QDII")
        or meta["fund_type"] == "指数型-海外股票"
    ):
        return False
    if re.search(
        r"美元|港币|后端|人民币[CD]|[CD](?:类)?(?:份额)?人民币|"
        r"(?:\(|（|/|\s)[CD](?:类|份额|\)|）|$)|[CD](?:类|份额|\)|）|$)",
        name,
    ):
        return False
    if re.search(r"人民币A|A(?:类|份额)?人民币|A类|A1(?:\(|$)|A(?:\(|$)", name):
        return True
    return "人民币" in name


def is_otc_share(meta: dict[str, str]) -> bool:
    name = meta["name"]
    if not is_rmb_a_share(meta):
        return False
    return not ("ETF" in name.upper() and "联接" not in name and "LOF" not in name.upper())


def build_holder_candidates(
    rows: list[list[str]], metadata: dict[str, dict[str, str]], keywords: list[str]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 6 or row[0] not in metadata or not row[2]:
            continue
        meta = metadata[row[0]]
        if not is_otc_share(meta):
            continue
        if meta["fund_type"] in EXCLUDED_FUND_TYPES:
            continue
        if any(keyword and keyword in meta["name"] for keyword in keywords):
            continue
        try:
            ratio = float(row[2])
        except ValueError:
            continue
        candidates.append(
            {
                **meta,
                "institution_holding_ratio_pct": ratio,
                "personal_holding_ratio_pct": float(row[3]) if row[3] else None,
                "holder_total_shares_100m": float(row[5].replace(",", "")) if row[5] else None,
            }
        )
    candidates.sort(key=lambda item: (-item["institution_holding_ratio_pct"], item["code"]))
    return candidates


def strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def amount_to_billion(value: str, unit: str) -> float:
    number = float(value.replace(",", ""))
    if unit == "亿元":
        return number
    if unit == "万元":
        return number / 10000
    return number / 100000000


def parse_fund_page(page: str, code: str) -> dict[str, Any]:
    scale_match = re.search(
        r"规模</a>：\s*([\d,.]+)\s*(亿元|万元|元)（(\d{4}-\d{2}-\d{2})）", page
    )
    if not scale_match:
        raise DataError(f"Could not parse scale for fund {code}")
    inception_match = re.search(
        r"成\s*立\s*日</span>[：:]\s*(\d{4}-\d{2}-\d{2})", page
    )
    if not inception_match:
        raise DataError(f"Could not parse inception date for fund {code}")
    scale = amount_to_billion(scale_match.group(1), scale_match.group(2))
    buy_match = re.search(r'var fundBuyStatus = "([^"]+)"', page)
    sale_match = re.search(r"var fundIsSale = (true|false)", page)
    state_match = re.search(r"交易状态：(.{0,450}?)购买手续费", page, re.S)
    state_text = strip_tags(state_match.group(1)) if state_match else ""
    buy_status = buy_match.group(1) if buy_match else None
    is_sale = sale_match.group(1) == "true" if sale_match else None

    if "暂停申购" in state_text or buy_status == "4" or is_sale is False:
        purchase_status = "suspended"
    elif "限大额" in state_text:
        purchase_status = "limited"
    elif "开放申购" in state_text or (buy_status == "1" and is_sale is True):
        purchase_status = "open"
    else:
        purchase_status = "unknown"

    page_limit_match = re.search(r"单日累计购买上限\s*([\d,.]+)\s*(万元|元)", state_text)
    page_limit = None
    if page_limit_match:
        page_limit = parse_cny_amount(page_limit_match.group(1), page_limit_match.group(2))
    latest_nav_match = re.search(
        r"单位净值</a></span>\s*\(</span>(\d{4}-\d{2}-\d{2})\)</p>"
        r".*?<dd class=\"dataNums\">\s*<span[^>]*>([\d.]+)</span>",
        page,
        re.S,
    )
    return {
        "inception_date": inception_match.group(1),
        "scale_billion_cny": round(scale, 4),
        "scale_report_date": scale_match.group(3),
        "purchase_status": purchase_status,
        "purchase_status_text": state_text,
        "page_agency_limit_cny": page_limit,
        "latest_nav_date": latest_nav_match.group(1) if latest_nav_match else None,
        "latest_nav_value": (
            float(latest_nav_match.group(2)) if latest_nav_match else None
        ),
        "fund_page_url": FUND_PAGE_URL.format(code=code),
    }


def enrich_fund_pages(
    client: HttpClient, candidates: list[dict[str, Any]], workers: int = 12
) -> list[dict[str, Any]]:
    def fetch(candidate: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        code = candidate["code"]
        url = FUND_PAGE_URL.format(code=code)
        return code, parse_fund_page(client.get_text(url, referer=url), code)

    details: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch, candidate) for candidate in candidates]
        for future in as_completed(futures):
            code, detail = future.result()
            details[code] = detail
    if len(details) != len(candidates):
        raise DataError("Not all candidate fund pages were fetched")
    return [{**candidate, **details[candidate["code"]]} for candidate in candidates]


def filter_and_rank(
    candidates: list[dict[str, Any]],
    min_scale: float | None,
    top: int,
    exclude_keywords: Iterable[str] = (),
    as_of: date | None = None,
    min_age_years: int = 0,
) -> list[dict[str, Any]]:
    eligible = [
        item
        for item in candidates
        if (min_scale is None or item["scale_billion_cny"] > min_scale)
        and item["purchase_status"] in {"open", "limited"}
        and (
            min_age_years == 0
            or (
                as_of is not None
                and is_older_than_years(item["inception_date"], as_of, min_age_years)
            )
        )
        and not any(
            keyword and keyword in item["name"] for keyword in exclude_keywords
        )
    ]
    eligible.sort(key=lambda item: (-item["institution_holding_ratio_pct"], item["code"]))
    return eligible[:top]


def parse_nasdaq100_history(payload: Any, as_of: date) -> dict[date, float]:
    if not isinstance(payload, list):
        raise DataError("Nasdaq XNDX history response is not a list")
    points: dict[date, float] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise DataError("Nasdaq XNDX history contains a non-object row")
        try:
            observed = datetime.fromtimestamp(float(item["x"]) / 1000, timezone.utc).date()
            value = float(item["y"])
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise DataError(f"Invalid Nasdaq XNDX history row: {exc}") from exc
        if observed > as_of:
            continue
        if not math.isfinite(value) or value <= 0:
            raise DataError(f"Invalid Nasdaq XNDX value on {observed}")
        previous = points.get(observed)
        if previous is not None and not math.isclose(previous, value, rel_tol=0, abs_tol=1e-9):
            raise DataError(f"Conflicting Nasdaq XNDX values on {observed}")
        points[observed] = value
    if not points:
        raise DataError("Nasdaq XNDX history response contains no usable points")
    return points


def parse_safe_usd_cny_history(payload: str, as_of: date) -> dict[date, float]:
    points: dict[date, float] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", payload, re.S | re.I):
        cells = [
            html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        ]
        if len(cells) < 2 or re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]) is None:
            continue
        try:
            observed = parse_date(cells[0])
            # SAFE quotes CNY per 100 USD for this direct currency pair.
            value = float(cells[1].replace(",", "")) / 100
        except ValueError as exc:
            raise DataError(f"Invalid SAFE USD/CNY history row for {cells[0]}") from exc
        if observed > as_of:
            continue
        if not math.isfinite(value) or value <= 0:
            raise DataError(f"Invalid SAFE USD/CNY value on {observed}")
        previous = points.get(observed)
        if previous is not None and not math.isclose(previous, value, rel_tol=0, abs_tol=1e-9):
            raise DataError(f"Conflicting SAFE USD/CNY values on {observed}")
        points[observed] = value
    if not points:
        raise DataError("SAFE USD/CNY history response contains no usable points")
    return points


def fetch_nasdaq100_history(
    client: HttpClient, start: date, as_of: date
) -> dict[date, float]:
    payload = client.post_form_json(
        NASDAQ100_HISTORY_DATA_URL,
        {
            "id": "XNDX",
            "startDate": f"{start.isoformat()}T00:00:00.000",
            "endDate": f"{as_of.isoformat()}T00:00:00.000",
        },
        referer=NASDAQ100_HISTORY_PAGE_URL,
    )
    return parse_nasdaq100_history(payload, as_of)


def fetch_safe_usd_cny_history(
    client: HttpClient, start: date, as_of: date
) -> dict[date, float]:
    params = {
        'startDate': start.isoformat(),
        'endDate': as_of.isoformat(),
        'queryYN': 'true',
    }
    url = f"{SAFE_USD_CNY_HISTORY_URL}?{urllib.parse.urlencode(params)}"
    return parse_safe_usd_cny_history(client.get_text(url), as_of)


class Nasdaq100BenchmarkCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.cache_hits = 0
        self.fetches = 0
        self.fallbacks = 0

    @staticmethod
    def _decode_series(payload: Any, value_field: str) -> dict[date, float]:
        if not isinstance(payload, list):
            raise DataError("Benchmark cache series is not a list")
        points: dict[date, float] = {}
        for item in payload:
            if not isinstance(item, dict) or set(item) != {"date", value_field}:
                raise DataError("Benchmark cache row is invalid")
            observed = parse_date(str(item["date"]))
            value = float(item[value_field])
            if not math.isfinite(value) or value <= 0 or observed in points:
                raise DataError("Benchmark cache contains an invalid or duplicate point")
            points[observed] = value
        return points

    def _load(self) -> tuple[dict[date, float], dict[date, float]]:
        if not self.path.exists():
            return {}, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != BENCHMARK_CACHE_SCHEMA_VERSION:
                raise DataError("Benchmark cache schema is incompatible")
            xndx = self._decode_series(payload.get("xndx"), "value")
            fx = self._decode_series(payload.get("usd_cny"), "rate")
            if not xndx or not fx:
                raise DataError("Benchmark cache is empty")
            self.cache_hits += 1
            return xndx, fx
        except (DataError, OSError, ValueError, json.JSONDecodeError):
            return {}, {}

    def _save(self, xndx: dict[date, float], fx: dict[date, float]) -> None:
        write_json(
            self.path,
            {
                "schema_version": BENCHMARK_CACHE_SCHEMA_VERSION,
                "benchmark": "XNDX_CNY",
                "xndx": [
                    {"date": observed.isoformat(), "value": xndx[observed]}
                    for observed in sorted(xndx)
                ],
                "usd_cny": [
                    {"date": observed.isoformat(), "rate": fx[observed]}
                    for observed in sorted(fx)
                ],
            },
        )

    @staticmethod
    def _validate_coverage(
        series: dict[date, float], label: str, required_start: date, as_of: date
    ) -> None:
        if not series or min(series) > required_start + timedelta(
            days=BENCHMARK_MAX_STALENESS_DAYS
        ):
            raise DataError(f"{label} history does not cover the required three-year window")
        latest = max(observed for observed in series if observed <= as_of)
        if (as_of - latest).days > BENCHMARK_MAX_STALENESS_DAYS:
            raise DataError(f"{label} history is stale as of {as_of}: latest {latest}")

    def get(self, client: HttpClient, as_of: date) -> tuple[Nasdaq100Benchmark, list[str]]:
        required_start = years_ago(as_of, BENCHMARK_WINDOW_YEARS) - timedelta(
            days=BENCHMARK_HISTORY_BUFFER_DAYS
        )
        xndx, fx = self._load()
        xndx = {observed: value for observed, value in xndx.items() if observed <= as_of}
        fx = {observed: value for observed, value in fx.items() if observed <= as_of}
        warnings: list[str] = []
        cache_start = required_start - timedelta(days=BENCHMARK_MAX_STALENESS_DAYS)
        if (
            not xndx
            or not fx
            or min(xndx) > required_start + timedelta(days=BENCHMARK_MAX_STALENESS_DAYS)
            or min(fx) > required_start + timedelta(days=BENCHMARK_MAX_STALENESS_DAYS)
        ):
            fetch_start = cache_start
        else:
            fetch_start = max(cache_start, min(max(xndx), max(fx)) - timedelta(days=7))

        sources = (
            ("Nasdaq XNDX", xndx, fetch_nasdaq100_history),
            ("SAFE USD/CNY", fx, fetch_safe_usd_cny_history),
        )
        for label, series, fetcher in sources:
            try:
                series.update(fetcher(client, fetch_start, as_of))
                self.fetches += 1
            except DataError:
                if not series:
                    raise
                self.fallbacks += 1
                warnings.append(
                    f"纳指100基准更新失败，使用完整缓存：{label} 最新数据 "
                    f"{max(observed for observed in series if observed <= as_of).isoformat()}。"
                )

        xndx = {
            observed: value
            for observed, value in xndx.items()
            if cache_start <= observed <= as_of
        }
        fx = {
            observed: value
            for observed, value in fx.items()
            if cache_start <= observed <= as_of
        }
        self._validate_coverage(xndx, "Nasdaq XNDX", required_start, as_of)
        self._validate_coverage(fx, "SAFE USD/CNY", required_start, as_of)
        self._save(xndx, fx)
        return Nasdaq100Benchmark(xndx, fx), warnings

    def stats(self) -> dict[str, int]:
        return {
            "cache_hits": self.cache_hits,
            "fetches": self.fetches,
            "fallbacks": self.fallbacks,
        }


def parse_performance_page(payload: str, code: str) -> list[dict[str, Any]]:
    match = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", payload, re.S)
    if not match:
        raise DataError(f"Could not locate net-worth trend for fund {code}")
    try:
        raw_points = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise DataError(f"Could not parse net-worth trend for fund {code}: {exc}") from exc

    points_by_date: dict[date, dict[str, Any]] = {}
    for item in raw_points:
        try:
            observed = datetime.fromtimestamp(float(item["x"]) / 1000, SHANGHAI_TZ).date()
            nav = float(item["y"])
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise DataError(f"Invalid net-worth point for fund {code}: {exc}") from exc
        if not math.isfinite(nav) or nav <= 0:
            raise DataError(f"Invalid net asset value for fund {code} on {observed}")
        equity_return = item.get("equityReturn")
        try:
            equity_return_pct = (
                float(equity_return) if equity_return not in (None, "") else None
            )
        except (TypeError, ValueError) as exc:
            raise DataError(f"Invalid daily return for fund {code} on {observed}") from exc
        points_by_date[observed] = {
            "date": observed,
            "nav": nav,
            "equity_return_pct": equity_return_pct,
            "unit_money": str(item.get("unitMoney") or ""),
        }
    if not points_by_date:
        raise DataError(f"No net-worth history was returned for fund {code}")
    return [points_by_date[key] for key in sorted(points_by_date)]


def adjusted_daily_factor(
    previous: dict[str, Any], current: dict[str, Any], code: str
) -> float:
    unit_money = current["unit_money"]
    if unit_money:
        dividend = re.search(r"派现金\s*([\d.]+)\s*元", unit_money)
        if dividend:
            factor = (current["nav"] + float(dividend.group(1))) / previous["nav"]
        elif current["equity_return_pct"] is not None:
            factor = 1 + current["equity_return_pct"] / 100
        else:
            raise DataError(
                f"Could not adjust net-worth event for fund {code} on {current['date']}"
            )
    else:
        factor = current["nav"] / previous["nav"]
    if not math.isfinite(factor) or factor <= 0:
        raise DataError(f"Invalid adjusted return factor for fund {code} on {current['date']}")
    return factor


def build_adjusted_wealth_series(
    points: list[dict[str, Any]], code: str, as_of: date
) -> list[tuple[date, float]]:
    available = [point for point in points if point["date"] <= as_of]
    if not available:
        raise DataError(f"No net-worth history on or before {as_of} for fund {code}")
    wealth = 1.0
    series = [(available[0]["date"], wealth)]
    for previous, current in zip(available, available[1:]):
        wealth *= adjusted_daily_factor(previous, current, code)
        if not math.isfinite(wealth) or wealth <= 0:
            raise DataError(f"Invalid adjusted wealth for fund {code} on {current['date']}")
        series.append((current["date"], wealth))
    return series


def latest_series_value(
    series: dict[date, float], ordered_dates: list[date], observed: date
) -> tuple[date, float] | None:
    index = bisect.bisect_right(ordered_dates, observed) - 1
    if index < 0:
        return None
    source_date = ordered_dates[index]
    if (observed - source_date).days > BENCHMARK_MAX_STALENESS_DAYS:
        return None
    return source_date, series[source_date]


def calculate_nasdaq100_fit(
    points: list[dict[str, Any]],
    code: str,
    as_of: date,
    benchmark: Nasdaq100Benchmark,
) -> dict[str, Any]:
    wealth_series = build_adjusted_wealth_series(points, code, as_of)
    end_date = wealth_series[-1][0]
    target_start = years_ago(end_date, BENCHMARK_WINDOW_YEARS)
    weekly: dict[date, tuple[date, float]] = {}
    for observed, wealth in wealth_series:
        if observed < target_start:
            continue
        week_start = observed - timedelta(days=observed.weekday())
        weekly[week_start] = (observed, wealth)

    xndx_dates = sorted(benchmark.xndx_levels)
    fx_dates = sorted(benchmark.usd_cny_rates)
    aligned: list[tuple[date, date, float, float]] = []
    for week_start in sorted(weekly):
        observed, wealth = weekly[week_start]
        xndx = latest_series_value(benchmark.xndx_levels, xndx_dates, observed)
        fx = latest_series_value(benchmark.usd_cny_rates, fx_dates, observed)
        if xndx is None or fx is None:
            continue
        benchmark_level = xndx[1] * fx[1]
        if not math.isfinite(benchmark_level) or benchmark_level <= 0:
            raise DataError(f"Invalid CNY XNDX benchmark level for {code} on {observed}")
        aligned.append((week_start, observed, wealth, benchmark_level))

    fund_returns: list[float] = []
    benchmark_returns: list[float] = []
    interval_dates: list[tuple[date, date]] = []
    for previous, current in zip(aligned, aligned[1:]):
        if (current[0] - previous[0]).days != 7:
            continue
        fund_return = current[2] / previous[2] - 1
        benchmark_return = current[3] / previous[3] - 1
        if not math.isfinite(fund_return) or not math.isfinite(benchmark_return):
            raise DataError(f"Non-finite weekly return for fund {code}")
        fund_returns.append(fund_return)
        benchmark_returns.append(benchmark_return)
        interval_dates.append((previous[1], current[1]))

    observations = len(fund_returns)
    if observations < NASDAQ100_MIN_OBSERVATIONS:
        raise DataError(
            f"Fund {code} has only {observations} valid Nasdaq-100 weekly observations; "
            f"requires {NASDAQ100_MIN_OBSERVATIONS}"
        )
    start_date = interval_dates[0][0]
    fit_end_date = interval_dates[-1][1]
    span_days = (fit_end_date - start_date).days
    if span_days < NASDAQ100_MIN_SPAN_DAYS:
        raise DataError(
            f"Fund {code} Nasdaq-100 fit spans only {span_days} days; "
            f"requires {NASDAQ100_MIN_SPAN_DAYS}"
        )

    fund_mean = statistics.mean(fund_returns)
    benchmark_mean = statistics.mean(benchmark_returns)
    covariance = sum(
        (fund_return - fund_mean) * (benchmark_return - benchmark_mean)
        for fund_return, benchmark_return in zip(fund_returns, benchmark_returns)
    ) / (observations - 1)
    fund_variance = statistics.variance(fund_returns)
    benchmark_variance = statistics.variance(benchmark_returns)
    if fund_variance <= 0 or benchmark_variance <= 0:
        raise DataError(f"Fund {code} Nasdaq-100 fit has zero return variance")
    correlation = covariance / math.sqrt(fund_variance * benchmark_variance)
    correlation = min(1.0, max(-1.0, correlation))
    beta = covariance / benchmark_variance
    tracking_error_pct = statistics.stdev(
        fund_return - benchmark_return
        for fund_return, benchmark_return in zip(fund_returns, benchmark_returns)
    ) * math.sqrt(52) * 100
    if not all(math.isfinite(value) for value in (correlation, beta, tracking_error_pct)):
        raise DataError(f"Fund {code} Nasdaq-100 fit contains a non-finite metric")
    return {
        "correlation": round(correlation, 4),
        "beta": round(beta, 4),
        "tracking_error_pct": round(tracking_error_pct, 2),
        "observations": observations,
        "start_date": start_date.isoformat(),
        "end_date": fit_end_date.isoformat(),
    }


def calculate_trailing_performance(
    points: list[dict[str, Any]], code: str, as_of: date, years: int
) -> dict[str, Any] | None:
    available = [point for point in points if point["date"] <= as_of]
    if not available:
        raise DataError(f"No net-worth history on or before {as_of} for fund {code}")
    end_point = available[-1]
    target_start = years_ago(end_point["date"], years)
    anchor_indexes = [
        index for index, point in enumerate(available) if point["date"] <= target_start
    ]
    if not anchor_indexes:
        return None
    anchor_index = anchor_indexes[-1]
    window = available[anchor_index:]
    wealth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for previous, current in zip(window, window[1:]):
        wealth *= adjusted_daily_factor(previous, current, code)
        peak = max(peak, wealth)
        max_drawdown = min(max_drawdown, wealth / peak - 1)
    return {
        "return_pct": round((wealth - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "start_date": window[0]["date"].isoformat(),
        "end_date": window[-1]["date"].isoformat(),
    }


def calculate_performance_from_points(
    points: list[dict[str, Any]],
    code: str,
    as_of: date,
    source_url: str,
    benchmark: Nasdaq100Benchmark | None = None,
) -> tuple[dict[str, Any], list[str]]:
    output: dict[str, Any] = {"performance_source_url": source_url}
    warnings: list[str] = []
    output["nav_history_start_date"] = points[0]["date"].isoformat()
    output["nav_history_end_date"] = max(
        (point["date"] for point in points if point["date"] <= as_of),
        default=points[0]["date"],
    ).isoformat()
    for years, prefix, label in (
        (1, "one_year", "近一年"),
        (3, "three_year", "近三年"),
        (5, "five_year", "近五年"),
        (10, "ten_year", "近十年"),
    ):
        performance = calculate_trailing_performance(points, code, as_of, years)
        if performance is None:
            output.update(
                {
                    f"{prefix}_return_pct": None,
                    f"{prefix}_max_drawdown_pct": None,
                    f"{prefix}_performance_start_date": None,
                    f"{prefix}_performance_end_date": None,
                }
            )
            if years <= 3:
                warnings.append(
                    f"{code} 的净值历史不足 {years} 年，{label}涨幅和最大回撤无法计算。"
                )
            continue
        output.update(
            {
                f"{prefix}_return_pct": performance["return_pct"],
                f"{prefix}_max_drawdown_pct": performance["max_drawdown_pct"],
                f"{prefix}_performance_start_date": performance["start_date"],
                f"{prefix}_performance_end_date": performance["end_date"],
            }
        )
    if benchmark is not None:
        try:
            output["nasdaq100_fit"] = calculate_nasdaq100_fit(
                points, code, as_of, benchmark
            )
            output["nasdaq100_fit_error"] = None
        except DataError as exc:
            output["nasdaq100_fit"] = None
            output["nasdaq100_fit_error"] = str(exc)
    return output, warnings


def fetch_trailing_performance(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    benchmark: Nasdaq100Benchmark | None = None,
) -> tuple[dict[str, Any], list[str]]:
    code = fund["code"]
    url = PERFORMANCE_DATA_URL.format(code=code, cache_buster=as_of.strftime("%Y%m%d"))
    payload = client.get_text(url, referer=fund["fund_page_url"])
    return calculate_performance_from_points(
        parse_performance_page(payload, code), code, as_of, url, benchmark
    )


class PerformanceResultCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.corrupt_rebuilds = 0
        self.conditional_requests = 0
        self.not_modified = 0
        self.updates = 0
        self._stats_lock = Lock()

    @staticmethod
    def _decode_points(payload: dict[str, Any], code: str) -> tuple[list[dict[str, Any]], str | None]:
        if (
            payload.get("schema_version") != PERFORMANCE_CACHE_SCHEMA_VERSION
            or payload.get("code") != code
            or not isinstance(payload.get("points"), list)
        ):
            raise DataError("Cached NAV history identity is invalid")
        points: list[dict[str, Any]] = []
        previous: date | None = None
        for item in payload["points"]:
            if not isinstance(item, dict) or set(item) != {
                "date",
                "nav",
                "equity_return_pct",
                "unit_money",
            }:
                raise DataError("Cached NAV history row is invalid")
            observed = parse_date(str(item["date"]))
            nav = float(item["nav"])
            daily_return = item["equity_return_pct"]
            if (
                (previous is not None and observed <= previous)
                or not math.isfinite(nav)
                or nav <= 0
                or (
                    daily_return is not None
                    and not math.isfinite(float(daily_return))
                )
            ):
                raise DataError("Cached NAV history contains invalid values")
            points.append(
                {
                    "date": observed,
                    "nav": nav,
                    "equity_return_pct": (
                        None if daily_return is None else float(daily_return)
                    ),
                    "unit_money": str(item["unit_money"]),
                }
            )
            previous = observed
        if not points:
            raise DataError("Cached NAV history is empty")
        last_modified = payload.get("last_modified")
        if last_modified is not None and not isinstance(last_modified, str):
            raise DataError("Cached NAV Last-Modified value is invalid")
        return points, last_modified

    @staticmethod
    def _encode_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "date": point["date"].isoformat(),
                "nav": point["nav"],
                "equity_return_pct": point["equity_return_pct"],
                "unit_money": point["unit_money"],
            }
            for point in points
        ]

    @staticmethod
    def _validate_page_snapshot(
        points: list[dict[str, Any]],
        fund: dict[str, Any],
        as_of: date,
        allow_page_lead: bool = False,
    ) -> str | None:
        raw_date = fund.get("latest_nav_date")
        raw_value = fund.get("latest_nav_value")
        if raw_date is None or raw_value is None:
            return None
        observed = parse_date(str(raw_date))
        if observed > as_of:
            return None
        match = next((point for point in reversed(points) if point["date"] == observed), None)
        if match is not None:
            if math.isclose(
                float(match["nav"]), float(raw_value), rel_tol=0, abs_tol=1e-8
            ):
                return None
            raise DataError(
                f"NAV history for {fund['code']} does not match fund-page observation "
                f"{observed}={float(raw_value):g}"
            )
        latest = max(
            (point for point in points if point["date"] <= as_of),
            key=lambda point: point["date"],
        )
        lag_days = (observed - latest["date"]).days
        if allow_page_lead and 0 < lag_days <= BENCHMARK_MAX_STALENESS_DAYS:
            return (
                f"{fund['code']} 的基金主页净值已更新至 {observed}，完整复权净值历史仍为 "
                f"{latest['date']}；已强制重新验证完整历史并按后者计算。"
            )
        raise DataError(
            f"NAV history for {fund['code']} does not contain fund-page observation "
            f"{observed}={float(raw_value):g}"
        )

    def _save(
        self,
        path: Path,
        code: str,
        last_modified: str | None,
        points: list[dict[str, Any]],
    ) -> None:
        write_json(
            path,
            {
                "schema_version": PERFORMANCE_CACHE_SCHEMA_VERSION,
                "code": code,
                "last_modified": last_modified,
                "points": self._encode_points(points),
            },
        )

    def get(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        as_of: date,
        benchmark: Nasdaq100Benchmark,
    ) -> tuple[dict[str, Any], list[str]]:
        code = fund["code"]
        path = self.directory / "nav-history" / f"{code}.json"
        points: list[dict[str, Any]] | None = None
        last_modified: str | None = None
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                points, last_modified = self._decode_points(payload, code)
            except (DataError, OSError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
                points = None
                last_modified = None

        url = PERFORMANCE_DATA_URL.format(code=code, cache_buster=as_of.strftime("%Y%m%d"))
        if last_modified:
            with self._stats_lock:
                self.conditional_requests += 1
        status, response_text, response_last_modified = client.get_conditional_text(
            url,
            referer=fund["fund_page_url"],
            last_modified=last_modified,
        )
        if status == 304:
            if points is None:
                raise DataError(f"NAV source returned 304 without a cache for fund {code}")
            try:
                self._validate_page_snapshot(points, fund, as_of)
            except DataError:
                status, response_text, response_last_modified = client.get_conditional_text(
                    url, referer=fund["fund_page_url"]
                )
                if status != 200 or response_text is None:
                    raise
            else:
                with self._stats_lock:
                    self.hits += 1
                    self.not_modified += 1
                return calculate_performance_from_points(
                    points, code, as_of, url, benchmark
                )

        if status != 200 or response_text is None:
            raise DataError(f"Unexpected NAV history response {status} for fund {code}")
        points = parse_performance_page(response_text, code)
        page_warning = self._validate_page_snapshot(
            points, fund, as_of, allow_page_lead=True
        )
        with self._stats_lock:
            self.misses += 1
            self.updates += 1
        self._save(path, code, response_last_modified, points)
        performance, warnings = calculate_performance_from_points(
            points, code, as_of, url, benchmark
        )
        if page_warning is not None:
            warnings.append(page_warning)
        return performance, warnings

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "corrupt_rebuilds": self.corrupt_rebuilds,
                "conditional_requests": self.conditional_requests,
                "not_modified": self.not_modified,
                "updates": self.updates,
            }


def filter_performance_full_scan(
    client: HttpClient,
    candidates: list[dict[str, Any]],
    as_of: date,
    min_three_year_return_pct: float,
    top: int,
    min_five_year_return_pct: float = DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
    min_ten_year_return_pct: float = DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
) -> tuple[list[dict[str, Any]], list[str], int]:
    selected: list[dict[str, Any]] = []
    warnings: list[str] = []
    scanned = 0
    for fund in candidates:
        scanned += 1
        performance, performance_warnings = fetch_trailing_performance(client, fund, as_of)
        warnings.extend(performance_warnings)
        if performance_threshold_failures(
            performance,
            min_three_year_return_pct,
            min_five_year_return_pct,
            min_ten_year_return_pct,
        ):
            continue
        selected.append({**fund, **performance})
    return selected[:top], warnings, scanned


def filter_performance_and_us_exposure_full_scan(
    client: HttpClient,
    candidates: list[dict[str, Any]],
    as_of: date,
    min_three_year_return_pct: float,
    min_us_equity_pct: float,
    top: int,
    report_cache: PeriodicReportCache,
    resolver: LookthroughResolver,
    performance_cache: PerformanceResultCache | None = None,
    exposure_cache: FundExposureResultCache | None = None,
    performance_workers: int = PERFORMANCE_WORKERS,
    benchmark: Nasdaq100Benchmark | None = None,
    min_five_year_return_pct: float = DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
    min_ten_year_return_pct: float = DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
) -> tuple[list[dict[str, Any]], list[str], int, int, int, int]:
    warnings: list[str] = []
    performance_results: list[tuple[dict[str, Any], list[str]] | None] = [
        None
    ] * len(candidates)

    def evaluate_performance(
        fund: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        if performance_cache is not None:
            if benchmark is None:
                raise DataError("Nasdaq-100 benchmark is required with the performance cache")
            return performance_cache.get(client, fund, as_of, benchmark)
        return fetch_trailing_performance(client, fund, as_of, benchmark)

    if candidates:
        with ThreadPoolExecutor(
            max_workers=min(max(1, performance_workers), len(candidates))
        ) as executor:
            futures = {
                executor.submit(evaluate_performance, fund): index
                for index, fund in enumerate(candidates)
            }
            for future in as_completed(futures):
                performance_results[futures[future]] = future.result()

    performance_qualified: list[dict[str, Any]] = []
    for fund, result in zip(candidates, performance_results):
        if result is None:
            raise DataError(f"Performance was not evaluated for fund {fund['code']}")
        performance, performance_warnings = result
        warnings.extend(performance_warnings)
        if performance_threshold_failures(
            performance,
            min_three_year_return_pct,
            min_five_year_return_pct,
            min_ten_year_return_pct,
        ):
            continue
        performance_qualified.append({**fund, **performance})

    exposure_qualified: list[dict[str, Any]] = []
    for fund in performance_qualified:
        exposure, exposure_warnings = fetch_us_equity_exposure(
            client,
            fund,
            as_of,
            report_cache,
            resolver,
            min_us_equity_pct,
            exposure_cache,
        )
        warnings.extend(f"{fund['code']} {warning}" for warning in exposure_warnings)
        if exposure["status"] != "qualified":
            continue
        if not isinstance(fund.get("nasdaq100_fit"), dict):
            detail = fund.get("nasdaq100_fit_error") or "unknown calculation error"
            raise DataError(
                f"Nasdaq-100 fit is unavailable for qualified fund {fund['code']}: {detail}"
            )
        exposure_qualified.append(
            {**fund, "us_equity_exposure": exposure}
        )
    exposure_qualified.sort(
        key=lambda item: (
            -item["nasdaq100_fit"]["correlation"],
            abs(item["nasdaq100_fit"]["beta"] - 1),
            -item["us_equity_exposure"]["confirmed_pct"],
            -item.get("institution_holding_ratio_pct", 0),
            -item["three_year_return_pct"],
            item["code"],
        )
    )
    return (
        exposure_qualified[:top],
        warnings,
        len(candidates),
        len(performance_qualified),
        len(performance_qualified),
        len(exposure_qualified),
    )


def performance_threshold_failures(
    performance: dict[str, Any],
    min_three_year_return_pct: float,
    min_five_year_return_pct: float,
    min_ten_year_return_pct: float,
) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    three_year = performance.get("three_year_return_pct")
    five_year = performance.get("five_year_return_pct")
    ten_year = performance.get("ten_year_return_pct")
    if three_year is None or float(three_year) < min_three_year_return_pct:
        failures.append(
            (
                "three_year_return_below_threshold",
                f"近三年收益低于 {min_three_year_return_pct:g}%",
            )
        )
    if five_year is not None and float(five_year) < min_five_year_return_pct:
        failures.append(
            (
                "five_year_return_below_threshold",
                f"有完整五年历史且近五年收益低于 {min_five_year_return_pct:g}%",
            )
        )
    if ten_year is not None and float(ten_year) < min_ten_year_return_pct:
        failures.append(
            (
                "ten_year_return_below_threshold",
                f"有完整十年历史且近十年收益低于 {min_ten_year_return_pct:g}%",
            )
        )
    return failures


def evaluate_performance_full_scan(
    client: HttpClient,
    candidates: list[dict[str, Any]],
    as_of: date,
    min_three_year_return_pct: float,
    performance_cache: PerformanceResultCache,
    benchmark: Nasdaq100Benchmark,
    min_five_year_return_pct: float = DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
    min_ten_year_return_pct: float = DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
    performance_workers: int = PERFORMANCE_WORKERS,
) -> tuple[
    list[dict[str, Any]],
    list[str],
    int,
    dict[str, list[tuple[str, str]]],
]:
    results: list[tuple[dict[str, Any], list[str]] | None] = [None] * len(candidates)

    def evaluate(fund: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        return performance_cache.get(client, fund, as_of, benchmark)

    if candidates:
        with ThreadPoolExecutor(
            max_workers=min(max(1, performance_workers), len(candidates))
        ) as executor:
            futures = {
                executor.submit(evaluate, fund): index
                for index, fund in enumerate(candidates)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()

    qualified: list[dict[str, Any]] = []
    rejected: dict[str, list[tuple[str, str]]] = {}
    warnings: list[str] = []
    for fund, result in zip(candidates, results):
        if result is None:
            raise DataError(f"Performance was not evaluated for fund {fund['code']}")
        performance, performance_warnings = result
        warnings.extend(performance_warnings)
        failures = performance_threshold_failures(
            performance,
            min_three_year_return_pct,
            min_five_year_return_pct,
            min_ten_year_return_pct,
        )
        if failures:
            rejected[fund["code"]] = failures
            continue
        qualified.append({**fund, **performance})
    return qualified, warnings, len(candidates), rejected


def direct_limit_qualifies(limit: dict[str, Any], threshold_cny: int) -> bool:
    if limit.get("status") == "unlimited":
        return True
    return (
        limit.get("status") == "limited"
        and isinstance(limit.get("amount_cny"), int)
        and int(limit["amount_cny"]) >= threshold_cny
    )


def calculate_return_drawdown_ratio(record: dict[str, Any]) -> tuple[float | None, float]:
    start = parse_date(str(record["three_year_performance_start_date"]))
    end = parse_date(str(record["three_year_performance_end_date"]))
    span_days = (end - start).days
    if span_days <= 0:
        raise DataError(f"Invalid three-year performance span for {record['code']}")
    total_return = float(record["three_year_return_pct"]) / 100
    if total_return <= -1:
        raise DataError(f"Invalid three-year return for {record['code']}")
    annualized = ((1 + total_return) ** (365 / span_days) - 1) * 100
    drawdown = abs(float(record["three_year_max_drawdown_pct"]))
    score = None if drawdown == 0 else annualized / drawdown
    return score, annualized


def us_main_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -float(item["nasdaq100_fit"]["correlation"]),
        abs(float(item["nasdaq100_fit"]["beta"]) - 1),
        -float(item["us_equity_exposure"]["confirmed_pct"]),
        -float(item.get("institution_holding_ratio_pct", 0)),
        -float(item["three_year_return_pct"]),
        item["code"],
    )


def global_supplement_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    score = item.get("_return_drawdown_ratio")
    score_key = float("-inf") if score is None else -round(float(score), 4)
    return (
        score_key,
        -float(item["three_year_return_pct"]),
        -float(item["three_year_max_drawdown_pct"]),
        -float(item.get("institution_holding_ratio_pct", 0)),
        -float(item["scale_billion_cny"]),
        item["code"],
    )


def ranking_list_for_exposure(
    exposure: dict[str, Any], threshold_pct: float
) -> str:
    return (
        "us_main"
        if float(exposure["confirmed_pct"]) >= threshold_pct
        else "global_supplement"
    )


def ranking_route(
    name: str,
    exposure: dict[str, Any],
    threshold_pct: float,
    us_main_exclude_keywords: Iterable[str],
) -> tuple[str, str]:
    if any(keyword and keyword in name for keyword in us_main_exclude_keywords):
        return "global_supplement", ROUTING_REASON_GEOGRAPHY_OVERRIDE
    if float(exposure["confirmed_pct"]) >= threshold_pct:
        return "us_main", ROUTING_REASON_CONFIRMED_US
    return "global_supplement", ROUTING_REASON_BELOW_US_THRESHOLD


def routing_reason_label(reason: str) -> str:
    try:
        return ROUTING_REASON_LABELS[reason]
    except KeyError as exc:
        raise DataError(f"Unknown ranking routing reason: {reason}") from exc


def normalize_notice_text(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Some announcement PDFs extract every Chinese character and numeric
    # punctuation as separate text runs. Rejoin those runs before matching.
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"(?<=\d)\s*([,.])\s*(?=\d)", r"\1", text)


def parse_cny_amount(value: str, unit: str) -> int:
    number = float(value.replace(",", ""))
    if unit == "万元":
        number *= 10000
    return int(round(number))


def find_amounts(text: str) -> list[tuple[int, int, str]]:
    results: list[tuple[int, int, str]] = []
    for match in re.finditer(r"(?<![\d-])([\d][\d,.]*)\s*(万元|元)", text):
        try:
            results.append((match.start(), parse_cny_amount(match.group(1), match.group(2)), match.group(0)))
        except ValueError:
            continue
    return results


def extract_channel_amount(text: str, channel_pattern: str) -> int | None:
    best: tuple[int, int] | None = None
    for channel_match in re.finditer(channel_pattern, text):
        clause = re.split(r"[。；]", text[channel_match.end() :], maxsplit=1)[0]
        explicit = re.search(
            r"(?:不得超过|不超过|高于|超过|限额(?:调整)?为)\s*"
            r"([\d][\d,.]*)\s*(万元|元)",
            clause,
        )
        if explicit:
            return parse_cny_amount(explicit.group(1), explicit.group(2))
        above = re.search(r"([\d][\d,.]*)\s*(万元|元)\s*以上", clause)
        if above:
            return parse_cny_amount(above.group(1), above.group(2))
        start = max(0, channel_match.start() - 80)
        end = min(len(text), channel_match.end() + 220)
        window = text[start:end]
        for amount_pos, amount, _ in find_amounts(window):
            absolute_pos = start + amount_pos
            distance = abs(absolute_pos - channel_match.end())
            context_start = max(0, amount_pos - 55)
            context_end = min(len(window), amount_pos + 55)
            context = window[context_start:context_end]
            if not re.search(r"上限|超过|不得超过|不超过|限制|暂停办理|累计申购", context):
                continue
            candidate = (distance, amount)
            if best is None or candidate[0] < best[0]:
                best = candidate
    return best[1] if best else None


def extract_global_amount(text: str) -> int | None:
    patterns = [
        r"限制\s*(?:大额\s*)?申购\s*(?:及\s*定期定额投资\s*)?金额\s*"
        r"(?:[（(]\s*单\s*位\s*[：:]\s*(?:人民币\s*)?元\s*[）)])?\s*([\d,.]+)",
        r"调整\s*申购\s*(?:[（(]\s*含\s*定期定额投资\s*[）)])?\s*金额\s*"
        r"(?:[（(]\s*单\s*位\s*[：:]\s*人民币\s*元\s*[）)])?\s*([\d,.]+)",
        r"(?:累计申购|申购金额)[^。；]{0,180}?(?:不超过|不得超过|不应超过|上限调整为)\s*"
        r"(?:人民币\s*)?([\d,.]+)\s*(?:人民币\s*)?(万元|元)",
        r"超过\s*([\d,.]+)\s*(万元|元)[^。；]{0,50}?(?:申购|大额申购)",
        r"(?:金额累计限额|业务限额)为\s*([\d,.]+)\s*(?:人民币\s*)?(万元|元)",
    ]
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue
        if index < 2:
            return int(round(float(match.group(1).replace(",", ""))))
        return parse_cny_amount(match.group(1), match.group(2))
    return None


def extract_effective_date(text: str, published: date) -> date:
    patterns = [
        r"(?:调整|暂停|恢复)[^。；]{0,30}?起始日\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:起|（含)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return published


def detect_share_aggregation(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if re.search(
        r"A[类级].{0,60}C[类级].{0,35}(?:合并计算|合计金额)|A类、C类.{0,30}合计金额",
        compact,
    ):
        return "A/C combined"
    if re.search(
        r"分别计算|分开计算|单独计算(?:限额)?|单一基金份额|单一类别|A类人民币份额或C类人民币份额",
        compact,
    ):
        return "A/C separate"
    return None


def extract_future_transitions(
    text: str, source_url: str, published: date
) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    date_pattern = re.compile(
        r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:起|（含[^）]*）)"
    )
    matches = list(date_pattern.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 500)
        clause = text[match.start():end]
        clause = re.split(r"[。；]", clause, maxsplit=1)[0]
        if not re.search(r"恢复|调整|暂停", clause):
            continue
        amount = extract_global_amount(clause)
        if amount is None:
            continue
        transitions.append(
            {
                "effective_date": date(int(match.group(1)), int(match.group(2)), int(match.group(3))),
                "direct_amount_cny": extract_channel_amount(
                    clause, DIRECT_CHANNEL_PATTERN
                ),
                "agency_amount_cny": extract_channel_amount(clause, AGENCY_CHANNEL_PATTERN),
                "global_amount_cny": amount,
                "source_url": source_url,
                "published_date": published.isoformat(),
                "share_aggregation": detect_share_aggregation(clause),
                "all_channels_combined": "全部销售机构累计" in re.sub(r"\s+", "", clause),
                "confidence": "high",
            }
        )
    return transitions


def parse_quota_notice(
    text: str, published: date, source_url: str
) -> list[dict[str, Any]]:
    normalized = normalize_notice_text(text)
    direct = extract_channel_amount(
        normalized, DIRECT_CHANNEL_PATTERN
    )
    agency = extract_channel_amount(normalized, AGENCY_CHANNEL_PATTERN)
    global_amount = extract_global_amount(normalized)
    compact = re.sub(r"\s+", "", normalized)
    if direct is None and agency is None and global_amount is None:
        future_restore = re.search(
            r"恢复(?:办理)?大额申购.{0,40}(?:具体时间|时间).{0,20}另行公告",
            compact,
        )
        if (
            re.search(r"恢复(?:办理)?大额申购", compact)
            and not re.search(r"暂停接受.*?超过", compact)
            and not future_restore
        ):
            global_status = "unlimited"
        else:
            return []
    else:
        global_status = "limited"
    base = {
        "effective_date": extract_effective_date(normalized, published),
        "direct_amount_cny": direct,
        "agency_amount_cny": agency,
        "global_amount_cny": global_amount,
        "global_status": global_status,
        "source_url": source_url,
        "published_date": published.isoformat(),
        "share_aggregation": detect_share_aggregation(normalized),
        "all_channels_combined": bool(
            "全部销售机构累计" in compact
            or re.search(r"多家销售渠道.{0,60}累计计算", compact)
        ),
        "confidence": "high" if global_amount is not None or (direct is not None and agency is not None) else "medium",
    }
    transitions = [base]
    for transition in extract_future_transitions(normalized, source_url, published):
        if transition["effective_date"] != base["effective_date"]:
            transitions.append(transition)
    return transitions


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf exposes several parser-specific exceptions
        raise DataError(f"Could not extract announcement PDF text: {exc}") from exc


def parse_periodic_report_date(title: str) -> date | None:
    if REPORT_TITLE_EXCLUDE_RE.search(title):
        return None
    year_match = re.search(r"([0-9〇零○ＯO一二三四五六七八九]{4})\s*年", title)
    if not year_match:
        return None
    translation = str.maketrans("〇零○ＯO一二三四五六七八九", "00000123456789")
    try:
        year = int(year_match.group(1).translate(translation))
    except ValueError:
        return None
    quarter_match = re.search(r"第\s*([1234一二三四])\s*季度报告", title)
    if quarter_match:
        quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(
            quarter_match.group(1), int(quarter_match.group(1)) if quarter_match.group(1).isdigit() else 0
        )
        month, day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
        return date(year, month, day)
    if re.search(r"(?:中期|半年度)报告", title):
        return date(year, 6, 30)
    if re.search(r"年度报告", title):
        return date(year, 12, 31)
    return None


def fetch_latest_periodic_report(
    client: HttpClient,
    code: str,
    as_of: date,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> PeriodicReport:
    if snapshot is not None:
        if snapshot.code != code or snapshot.as_of != as_of:
            raise DataError("Announcement snapshot identity does not match report request")
        latest_ids = set(snapshot.latest_page_ids)
        records = tuple(
            item
            for item in snapshot.items
            if not latest_ids or item.announcement_id in latest_ids
        )
    else:
        records = tuple(
            _parse_announcement_page(_announcement_page(client, code, 1), code)
        )
    reports: list[PeriodicReport] = []
    for item in records:
        report_date = parse_periodic_report_date(item.title)
        if report_date is None:
            continue
        published = item.published_date
        if published > as_of or report_date > as_of:
            continue
        announcement_id = item.announcement_id
        reports.append(
            PeriodicReport(
                announcement_id=announcement_id,
                title=item.title,
                report_date=report_date,
                published_date=published,
                source_url=item.source_url,
            )
        )
    if not reports:
        raise DataError(f"No readable periodic report was disclosed by {as_of} for fund {code}")
    return max(reports, key=lambda item: (item.report_date, item.published_date))


class PeriodicReportCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.downloads = 0
        self.corrupt_redownloads = 0
        self.text_extractions = 0
        self._stats_lock = Lock()
        self._key_locks: dict[str, Lock] = {}
        self._key_locks_lock = Lock()

    @staticmethod
    def _validate(pdf_bytes: bytes) -> str:
        if len(pdf_bytes) < 1000 or not pdf_bytes.lstrip().startswith(b"%PDF-"):
            raise DataError("Downloaded periodic report is not a valid PDF")
        text = extract_pdf_text(pdf_bytes)
        if not text.strip():
            raise DataError("Downloaded periodic report has no extractable text")
        return text

    def get_text(
        self,
        client: HttpClient,
        report: PeriodicReport | LegalDocument,
        referer: str,
    ) -> str:
        with self._key_locks_lock:
            key_lock = self._key_locks.setdefault(report.announcement_id, Lock())
        with key_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{report.announcement_id}.pdf"
            if path.exists():
                try:
                    text = self._validate(path.read_bytes())
                    with self._stats_lock:
                        self.hits += 1
                        self.text_extractions += 1
                    return text
                except (DataError, OSError):
                    with self._stats_lock:
                        self.corrupt_redownloads += 1
            pdf_bytes = client.get_bytes(report.source_url, referer=referer)
            text = self._validate(pdf_bytes)
            temporary = path.with_name(
                f"{path.name}.{os.getpid()}.{get_ident()}.tmp"
            )
            temporary.write_bytes(pdf_bytes)
            temporary.replace(path)
            with self._stats_lock:
                self.downloads += 1
                self.text_extractions += 1
            return text

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "downloads": self.downloads,
                "corrupt_redownloads": self.corrupt_redownloads,
                "text_extractions": self.text_extractions,
            }


def normalize_benchmark_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]+", "", value.upper())


class ContractBenchmarkCatalog:
    def __init__(self, path: Path) -> None:
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"Could not load contract benchmark catalog: {exc}") from exc
        if payload.get("schema_version") != 1 or not isinstance(payload.get("entries"), list):
            raise DataError("Unsupported contract benchmark catalog schema")
        self.path = path
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        self.entries: list[dict[str, Any]] = []
        for entry in payload["entries"]:
            aliases = entry.get("aliases") or []
            required = {
                "id",
                "display_name",
                "market_scope",
                "market_label",
                "asset_class",
                "style_label",
                "structure",
                "excluded_target",
            }
            if not required.issubset(entry) or not aliases:
                raise DataError(f"Invalid contract benchmark catalog entry: {entry!r}")
            normalized_aliases = sorted(
                {normalize_benchmark_name(str(alias)) for alias in aliases if str(alias).strip()},
                key=len,
                reverse=True,
            )
            if not normalized_aliases:
                raise DataError(f"Contract benchmark {entry['id']} has no usable aliases")
            self.entries.append({**entry, "normalized_aliases": normalized_aliases})

    def match(self, value: str) -> list[dict[str, Any]]:
        normalized = normalize_benchmark_name(value)
        matches: dict[str, dict[str, Any]] = {}
        for entry in self.entries:
            if any(alias in normalized for alias in entry["normalized_aliases"]):
                matches[str(entry["id"])] = entry
        return list(matches.values())


def _announcement_page(
    client: HttpClient, code: str, page_index: int, page_size: int = 100
) -> dict[str, Any]:
    params = {
        "fundcode": code,
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "type": "0",
    }
    url = f"{ANNOUNCEMENT_API_URL}?{urllib.parse.urlencode(params)}"
    return client.get_json(url, referer=f"https://fundf10.eastmoney.com/jjgg_{code}.html")


def _parse_announcement_page(
    payload: dict[str, Any], code: str
) -> list[AnnouncementRecord]:
    items = payload.get("Data") or []
    if not isinstance(items, list):
        raise DataError(f"Announcement index is invalid for fund {code}")
    records: list[AnnouncementRecord] = []
    for item in items:
        if not isinstance(item, dict):
            raise DataError(f"Announcement index row is invalid for fund {code}")
        announcement_id = str(item.get("ID") or "").strip()
        title = str(item.get("TITLE") or "").strip()
        published_raw = str(item.get("PUBLISHDATEDesc") or "").strip()
        if not announcement_id or not title or not published_raw:
            continue
        records.append(
            AnnouncementRecord(
                announcement_id,
                title,
                parse_date(published_raw),
            )
        )
    return records


def _announcement_has_legal_pair(items: Iterable[AnnouncementRecord]) -> bool:
    has_prospectus = any(
        "招募说明书" in item.title
        and "提示性公告" not in item.title
        and "摘要" not in item.title
        for item in items
    )
    has_summary = any(
        "基金产品资料概要" in item.title and _is_rmb_product_summary(item.title)
        for item in items
    )
    return has_prospectus and has_summary


class AnnouncementIndexCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.checks = 0
        self.pages_fetched = 0
        self.full_seeds = 0
        self.cache_loads = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()
        self._key_locks: dict[str, Lock] = {}
        self._key_locks_lock = Lock()

    @staticmethod
    def _decode(
        payload: dict[str, Any], code: str
    ) -> tuple[dict[str, AnnouncementRecord], bool]:
        if (
            payload.get("schema_version") != ANNOUNCEMENT_INDEX_CACHE_SCHEMA_VERSION
            or payload.get("code") != code
            or not isinstance(payload.get("items"), list)
            or not isinstance(payload.get("history_seeded"), bool)
        ):
            raise DataError("Cached announcement index identity is invalid")
        items: dict[str, AnnouncementRecord] = {}
        for raw in payload["items"]:
            if not isinstance(raw, dict) or set(raw) != {
                "id",
                "title",
                "published_date",
            }:
                raise DataError("Cached announcement index row is invalid")
            record = AnnouncementRecord(
                str(raw["id"]),
                str(raw["title"]),
                parse_date(str(raw["published_date"])),
            )
            if not record.announcement_id or record.announcement_id in items:
                raise DataError("Cached announcement index contains duplicate IDs")
            items[record.announcement_id] = record
        return items, bool(payload["history_seeded"])

    @staticmethod
    def _encode(
        code: str,
        items: dict[str, AnnouncementRecord],
        history_seeded: bool,
    ) -> dict[str, Any]:
        ordered = sorted(
            items.values(),
            key=lambda item: (item.published_date, item.announcement_id),
            reverse=True,
        )
        return {
            "schema_version": ANNOUNCEMENT_INDEX_CACHE_SCHEMA_VERSION,
            "code": code,
            "history_seeded": history_seeded,
            "items": [
                {
                    "id": item.announcement_id,
                    "title": item.title,
                    "published_date": item.published_date.isoformat(),
                }
                for item in ordered
            ],
        }

    def _get_locked(
        self, client: HttpClient, code: str, as_of: date
    ) -> FundAnnouncementSnapshot:
        path = self.directory / f"{code}.json"
        cached: dict[str, AnnouncementRecord] = {}
        history_seeded = False
        if path.exists():
            try:
                cached, history_seeded = self._decode(
                    json.loads(path.read_text(encoding="utf-8")), code
                )
                with self._stats_lock:
                    self.cache_loads += 1
            except (DataError, OSError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
                cached = {}
                history_seeded = False

        first = _announcement_page(client, code, 1)
        with self._stats_lock:
            self.checks += 1
            self.pages_fetched += 1
        first_records = _parse_announcement_page(first, code)
        latest_page_ids = tuple(record.announcement_id for record in first_records)
        for record in first_records:
            cached[record.announcement_id] = record
        total_count = int(first.get("TotalCount") or len(cached))
        page_size = int(first.get("PageSize") or 100)
        total_pages = max(1, math.ceil(total_count / max(1, page_size)))

        if not history_seeded:
            with self._stats_lock:
                self.full_seeds += 1
            page = 2
            while page <= total_pages and not _announcement_has_legal_pair(
                item for item in cached.values() if item.published_date <= as_of
            ):
                payload = _announcement_page(client, code, page)
                with self._stats_lock:
                    self.pages_fetched += 1
                page_items = _parse_announcement_page(payload, code)
                for record in page_items:
                    cached[record.announcement_id] = record
                if not page_items:
                    break
                page += 1
            history_seeded = True

        write_json(path, self._encode(code, cached, history_seeded))
        visible = tuple(
            sorted(
                (
                    item
                    for item in cached.values()
                    if item.published_date <= as_of
                ),
                key=lambda item: (item.published_date, item.announcement_id),
                reverse=True,
            )
        )
        if not visible:
            raise DataError(f"No announcements were disclosed by {as_of} for fund {code}")
        return FundAnnouncementSnapshot(code, as_of, visible, latest_page_ids)

    def get(
        self, client: HttpClient, code: str, as_of: date
    ) -> FundAnnouncementSnapshot:
        with self._key_locks_lock:
            key_lock = self._key_locks.setdefault(code, Lock())
        with key_lock:
            return self._get_locked(client, code, as_of)

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "checks": self.checks,
                "pages_fetched": self.pages_fetched,
                "full_seeds": self.full_seeds,
                "cache_loads": self.cache_loads,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }


def _is_rmb_product_summary(title: str) -> bool:
    if "提示性公告" in title or "美元" in title or "港币" in title:
        return False
    if re.search(r"人民币[CD]|[CD](?:类)?(?:份额)?人民币|\([CD]类份额\)|（[CD]类份额）", title):
        return False
    return True


def fetch_latest_legal_documents(
    client: HttpClient,
    code: str,
    as_of: date,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> tuple[LegalDocument | None, LegalDocument | None]:
    prospectuses: list[LegalDocument] = []
    summaries: list[LegalDocument] = []
    if snapshot is not None:
        if snapshot.code != code or snapshot.as_of != as_of:
            raise DataError("Announcement snapshot identity does not match legal-document request")
        page_items: list[list[AnnouncementRecord]] = [list(snapshot.items)]
    else:
        page_items = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        if snapshot is not None:
            records = page_items[0]
            total_pages = 1
        else:
            payload = _announcement_page(client, code, page)
            if page == 1:
                total_count = int(payload.get("TotalCount") or 0)
                page_size = int(payload.get("PageSize") or 100)
                total_pages = max(1, math.ceil(total_count / max(page_size, 1)))
            records = _parse_announcement_page(payload, code)
        for record in records:
            title = record.title
            published = record.published_date
            if published > as_of:
                continue
            announcement_id = record.announcement_id
            if not announcement_id:
                continue
            source_url = record.source_url
            if (
                "招募说明书" in title
                and "提示性公告" not in title
                and "摘要" not in title
            ):
                prospectuses.append(
                    LegalDocument(
                        announcement_id,
                        title,
                        published,
                        source_url,
                        "prospectus",
                    )
                )
            if "基金产品资料概要" in title and _is_rmb_product_summary(title):
                summaries.append(
                    LegalDocument(
                        announcement_id,
                        title,
                        published,
                        source_url,
                        "product_summary",
                    )
                )
        if prospectuses and summaries:
            break
        if not records:
            break
        page += 1
    prospectus = (
        max(prospectuses, key=lambda item: (item.published_date, item.announcement_id))
        if prospectuses
        else None
    )
    summary = (
        max(summaries, key=lambda item: (item.published_date, item.announcement_id))
        if summaries
        else None
    )
    return prospectus, summary


def extract_contract_benchmark_statement(text: str) -> str:
    compact = re.sub(
        r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])",
        "",
        text.replace("\u3000", " "),
    )
    compact = re.sub(r"\s+", " ", compact).strip()
    starts = list(
        re.finditer(
            r"(?:本基金(?:选择)?的?)?业绩比较基准(?:为|是|采用|：|:)\s*",
            compact,
        )
    )
    starts.extend(
        re.finditer(
            r"业绩比较基准\s*(?=(?:\d|经|标|纳|MSCI|摩根|彭博|伦敦|富时|恒生|中证|人民币|美元|[A-Z]))",
            compact,
            re.I,
        )
    )
    stop_re = re.compile(
        r"风险收益特征|业绩比较基准的选择理由|如果今后|若今后|在法律法规|"
        r"基金管理人可|本基金为|本基金选择|本基金设置|本基金采取"
    )
    candidates: list[str] = []
    for match in starts:
        tail = compact[match.end() : match.end() + 1200]
        stop = stop_re.search(tail)
        statement = tail[: stop.start() if stop else 800].strip(" ：:。；;")
        sentence_end = re.search(r"[。；;]", statement)
        if sentence_end:
            statement = statement[: sentence_end.start()].strip()
        normalized = normalize_benchmark_name(statement)
        if statement and any(
            token in normalized
            for token in ("指数", "价格", "利率", "INDEX", "PRICE", "VIX")
        ):
            candidates.append(statement)
    if not candidates:
        raise DataError("Could not locate the current performance benchmark statement")
    return min(candidates, key=lambda value: (0 if "%" in value else 1, len(value)))


def _benchmark_weight(statement: str, entry: dict[str, Any]) -> float:
    compact = re.sub(r"\s+", " ", statement)
    for alias in sorted(entry.get("aliases") or [], key=lambda value: len(str(value)), reverse=True):
        parts = [re.escape(part) for part in re.split(r"\s+", str(alias).strip()) if part]
        if not parts:
            continue
        alias_pattern = r"\s*".join(parts)
        for match in re.finditer(alias_pattern, compact, re.I):
            before = compact[max(0, match.start() - 45) : match.start()]
            after = compact[match.end() : match.end() + 90]
            before_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*[×*xX]\s*$", before)
            if before_match:
                return float(before_match.group(1))
            after_match = re.search(
                r"^[^+＋，,。；;]{0,55}?[×*xX]\s*(\d+(?:\.\d+)?)\s*%",
                after,
            )
            if after_match:
                return float(after_match.group(1))
    percentages = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s*%", compact)]
    if percentages:
        return max(percentages)
    return 100.0


def detect_product_structure(text: str, catalog_value: str) -> str:
    normalized = re.sub(r"\s+", "", text)
    if re.search(r"反向|做空|Inverse|Short", normalized, re.I):
        return "inverse"
    if re.search(
        r"(?:[2-9]|两|三)倍(?:做多|多头|杠杆)|杠杆指数|Leveraged|Ultra",
        normalized,
        re.I,
    ):
        return "leveraged"
    if re.search(r"波动率|VIX|Volatility", normalized, re.I):
        return "volatility"
    return catalog_value


def parse_contract_benchmark(
    text: str,
    fund: dict[str, Any],
    catalog: ContractBenchmarkCatalog,
) -> dict[str, Any]:
    statement = extract_contract_benchmark_statement(text)
    matches = catalog.match(statement)
    if not matches and "标的指数" in statement:
        matches = catalog.match(f"{fund['name']} {text[:3000]}")
    components = [
        {
            "benchmark_id": entry["id"],
            "benchmark_name": entry["display_name"],
            "weight_pct": round(_benchmark_weight(statement, entry), 2),
            "market_scope": entry["market_scope"],
            "market_label": entry["market_label"],
            "asset_class": entry["asset_class"],
            "style_label": entry["style_label"],
            "structure": entry["structure"],
            "excluded_target": bool(entry["excluded_target"]),
        }
        for entry in matches
    ]
    components.sort(key=lambda item: (-float(item["weight_pct"]), item["benchmark_id"]))
    single = components[0] if len(components) == 1 else None
    status = "recognized" if single else "composite" if components else "unrecognized"
    return {
        "status": status,
        "benchmark_text": statement,
        "benchmark_id": single["benchmark_id"] if single else None,
        "benchmark_name": (
            single["benchmark_name"]
            if single
            else " + ".join(item["benchmark_name"] for item in components)
            if components
            else "未识别"
        ),
        "benchmark_weight_pct": single["weight_pct"] if single else None,
        "market_scope": single["market_scope"] if single else "composite" if components else "unknown",
        "market_label": single["market_label"] if single else "复合市场" if components else "未识别",
        "asset_class": single["asset_class"] if single else "mixed" if components else "unknown",
        "style_label": single["style_label"] if single else "复合风格" if components else "未识别",
        "structure": detect_product_structure(
            f"{fund['name']} {statement}",
            str(single["structure"]) if single else "standard",
        ),
        "excluded_target": bool(components) and all(
            bool(item["excluded_target"]) for item in components
        ),
        "components": components,
    }


def unavailable_holding_cost(
    summary: LegalDocument | None, status: str = "unavailable"
) -> dict[str, Any]:
    return {
        "status": status,
        "annualized_pct": None,
        "measurement_date": None,
        "source_title": summary.title if summary else None,
        "source_published_date": summary.published_date.isoformat() if summary else None,
        "source_url": summary.source_url if summary else None,
    }


def unreadable_contract_benchmark(fund: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "unreadable",
        "benchmark_text": "未识别",
        "benchmark_id": None,
        "benchmark_name": "未识别",
        "benchmark_weight_pct": None,
        "market_scope": "unknown",
        "market_label": "未识别",
        "asset_class": "unknown",
        "style_label": "未识别",
        "structure": detect_product_structure(fund["name"], "standard"),
        "excluded_target": False,
        "components": [],
    }


def parse_holding_cost(
    text: str, summary: LegalDocument, as_of: date
) -> dict[str, Any]:
    if summary.published_date > as_of:
        raise DataError("Holding-cost source publication date is in the future")
    compact = re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()
    rate_match = re.search(
        r"基金运作综合费率\s*[（(]\s*年化\s*[）)]"
        r"(?:\s*(?:基金运作综合费率|\d+\s*/\s*\d+|[-–—])){0,3}"
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        compact,
    )
    if not rate_match:
        raise DataError("Could not locate annualized comprehensive operating expense")
    rate = float(rate_match.group(1))
    if not math.isfinite(rate) or rate < 0 or rate > 100:
        raise DataError("Annualized comprehensive operating expense is outside its valid range")
    date_match = re.search(
        r"(?:综合费率[^。]{0,80})?测算日期为\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        compact,
    )
    measurement_date = None
    if date_match:
        measured = date(*(int(value) for value in date_match.groups()))
        if measured > as_of:
            raise DataError("Holding-cost measurement date is in the future")
        measurement_date = measured.isoformat()
    return {
        "status": "parsed",
        "annualized_pct": round(rate, 2),
        "measurement_date": measurement_date,
        "source_title": summary.title,
        "source_published_date": summary.published_date.isoformat(),
        "source_url": summary.source_url,
    }


def resolve_contract_benchmark(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    document_cache: PeriodicReportCache,
    catalog: ContractBenchmarkCatalog,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    code = fund["code"]
    warnings: list[str] = []
    management_style = (
        "passive"
        if fund["fund_type"] == "指数型-海外股票" and "增强" not in fund["name"]
        else "active"
    )
    try:
        prospectus, summary = fetch_latest_legal_documents(
            client, code, as_of, snapshot=snapshot
        )
    except DataError as exc:
        profile = unreadable_contract_benchmark(fund)
        profile.update(
            {
                "management_style": management_style,
                "prospectus_title": None,
                "prospectus_published_date": None,
                "source_url": None,
                "product_summary_status": "unreadable",
                "product_summary_published_date": None,
                "product_summary_source_url": None,
                "catalog_fingerprint": catalog.fingerprint,
            }
        )
        warnings.extend(
            (
                f"合同基准告警 {code}：法律文件目录无法读取：{exc}",
                f"持有费率告警 {code}：法律文件目录无法读取：{exc}",
            )
        )
        return profile, unavailable_holding_cost(None), warnings

    if prospectus is not None:
        try:
            prospectus_text = document_cache.get_text(
                client, prospectus, fund["fund_page_url"]
            )
            profile = parse_contract_benchmark(prospectus_text, fund, catalog)
        except DataError as exc:
            profile = unreadable_contract_benchmark(fund)
            warnings.append(f"合同基准告警 {code}：{exc}")
    else:
        profile = unreadable_contract_benchmark(fund)
        warnings.append(f"合同基准告警 {code}：截至 {as_of} 没有可用的招募说明书。")

    summary_status = "missing"
    summary_url = summary.source_url if summary else None
    summary_published_date = summary.published_date.isoformat() if summary else None
    holding_cost = unavailable_holding_cost(summary)
    if summary is not None:
        try:
            summary_text = document_cache.get_text(
                client, summary, fund["fund_page_url"]
            )
        except DataError as exc:
            summary_status = "unreadable"
            warnings.append(f"产品概要告警 {code}：{exc}")
            warnings.append(f"持有费率告警 {code}：{exc}")
        else:
            try:
                holding_cost = parse_holding_cost(summary_text, summary, as_of)
            except DataError as exc:
                warnings.append(f"持有费率告警 {code}：{exc}")
            try:
                summary_profile = parse_contract_benchmark(summary_text, fund, catalog)
                prospectus_ids = {
                    item["benchmark_id"] for item in profile["components"]
                }
                summary_ids = {
                    item["benchmark_id"] for item in summary_profile["components"]
                }
                if profile["status"] in {"unreadable", "unrecognized"}:
                    summary_status = "unreadable"
                elif prospectus_ids != summary_ids:
                    summary_status = "conflict"
                    warnings.append(
                        f"产品概要告警 {code}：招募说明书与人民币产品概要的合同基准不一致。"
                    )
                else:
                    summary_status = "matched"
            except DataError as exc:
                summary_status = "unreadable"
                warnings.append(f"产品概要告警 {code}：{exc}")
    else:
        warnings.append(f"持有费率告警 {code}：截至 {as_of} 没有可用的人民币产品概要。")

    profile.update(
        {
            "management_style": management_style,
            "prospectus_title": prospectus.title if prospectus else None,
            "prospectus_published_date": (
                prospectus.published_date.isoformat() if prospectus else None
            ),
            "source_url": prospectus.source_url if prospectus else None,
            "product_summary_status": summary_status,
            "product_summary_published_date": summary_published_date,
            "product_summary_source_url": summary_url,
            "catalog_fingerprint": catalog.fingerprint,
        }
    )
    return profile, holding_cost, warnings


class ContractProfileResultCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()

    @staticmethod
    def _fund_identity(fund: dict[str, Any]) -> str:
        return hashlib.sha256(
            f"{fund['code']}\0{fund['name']}\0{fund['fund_type']}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _document_ids(
        client: HttpClient,
        code: str,
        as_of: date,
        snapshot: FundAnnouncementSnapshot,
    ) -> tuple[str | None, str | None]:
        prospectus, summary = fetch_latest_legal_documents(
            client, code, as_of, snapshot=snapshot
        )
        return (
            prospectus.announcement_id if prospectus else None,
            summary.announcement_id if summary else None,
        )

    @staticmethod
    def _validate(
        payload: dict[str, Any],
        code: str,
        fund_identity: str,
        prospectus_id: str | None,
        summary_id: str | None,
        catalog_fingerprint: str,
        as_of: date,
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        if (
            payload.get("schema_version") != CONTRACT_RESULT_CACHE_SCHEMA_VERSION
            or payload.get("method_version") != CONTRACT_RESULT_METHOD_VERSION
            or payload.get("code") != code
            or payload.get("fund_identity") != fund_identity
            or payload.get("prospectus_id") != prospectus_id
            or payload.get("summary_id") != summary_id
            or payload.get("catalog_fingerprint") != catalog_fingerprint
        ):
            raise DataError("Cached contract profile identity is invalid")
        profile = payload.get("profile")
        holding_cost = payload.get("holding_cost")
        warnings = payload.get("warnings")
        if (
            not isinstance(profile, dict)
            or not isinstance(holding_cost, dict)
            or not isinstance(warnings, list)
            or not all(isinstance(item, str) for item in warnings)
        ):
            raise DataError("Cached contract profile result is incomplete")
        for raw_date in (
            profile.get("prospectus_published_date"),
            profile.get("product_summary_published_date"),
            holding_cost.get("source_published_date"),
            holding_cost.get("measurement_date"),
        ):
            if raw_date is not None and parse_date(str(raw_date)) > as_of:
                raise DataError("Cached contract profile contains future data")
        return dict(profile), dict(holding_cost), list(warnings)

    def get(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        as_of: date,
        document_cache: PeriodicReportCache,
        catalog: ContractBenchmarkCatalog,
        snapshot: FundAnnouncementSnapshot,
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        code = fund["code"]
        prospectus_id, summary_id = self._document_ids(
            client, code, as_of, snapshot
        )
        fund_identity = self._fund_identity(fund)
        path = self.directory / f"{code}.json"
        if path.exists():
            try:
                result = self._validate(
                    json.loads(path.read_text(encoding="utf-8")),
                    code,
                    fund_identity,
                    prospectus_id,
                    summary_id,
                    catalog.fingerprint,
                    as_of,
                )
                with self._stats_lock:
                    self.hits += 1
                return result
            except (DataError, OSError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
        result = resolve_contract_benchmark(
            client,
            fund,
            as_of,
            document_cache,
            catalog,
            snapshot=snapshot,
        )
        profile, holding_cost, warnings = result
        write_json(
            path,
            {
                "schema_version": CONTRACT_RESULT_CACHE_SCHEMA_VERSION,
                "method_version": CONTRACT_RESULT_METHOD_VERSION,
                "code": code,
                "fund_identity": fund_identity,
                "prospectus_id": prospectus_id,
                "summary_id": summary_id,
                "catalog_fingerprint": catalog.fingerprint,
                "profile": profile,
                "holding_cost": holding_cost,
                "warnings": warnings,
            },
        )
        with self._stats_lock:
            self.misses += 1
        return result

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }


def normalize_instrument_name(value: str) -> str:
    return re.sub(
        r"[^\u4e00-\u9fffA-Z0-9]+", "", value.upper().replace("V AN", "VAN")
    )


class LookthroughResolver:
    def __init__(self, catalog_path: Path, cache_path: Path) -> None:
        try:
            catalog_bytes = catalog_path.read_bytes()
            catalog = json.loads(catalog_bytes.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"Could not load US-equity instrument catalog: {exc}") from exc
        self.entries = catalog.get("entries") or []
        self.catalog_fingerprint = hashlib.sha256(catalog_bytes).hexdigest()
        self.cache_path = cache_path
        self.cache: dict[str, dict[str, Any]] = {}
        if cache_path.exists():
            try:
                saved = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    saved.get("schema_version") == 2
                    and saved.get("catalog_fingerprint") == self.catalog_fingerprint
                ):
                    self.cache = saved.get("entries") or {}
            except (OSError, json.JSONDecodeError):
                self.cache = {}
        self.hits = 0
        self.misses = 0
        self._lock = Lock()

    @staticmethod
    def _usable(result: dict[str, Any], report_date: date) -> bool:
        category = result.get("category")
        if category in {"us_equity", "non_us_equity", "fixed_income", "commodity"}:
            return bool(result.get("source_url"))
        if category != "global_equity" or result.get("data_date") is None:
            return False
        observed = parse_date(str(result["data_date"]))
        return observed <= report_date and (report_date - observed).days <= 120

    def _from_catalog(self, normalized_name: str, report_date: date) -> dict[str, Any] | None:
        for entry in self.entries:
            aliases = [normalize_instrument_name(alias) for alias in entry.get("aliases") or []]
            if not any(alias and alias in normalized_name for alias in aliases):
                continue
            result = {
                "category": entry["category"],
                "us_equity_pct": float(entry["us_equity_pct"]),
                "data_date": entry.get("data_date"),
                "source_url": entry["source_url"],
                "source_name": entry.get("source_name"),
            }
            if self._usable(result, report_date):
                return result
        return None

    def resolve(self, fund_name: str, report_date: date) -> dict[str, Any] | None:
        normalized = normalize_instrument_name(fund_name)
        result = self._from_catalog(normalized, report_date)
        if result is None:
            with self._lock:
                self.misses += 1
            return None
        key = "|".join(
            (
                normalized,
                report_date.isoformat(),
                str(result.get("data_date") or "structural"),
                str(result["source_url"]),
            )
        )
        with self._lock:
            cached = self.cache.get(key)
            if cached is not None and self._usable(cached, report_date):
                self.hits += 1
                return cached
            self.misses += 1
            self.cache[key] = result
            self._save()
            return result

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            self.cache_path,
            {
                "schema_version": 2,
                "catalog_fingerprint": self.catalog_fingerprint,
                "entries": self.cache,
            },
        )

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses}


def clean_report_text(text: str) -> str:
    header = re.compile(
        r"(?m)^.*?[0-9〇零○ＯO一二三四五六七八九]{4}\s*年"
        r"(?:第\s*[1234一二三四]\s*季度|半年度|年度).*?报告\s*$\n^\s*\d+\s*$\n?"
    )
    text = header.sub("", text)
    text = re.sub(r"(?<=\d)\.\s+(?=\d{1,2}(?:\s|$))", ".", text)
    text = re.sub(
        r"(\d{1,3}(?:,\d{3})+\.\d)\s+(\d)(?=\s)", r"\1\2", text
    )
    text = re.sub(
        r"(\d{1,3}(?:,\d{3})+,\d{1,2})\s+(\d{1,2}\.\d{2})(?=\s)",
        r"\1\2",
        text,
    )
    return re.sub(r"(\d{1,3}(?:,\d{3})+)\s+(\.\d{2})(?=\s)", r"\1\2", text)


def parse_fund_investment_rows(text: str, code: str) -> list[dict[str, Any]]:
    headings = list(re.finditer(r"前十名基金投资明\s*细", text))
    if not headings:
        raise DataError(f"Could not locate top fund investments for fund {code}")
    heading = headings[-1]
    end = re.search(r"投资组合报告附注", text[heading.end() :])
    if not end:
        raise DataError(f"Could not locate end of top fund investments for fund {code}")
    table = text[heading.end() : heading.end() + end.start()]
    row_matches = list(re.finditer(r"(?m)^\s*(10|[1-9])\s+", table))
    rows: list[dict[str, Any]] = []
    for index, match in enumerate(row_matches):
        row_end = row_matches[index + 1].start() if index + 1 < len(row_matches) else len(table)
        body = re.sub(r"\s+", " ", table[match.end() : row_end]).strip()
        name_match = re.match(
            r"(.+?)\s+(?:ETF\s*基\s*金|指数基\s*金|开放式\s*基\s*金|基\s*金)\s+",
            body,
        )
        if not name_match:
            name_match = re.match(r"(.+?)\s+ETF\s+(?:交易型|契约型)", body)
        if not name_match:
            name_match = re.match(r"(.+?)\s+QDII\s+(?:交易型|契约型|开放式)", body)
        if not name_match:
            name_match = re.match(
                r"(.+?)\s+(?:债\s*券\s*型|股\s*票\s*型|混\s*合\s*型|商\s*品\s*型|权\s*益\s*类)\s+",
                body,
            )
        # Periodic reports commonly render unused rows as dash-only placeholders.
        # A page footer may follow the dashes (and include the word "基金"), so
        # identify the placeholder before the generic "基金" fallback below.
        if not name_match and re.match(r"^(?:-\s*){2,}", body):
            continue
        if not name_match and "基金" not in body:
            continue
        percentages = [
            float(value)
            for value in re.findall(r"(?<![\d,])(\d{1,3}\.\d{2})(?!\d)", body)
            if float(value) <= 100
        ]
        # A dash in the percentage column denotes a negligible holding
        # (the reports use it instead of a rounded 0.00).  Preserve the row
        # with a zero weight so the rest of the look-through scan remains
        # complete, while still failing on genuinely malformed rows.
        if not percentages and name_match and re.search(r"\d[\d,]*\.\d{2}\s+-", body):
            percentages = [0.0]
        if not name_match or not percentages:
            raise DataError(
                f"Could not parse top fund investment row {match.group(1)} for fund {code}"
            )
        fund_name = name_match.group(1)
        if re.search(r"商\s*品\s*型", body):
            reported_category = "commodity"
        elif re.search(r"债\s*券\s*型", body):
            reported_category = "fixed_income"
        else:
            reported_category = None
        if len(normalize_instrument_name(fund_name)) < 12:
            continuation = re.search(
                r"\d{1,3}\.\d{2}\s+([A-Z][A-Z0-9& ]+?ETF)(?:\s+[A-Z][a-z]|\s*$)",
                body,
            )
            if continuation:
                fund_name = f"{fund_name} {continuation.group(1)}"
        rows.append(
            {
                "rank": int(match.group(1)),
                "fund_name": fund_name,
                "weight_pct": percentages[0],
                "reported_category": reported_category,
            }
        )
    if not rows:
        raise DataError(f"No top fund investments were parsed for fund {code}")
    return rows


def parse_us_equity_report(text: str, code: str) -> dict[str, Any]:
    text = clean_report_text(text)
    direct_headings = list(re.finditer(
        r"(?:报告期末|期末)在各个国家（地区）证券市场的"
        r"(?:股票及存托\s*凭证|权益)投资分\s*布",
        text,
    ))
    if not direct_headings:
        raise DataError(f"Could not locate country equity distribution for fund {code}")
    direct_heading = direct_headings[-1]
    direct_segment = text[direct_heading.end() : direct_heading.end() + 1200]
    next_heading = re.search(r"\n\s*\d+(?:\.\d+)+\s*", direct_segment)
    if next_heading:
        direct_segment = direct_segment[: next_heading.start()]
    direct_match = re.search(
        r"美国\s+([\d,.]+)\s+(\d+(?:\.\d+)?)", re.sub(r"\s+", " ", direct_segment)
    )
    direct_us_pct = float(direct_match.group(2)) if direct_match else 0.0

    asset_headings = list(re.finditer(r"(?:报告期末|期末)基金资产组合情况", text))
    if not asset_headings:
        raise DataError(f"Could not locate asset allocation table for fund {code}")
    asset_heading = asset_headings[-1]
    asset_segment = re.sub(r"\s+", " ", text[asset_heading.end() : asset_heading.end() + 2500])
    fund_amount_match = re.search(r"(?:^|\s)2\s+基金投资\s+([\d,.]+)\s+\d+(?:\.\d+)?", asset_segment)
    no_fund_investment = bool(
        re.search(r"(?:^|\s)2\s+基金投资\s+(?:[－—-]\s*){1,2}(?:\s|$)", asset_segment)
    )
    if not fund_amount_match and not no_fund_investment:
        raise DataError(f"Could not parse fund investment amount for fund {code}")
    fund_amount = (
        float(fund_amount_match.group(1).replace(",", "")) if fund_amount_match else 0.0
    )

    holdings: list[dict[str, Any]] = []
    total_fund_pct = 0.0
    if fund_amount > 0:
        net_match = re.search(
            r"期末基金\s*资产\s*净值(.{0,1400}?)"
            r"5\.\s*期末基金\s*份额\s*净值",
            text,
            re.S,
        )
        net_values: list[float] = []
        if net_match:
            net_values = [
                float(value.replace(",", ""))
                for value in re.findall(r"(?<!\d)(\d[\d,]*\.\d{2})(?!\d)", net_match.group(1))
            ]
        else:
            # Midyear and annual reports disclose the aggregate in their balance sheet.
            balance_headings = list(re.finditer(r"\d+\.\d+\s+资产负债表", text))
            for balance_heading in reversed(balance_headings):
                balance_segment = text[balance_heading.end() : balance_heading.end() + 8000]
                balance_match = re.search(r"净资产合计\s+(\d[\d,]*\.\d{2})", balance_segment)
                if balance_match:
                    net_values = [float(balance_match.group(1).replace(",", ""))]
                    break
        if not net_values or sum(net_values) <= 0:
            raise DataError(f"Could not parse positive fund net assets for fund {code}")
        total_fund_pct = fund_amount / sum(net_values) * 100
        holdings = parse_fund_investment_rows(text, code)
    return {
        "direct_us_pct": round(direct_us_pct, 4),
        "fund_investment_pct": round(total_fund_pct, 4),
        "fund_holdings": holdings,
    }


def calculate_us_equity_exposure_base(
    parsed: dict[str, Any],
    report: PeriodicReport,
    resolver: LookthroughResolver,
) -> tuple[dict[str, Any], list[str]]:
    direct_us_pct = float(parsed["direct_us_pct"])
    lookthrough_confirmed = 0.0
    unresolved = 0.0
    components: list[dict[str, Any]] = []
    warnings: list[str] = []
    disclosed_weight = 0.0
    for holding in parsed["fund_holdings"]:
        weight = float(holding["weight_pct"])
        disclosed_weight += weight
        if holding.get("reported_category") in {"commodity", "fixed_income"}:
            resolved = {
                "category": holding["reported_category"],
                "us_equity_pct": 0.0,
                "data_date": report.report_date.isoformat(),
                "source_url": report.source_url,
            }
        else:
            resolved = resolver.resolve(holding["fund_name"], report.report_date)
        if resolved is None:
            contribution = 0.0
            possible_contribution = weight
            unresolved += weight
            if weight >= 0.005:
                warnings.append(
                    f"{holding['fund_name']} 无法按 {report.report_date} 的可用数据穿透，"
                    f"其 {weight:.2f}% 仓位仅计入可能上限。"
                )
            category = "unresolved"
            exposure_pct = None
            source_url = None
            data_date = None
        else:
            exposure_pct = float(resolved["us_equity_pct"])
            contribution = weight * exposure_pct / 100
            possible_contribution = contribution
            category = str(resolved["category"])
            source_url = resolved.get("source_url")
            data_date = resolved.get("data_date")
            lookthrough_confirmed += contribution
        components.append(
            {
                "fund_name": holding["fund_name"],
                "weight_pct": round(weight, 4),
                "category": category,
                "us_equity_pct": exposure_pct,
                "confirmed_contribution_pct": round(contribution, 4),
                "possible_contribution_pct": round(possible_contribution, 4),
                "data_date": data_date,
                "source_url": source_url,
            }
        )
    residual = max(0.0, float(parsed["fund_investment_pct"]) - disclosed_weight)
    if residual > 0.01:
        unresolved += residual
        warnings.append(
            f"前十大基金之外尚有 {residual:.2f}% 基金仓位未披露，仅计入可能上限。"
        )
        components.append(
            {
                "fund_name": "前十大之外未披露基金仓位",
                "weight_pct": round(residual, 4),
                "category": "unresolved_residual",
                "us_equity_pct": None,
                "confirmed_contribution_pct": 0.0,
                "possible_contribution_pct": round(residual, 4),
                "data_date": None,
                "source_url": report.source_url,
            }
        )
    confirmed = min(100.0, direct_us_pct + lookthrough_confirmed)
    possible = min(100.0, confirmed + unresolved)
    return (
        {
            "confirmed_pct": round(confirmed, 2),
            "possible_pct": round(possible, 2),
            "direct_us_pct": round(direct_us_pct, 2),
            "lookthrough_confirmed_pct": round(lookthrough_confirmed, 2),
            "unresolved_pct": round(unresolved, 2),
            "report_date": report.report_date.isoformat(),
            "published_date": report.published_date.isoformat(),
            "source_url": report.source_url,
            "components": components,
        },
        warnings,
    )


def apply_us_equity_threshold(
    exposure: dict[str, Any], threshold: float
) -> tuple[dict[str, Any], list[str]]:
    confirmed = float(exposure["confirmed_pct"])
    possible = float(exposure["possible_pct"])
    warnings: list[str] = []
    if confirmed >= threshold:
        status = "qualified"
    elif possible < threshold:
        status = "excluded"
    else:
        status = "ambiguous"
        warnings.append(
            f"美股占比区间 {confirmed:.2f}%-{possible:.2f}% 跨越 {threshold:g}% 阈值，"
            "按确认下限进入全球补充榜。"
        )
    return {**exposure, "status": status}, warnings


def calculate_us_equity_exposure(
    parsed: dict[str, Any],
    report: PeriodicReport,
    resolver: LookthroughResolver,
    threshold: float,
) -> tuple[dict[str, Any], list[str]]:
    exposure, warnings = calculate_us_equity_exposure_base(parsed, report, resolver)
    classified, threshold_warnings = apply_us_equity_threshold(exposure, threshold)
    return classified, [*warnings, *threshold_warnings]


class FundExposureResultCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()

    @staticmethod
    def _validate(
        payload: dict[str, Any],
        code: str,
        report: PeriodicReport,
        as_of: date,
        catalog_fingerprint: str,
    ) -> tuple[dict[str, Any], list[str]]:
        if (
            payload.get("schema_version") != FUND_EXPOSURE_CACHE_SCHEMA_VERSION
            or payload.get("method_version") != US_EQUITY_METHOD_VERSION
            or payload.get("catalog_fingerprint") != catalog_fingerprint
            or payload.get("code") != code
            or payload.get("announcement_id") != report.announcement_id
            or payload.get("report_date") != report.report_date.isoformat()
            or payload.get("published_date") != report.published_date.isoformat()
        ):
            raise DataError("Cached US-equity exposure identity does not match the report")
        if report.report_date > as_of or report.published_date > as_of:
            raise DataError("Cached US-equity exposure uses a future report")
        exposure = payload.get("exposure")
        warnings = payload.get("warnings")
        if not isinstance(exposure, dict) or not isinstance(warnings, list):
            raise DataError("Cached US-equity exposure is incomplete")
        required_fields = {
            "confirmed_pct",
            "possible_pct",
            "direct_us_pct",
            "lookthrough_confirmed_pct",
            "unresolved_pct",
            "report_date",
            "published_date",
            "source_url",
            "components",
        }
        if not required_fields.issubset(exposure) or not isinstance(
            exposure["components"], list
        ):
            raise DataError("Cached US-equity exposure fields are incomplete")
        if (
            exposure["report_date"] != report.report_date.isoformat()
            or exposure["published_date"] != report.published_date.isoformat()
            or exposure["source_url"] != report.source_url
        ):
            raise DataError("Cached US-equity exposure source does not match the report")
        confirmed = float(exposure["confirmed_pct"])
        possible = float(exposure["possible_pct"])
        if not 0 <= confirmed <= possible <= 100:
            raise DataError("Cached US-equity exposure interval is invalid")
        for component in exposure["components"]:
            if not isinstance(component, dict):
                raise DataError("Cached US-equity component is invalid")
            data_date = component.get("data_date")
            if data_date is None:
                continue
            observed = parse_date(str(data_date))
            if observed > report.report_date:
                raise DataError("Cached US-equity component uses future data")
            if (
                component.get("category") == "global_equity"
                and (report.report_date - observed).days > 120
            ):
                raise DataError("Cached global-equity component is stale")
        if not all(isinstance(warning, str) for warning in warnings):
            raise DataError("Cached US-equity warnings are invalid")
        return dict(exposure), list(warnings)

    def get(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        as_of: date,
        report_cache: PeriodicReportCache,
        resolver: LookthroughResolver,
        threshold: float,
        report: PeriodicReport | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        code = fund["code"]
        if report is None:
            report = fetch_latest_periodic_report(client, code, as_of)
        if report.report_date > as_of or report.published_date > as_of:
            raise DataError(f"Periodic report for fund {code} is later than {as_of}")
        path = self.directory / code / f"{report.announcement_id}.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                exposure, warnings = self._validate(
                    payload, code, report, as_of, resolver.catalog_fingerprint
                )
                with self._stats_lock:
                    self.hits += 1
                classified, threshold_warnings = apply_us_equity_threshold(
                    exposure, threshold
                )
                return classified, [*warnings, *threshold_warnings]
            except (DataError, OSError, TypeError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
        with self._stats_lock:
            self.misses += 1
        text = report_cache.get_text(client, report, fund["fund_page_url"])
        parsed = parse_us_equity_report(text, code)
        exposure, warnings = calculate_us_equity_exposure_base(parsed, report, resolver)
        write_json(
            path,
            {
                "schema_version": FUND_EXPOSURE_CACHE_SCHEMA_VERSION,
                "method_version": US_EQUITY_METHOD_VERSION,
                "catalog_fingerprint": resolver.catalog_fingerprint,
                "code": code,
                "announcement_id": report.announcement_id,
                "report_date": report.report_date.isoformat(),
                "published_date": report.published_date.isoformat(),
                "exposure": exposure,
                "warnings": warnings,
            },
        )
        classified, threshold_warnings = apply_us_equity_threshold(exposure, threshold)
        return classified, [*warnings, *threshold_warnings]

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }


def fetch_us_equity_exposure(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    report_cache: PeriodicReportCache,
    resolver: LookthroughResolver,
    threshold: float,
    exposure_cache: FundExposureResultCache | None = None,
    report: PeriodicReport | None = None,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if exposure_cache is not None:
        return exposure_cache.get(
            client, fund, as_of, report_cache, resolver, threshold, report=report
        )
    if report is None:
        report = fetch_latest_periodic_report(
            client, fund["code"], as_of, snapshot=snapshot
        )
    text = report_cache.get_text(client, report, fund["fund_page_url"])
    parsed = parse_us_equity_report(text, fund["code"])
    return calculate_us_equity_exposure(parsed, report, resolver, threshold)


def fetch_announcements(
    client: HttpClient,
    code: str,
    as_of: date,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> list[dict[str, Any]]:
    if snapshot is not None:
        if snapshot.code != code or snapshot.as_of != as_of:
            raise DataError("Announcement snapshot identity does not match quota request")
        latest_ids = set(snapshot.latest_page_ids)
        records = tuple(
            item
            for item in snapshot.items
            if not latest_ids or item.announcement_id in latest_ids
        )
    else:
        records = tuple(
            _parse_announcement_page(_announcement_page(client, code, 1), code)
        )
    notices = []
    cutoff = as_of - timedelta(days=550)
    for item in records:
        published = item.published_date
        if published > as_of or published < cutoff or not NOTICE_TITLE_RE.search(item.title):
            continue
        announcement_id = item.announcement_id
        notices.append(
            {
                "id": announcement_id,
                "title": item.title,
                "published": published,
                "url": item.source_url,
            }
        )
    notices.sort(key=lambda item: item["published"])
    return notices[-12:]


class QuotaNoticeParseCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.failures = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()
        self._key_locks: dict[str, Lock] = {}
        self._key_locks_lock = Lock()

    @staticmethod
    def _identity(notice: dict[str, Any]) -> dict[str, str]:
        return {
            "id": str(notice["id"]),
            "title": str(notice["title"]),
            "published_date": notice["published"].isoformat(),
            "source_url": str(notice["url"]),
        }

    @staticmethod
    def _encode_transitions(
        transitions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                **transition,
                "effective_date": transition["effective_date"].isoformat(),
            }
            for transition in transitions
        ]

    @staticmethod
    def _decode(
        payload: dict[str, Any], identity: dict[str, str]
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        if (
            payload.get("schema_version") != QUOTA_NOTICE_CACHE_SCHEMA_VERSION
            or payload.get("method_version") != QUOTA_NOTICE_METHOD_VERSION
            or payload.get("identity") != identity
            or not isinstance(payload.get("ok"), bool)
        ):
            raise DataError("Cached quota notice identity is invalid")
        if not payload["ok"]:
            error = payload.get("error")
            if not isinstance(error, str) or not error:
                raise DataError("Cached quota notice failure is invalid")
            return None, error
        raw_transitions = payload.get("transitions")
        if not isinstance(raw_transitions, list) or not raw_transitions:
            raise DataError("Cached quota notice transitions are empty")
        transitions: list[dict[str, Any]] = []
        for item in raw_transitions:
            if not isinstance(item, dict) or "effective_date" not in item:
                raise DataError("Cached quota transition is invalid")
            transition = dict(item)
            transition["effective_date"] = parse_date(str(item["effective_date"]))
            if transition.get("source_url") != identity["source_url"]:
                raise DataError("Cached quota transition source is invalid")
            transitions.append(transition)
        return transitions, None

    def _get_locked(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        notice: dict[str, Any],
        document_cache: PeriodicReportCache,
    ) -> list[dict[str, Any]]:
        identity = self._identity(notice)
        path = self.directory / f"{identity['id']}.json"
        if path.exists():
            try:
                transitions, error = self._decode(
                    json.loads(path.read_text(encoding="utf-8")), identity
                )
                with self._stats_lock:
                    self.hits += 1
                if error is not None:
                    raise DataError(error)
                if transitions is None:
                    raise DataError("Cached quota notice result is missing")
                return transitions
            except DataError as exc:
                if str(exc) and "Cached quota" not in str(exc):
                    raise
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
            except (OSError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1

        document = LegalDocument(
            identity["id"],
            identity["title"],
            notice["published"],
            identity["source_url"],
            "quota_notice",
        )
        try:
            text = document_cache.get_text(client, document, fund["fund_page_url"])
            transitions = parse_quota_notice(
                text, notice["published"], identity["source_url"]
            )
            if not transitions:
                raise DataError("quota notice produced no effective limit transition")
        except DataError as exc:
            write_json(
                path,
                {
                    "schema_version": QUOTA_NOTICE_CACHE_SCHEMA_VERSION,
                    "method_version": QUOTA_NOTICE_METHOD_VERSION,
                    "identity": identity,
                    "ok": False,
                    "error": str(exc),
                },
            )
            with self._stats_lock:
                self.misses += 1
                self.failures += 1
            raise
        write_json(
            path,
            {
                "schema_version": QUOTA_NOTICE_CACHE_SCHEMA_VERSION,
                "method_version": QUOTA_NOTICE_METHOD_VERSION,
                "identity": identity,
                "ok": True,
                "transitions": self._encode_transitions(transitions),
            },
        )
        with self._stats_lock:
            self.misses += 1
        return transitions

    def get(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        notice: dict[str, Any],
        document_cache: PeriodicReportCache,
    ) -> list[dict[str, Any]]:
        key = str(notice["id"])
        with self._key_locks_lock:
            key_lock = self._key_locks.setdefault(key, Lock())
        with key_lock:
            return self._get_locked(client, fund, notice, document_cache)

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "failures": self.failures,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }


def new_limit_state(status: str = "unknown", amount: int | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "amount_cny": amount,
        "effective_date": None,
        "source_url": None,
        "confidence": "low" if status == "unknown" else "medium",
    }


def apply_limit(
    state: dict[str, Any], status: str, amount: int | None, transition: dict[str, Any]
) -> None:
    state.update(
        {
            "status": status,
            "amount_cny": amount,
            "effective_date": transition["effective_date"].isoformat(),
            "source_url": transition["source_url"],
            "confidence": transition.get("confidence", "medium"),
        }
    )


def resolve_quota(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    document_cache: PeriodicReportCache | None = None,
    snapshot: FundAnnouncementSnapshot | None = None,
    notice_cache: QuotaNoticeParseCache | None = None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if fund["purchase_status"] == "open" and fund.get("page_agency_limit_cny") is None:
        direct = new_limit_state("unlimited")
        agency = new_limit_state("unlimited")
    else:
        direct = new_limit_state()
        agency = new_limit_state(
            "limited" if fund.get("page_agency_limit_cny") else "unknown",
            fund.get("page_agency_limit_cny"),
        )
        if agency["status"] == "limited":
            agency["source_url"] = fund["fund_page_url"]
            agency["confidence"] = "medium"

    aggregation = "not applicable"
    all_channels_combined = False
    applied_sources: set[str] = set()
    transitions: list[dict[str, Any]] = []
    latest_unparsed_notice_date: date | None = None
    notices = fetch_announcements(client, fund["code"], as_of, snapshot=snapshot)
    for notice in notices:
        try:
            if notice_cache is not None:
                if document_cache is None:
                    raise DataError("Quota notice cache requires the PDF document cache")
                parsed_transitions = notice_cache.get(
                    client, fund, notice, document_cache
                )
            elif document_cache is None:
                pdf = client.get_bytes(notice["url"], referer=fund["fund_page_url"])
                text = extract_pdf_text(pdf)
                parsed_transitions = parse_quota_notice(
                    text, notice["published"], notice["url"]
                )
            else:
                document = LegalDocument(
                    str(notice["id"]),
                    str(notice["title"]),
                    notice["published"],
                    str(notice["url"]),
                    "quota_notice",
                )
                text = document_cache.get_text(
                    client, document, fund["fund_page_url"]
                )
                parsed_transitions = parse_quota_notice(
                    text, notice["published"], notice["url"]
                )
            if not parsed_transitions:
                raise DataError("quota notice produced no effective limit transition")
            transitions.extend(parsed_transitions)
        except DataError as exc:
            warnings.append(f"{fund['code']} quota notice could not be parsed: {exc}")
            if (
                latest_unparsed_notice_date is None
                or notice["published"] > latest_unparsed_notice_date
            ):
                latest_unparsed_notice_date = notice["published"]

    if latest_unparsed_notice_date is not None:
        # A newer unreadable limit notice invalidates page state and older
        # transitions. A later readable notice can establish a fresh state.
        direct = new_limit_state()
        agency = new_limit_state()
        transitions = [
            item
            for item in transitions
            if parse_date(str(item["published_date"])) > latest_unparsed_notice_date
        ]

    transitions.sort(key=lambda item: (item["effective_date"], item["published_date"]))
    for transition in transitions:
        if transition["effective_date"] > as_of:
            continue
        if transition.get("share_aggregation"):
            aggregation = transition["share_aggregation"]
        if transition.get("all_channels_combined"):
            all_channels_combined = True
        global_amount = transition.get("global_amount_cny")
        direct_amount = transition.get("direct_amount_cny")
        agency_amount = transition.get("agency_amount_cny")
        global_status = transition.get("global_status", "limited")
        if direct_amount is not None:
            apply_limit(direct, "limited", direct_amount, transition)
            applied_sources.add(transition["source_url"])
        if agency_amount is not None:
            apply_limit(agency, "limited", agency_amount, transition)
            applied_sources.add(transition["source_url"])
        if direct_amount is None and agency_amount is None and global_status == "unlimited":
            apply_limit(direct, "unlimited", None, transition)
            apply_limit(agency, "unlimited", None, transition)
            applied_sources.add(transition["source_url"])
        elif global_amount is not None:
            if direct_amount is None:
                apply_limit(direct, "limited", global_amount, transition)
            if agency_amount is None:
                apply_limit(agency, "limited", global_amount, transition)
            applied_sources.add(transition["source_url"])

    if direct["status"] == "unknown" or agency["status"] == "unknown":
        warnings.append(
            f"{fund['code']} 的申购额度无法确定；引用前请核对关联公告。"
        )
    statuses = {direct["status"], agency["status"]}
    if "unknown" in statuses:
        quota_status = "unknown"
        confidence = "low"
    elif "limited" in statuses:
        quota_status = "limited"
        confidence = "high" if all(item["confidence"] == "high" for item in (direct, agency)) else "medium"
    else:
        quota_status = "unlimited"
        confidence = "medium"
    if all_channels_combined:
        channel_rule = "all sales channels combined"
    elif direct["amount_cny"] != agency["amount_cny"]:
        channel_rule = "direct and agency limits differ"
    else:
        channel_rule = "same fund-level limit"
    return (
        {
            "quota_status": quota_status,
            "quota_confidence": confidence,
            "direct_limit": direct,
            "agency_limit": agency,
            "share_class_rule": aggregation,
            "channel_rule": channel_rule,
            "quota_source_urls": sorted(applied_sources),
        },
        warnings,
    )


def format_limit(limit: dict[str, Any]) -> str:
    if limit["status"] == "unlimited":
        return "正常开放"
    if limit["status"] == "suspended":
        return "暂停申购"
    if limit["status"] == "unknown" or limit.get("amount_cny") is None:
        return "待核实"
    amount = int(limit["amount_cny"])
    if amount >= 10000 and amount % 10000 == 0:
        return f"{amount // 10000:,}万元"
    return f"{amount:,}元"


def format_rule(share_class_rule: str, channel_rule: str) -> str:
    share_labels = {
        "A/C combined": "A/C合并",
        "A/C separate": "A/C分别",
        "not applicable": "不适用",
    }
    channel_labels = {
        "all sales channels combined": "全部渠道合计",
        "direct and agency limits differ": "直销/代销分别",
        "same fund-level limit": "同一基金级限额",
    }
    share = share_labels.get(share_class_rule, share_class_rule)
    channel = channel_labels.get(channel_rule, channel_rule)
    if share == "不适用" and channel == "同一基金级限额":
        return "不适用"
    return f"{share}；{channel}"


def format_percentage(value: float | None, show_sign: bool = False) -> str:
    if value is None:
        return "待核实"
    return f"{value:+.2f}%" if show_sign else f"{value:.2f}%"


def format_correlation(value: float) -> str:
    return f"{value * 100:.1f}%"


def format_beta(value: float) -> str:
    return f"{value:.2f}"


def format_optional_percentage(value: Any, show_sign: bool = False) -> str:
    if value is None:
        return "--"
    return format_percentage(float(value), show_sign=show_sign)


def format_holding_cost(cost: dict[str, Any]) -> str:
    value = cost.get("annualized_pct")
    return "--" if value is None else f"{float(value):.2f}%/年"


def benchmark_display(profile: dict[str, Any]) -> str:
    if profile["status"] == "recognized" and profile["benchmark_weight_pct"] is not None:
        return f"{profile['benchmark_name']} · {float(profile['benchmark_weight_pct']):g}%"
    return str(profile["benchmark_name"])


def format_long_return(record: dict[str, Any], prefix: str) -> str:
    value = record.get(f"{prefix}_return_pct")
    if value is not None:
        return format_percentage(float(value), show_sign=True)
    return f"--（净值始于 {record['nav_history_start_date']}）"


def summarize_periods(records: list[dict[str, Any]], prefix: str) -> str:
    periods = sorted(
        {
            (
                item.get(f"{prefix}_performance_start_date"),
                item.get(f"{prefix}_performance_end_date"),
            )
            for item in records
            if item.get(f"{prefix}_performance_start_date")
            and item.get(f"{prefix}_performance_end_date")
        }
    )
    return "、".join(f"{start} 至 {end}" for start, end in periods) or "无完整区间"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_exchange_premium_catalog(path: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataError(f"Could not read US-equity ETF catalog {path}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ETF_PREMIUM_CATALOG_SCHEMA_VERSION
        or not isinstance(payload.get("entries"), list)
    ):
        raise DataError("US-equity ETF catalog has an unsupported schema")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in payload["entries"]:
        if not isinstance(raw_entry, dict):
            raise DataError("US-equity ETF catalog entries must be objects")
        code = str(raw_entry.get("code", ""))
        name = str(raw_entry.get("name", "")).strip()
        exchange = str(raw_entry.get("exchange", ""))
        market_id = raw_entry.get("market_id")
        category = str(raw_entry.get("category", ""))
        benchmark_group = str(raw_entry.get("benchmark_group", ""))
        source_url = str(raw_entry.get("source_url", ""))
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            raise DataError(f"Invalid or duplicate ETF code in catalog: {code!r}")
        if not name:
            raise DataError(f"ETF catalog name is missing for {code}")
        if exchange not in {"SSE", "SZSE"} or market_id not in {0, 1}:
            raise DataError(f"ETF catalog exchange is invalid for {code}")
        if (exchange == "SSE") != (market_id == 1):
            raise DataError(f"ETF catalog market ID differs from exchange for {code}")
        if category not in {"broad_market", "sector_theme"}:
            raise DataError(f"ETF catalog category is invalid for {code}")
        if benchmark_group not in ETF_PREMIUM_GROUP_ORDER:
            raise DataError(f"ETF catalog benchmark group is invalid for {code}")
        if not source_url.startswith("https://"):
            raise DataError(f"ETF catalog source URL is invalid for {code}")
        seen.add(code)
        entries.append(
            {
                "code": code,
                "name": name,
                "exchange": exchange,
                "market_id": int(market_id),
                "category": category,
                "benchmark_group": benchmark_group,
                "source_url": source_url,
            }
        )
    if not entries:
        raise DataError("US-equity ETF catalog is empty")
    order = {group: index for index, group in enumerate(ETF_PREMIUM_GROUP_ORDER)}
    entries.sort(key=lambda item: (order[item["benchmark_group"]], item["code"]))
    return entries, hashlib.sha256(raw).hexdigest()


class ExchangePremiumHoldingCostCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.stale_fallbacks = 0
        self.writes = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()

    @staticmethod
    def _decode(
        payload: dict[str, Any], code: str, as_of: date
    ) -> tuple[str | None, dict[str, Any]]:
        if (
            payload.get("schema_version") != ETF_HOLDING_COST_CACHE_SCHEMA_VERSION
            or payload.get("method_version") != ETF_HOLDING_COST_METHOD_VERSION
            or payload.get("code") != code
            or (
                payload.get("summary_id") is not None
                and not isinstance(payload.get("summary_id"), str)
            )
            or not isinstance(payload.get("holding_cost"), dict)
        ):
            raise DataError("Cached ETF holding-cost identity is invalid")
        cost = dict(payload["holding_cost"])
        if set(cost) != {
            "status",
            "annualized_pct",
            "measurement_date",
            "source_title",
            "source_published_date",
            "source_url",
        } or cost["status"] not in {"parsed", "unavailable"}:
            raise DataError("Cached ETF holding-cost fields are invalid")
        value = cost["annualized_pct"]
        if cost["status"] == "parsed":
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise DataError("Cached ETF holding cost is non-numeric") from exc
            if not math.isfinite(numeric) or numeric < 0 or numeric > 100:
                raise DataError("Cached ETF holding cost is outside its valid range")
            cost["annualized_pct"] = round(numeric, 2)
            if (
                not isinstance(cost["source_title"], str)
                or not cost["source_title"].strip()
                or not isinstance(cost["source_published_date"], str)
                or not str(cost["source_url"] or "").startswith("https://")
            ):
                raise DataError("Cached ETF holding-cost source is invalid")
        else:
            if value is not None:
                raise DataError("Unavailable cached ETF holding cost has a value")
            source_values = (
                cost["source_title"],
                cost["source_published_date"],
                cost["source_url"],
            )
            if not (
                all(item is None for item in source_values)
                or (
                    isinstance(source_values[0], str)
                    and bool(source_values[0].strip())
                    and isinstance(source_values[1], str)
                    and str(source_values[2] or "").startswith("https://")
                )
            ):
                raise DataError("Unavailable cached ETF holding-cost source is incomplete")
        for field in ("measurement_date", "source_published_date"):
            raw_date = cost[field]
            if raw_date is not None and parse_date(str(raw_date)) > as_of:
                raise DataError("Cached ETF holding cost contains future data")
        return payload.get("summary_id"), cost

    def load(
        self, code: str, as_of: date
    ) -> tuple[str | None, dict[str, Any]] | None:
        path = self.directory / f"{code}.json"
        if not path.exists():
            return None
        try:
            result = self._decode(
                json.loads(path.read_text(encoding="utf-8")), code, as_of
            )
        except (DataError, OSError, ValueError, json.JSONDecodeError):
            with self._stats_lock:
                self.corrupt_rebuilds += 1
            return None
        return result

    def save(
        self, code: str, summary_id: str | None, holding_cost: dict[str, Any]
    ) -> None:
        write_json(
            self.directory / f"{code}.json",
            {
                "schema_version": ETF_HOLDING_COST_CACHE_SCHEMA_VERSION,
                "method_version": ETF_HOLDING_COST_METHOD_VERSION,
                "code": code,
                "summary_id": summary_id,
                "holding_cost": holding_cost,
            },
        )
        with self._stats_lock:
            self.writes += 1

    def mark_hit(self) -> None:
        with self._stats_lock:
            self.hits += 1

    def mark_miss(self) -> None:
        with self._stats_lock:
            self.misses += 1

    def mark_stale_fallback(self) -> None:
        with self._stats_lock:
            self.stale_fallbacks += 1

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "stale_fallbacks": self.stale_fallbacks,
                "writes": self.writes,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }


def resolve_exchange_premium_holding_cost(
    client: HttpClient,
    entry: dict[str, Any],
    as_of: date,
    announcement_cache: AnnouncementIndexCache,
    document_cache: PeriodicReportCache,
    result_cache: ExchangePremiumHoldingCostCache,
) -> tuple[dict[str, Any], list[str]]:
    code = entry["code"]
    cached = result_cache.load(code, as_of)
    try:
        snapshot = announcement_cache.get(client, code, as_of)
    except (DataError, OSError, ValueError) as exc:
        if cached is not None and cached[1]["status"] == "parsed":
            result_cache.mark_stale_fallback()
            return (
                {**cached[1], "status": "stale"},
                [f"场内费率告警 {code}：公告索引无法读取，使用上次费率：{exc}"],
            )
        return (
            unavailable_holding_cost(None),
            [f"场内费率告警 {code}：公告索引无法读取，且没有可用旧值：{exc}"],
        )

    try:
        _prospectus, summary = fetch_latest_legal_documents(
            client, code, as_of, snapshot=snapshot
        )
    except (DataError, OSError, ValueError) as exc:
        result_cache.mark_miss()
        return (
            unavailable_holding_cost(None),
            [f"场内费率告警 {code}：无法选择最新产品概要：{exc}"],
        )
    if summary is None:
        result_cache.mark_miss()
        cost = unavailable_holding_cost(None)
        warnings = [f"场内费率告警 {code}：截至 {as_of} 没有可用的产品概要。"]
        try:
            result_cache.save(code, None, cost)
        except OSError as exc:
            warnings.append(f"场内费率告警 {code}：无法保存费率缓存：{exc}")
        return cost, warnings

    if cached is not None and cached[0] == summary.announcement_id and cached[1]["status"] == "parsed":
        result_cache.mark_hit()
        return cached[1], []

    result_cache.mark_miss()
    try:
        summary_text = document_cache.get_text(
            client, summary, FUND_PAGE_URL.format(code=code)
        )
        cost = parse_holding_cost(summary_text, summary, as_of)
        warnings: list[str] = []
    except (DataError, OSError, ValueError) as exc:
        cost = unavailable_holding_cost(summary)
        warnings = [f"场内费率告警 {code}：最新产品概要无法解析：{exc}"]
    try:
        result_cache.save(code, summary.announcement_id, cost)
    except OSError as exc:
        warnings.append(f"场内费率告警 {code}：无法保存费率缓存：{exc}")
    return cost, warnings


def build_exchange_premium_holding_costs(
    client: HttpClient,
    entries: list[dict[str, Any]],
    as_of: date,
    announcement_cache: AnnouncementIndexCache,
    document_cache: PeriodicReportCache,
    result_cache: ExchangePremiumHoldingCostCache,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    costs: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=min(DOCUMENT_WORKERS, len(entries))) as executor:
        futures = {
            executor.submit(
                resolve_exchange_premium_holding_cost,
                client,
                entry,
                as_of,
                announcement_cache,
                document_cache,
                result_cache,
            ): entry["code"]
            for entry in entries
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                cost, item_warnings = future.result()
            except Exception as exc:  # pragma: no cover - defensive isolation for auxiliary data
                cost = unavailable_holding_cost(None)
                item_warnings = [f"场内费率告警 {code}：费率处理失败：{exc}"]
            costs[code] = cost
            warnings.extend(item_warnings)
    return costs, warnings


def attach_exchange_premium_holding_costs(
    section: dict[str, Any], costs: dict[str, dict[str, Any]]
) -> None:
    for record in section["records"]:
        record["holding_cost"] = costs.get(
            record["code"], unavailable_holding_cost(None)
        )
    section["schema_version"] = 2


def exchange_premium_quote_url(entries: list[dict[str, Any]]) -> str:
    if any(entry.get("category") == "qdii" for entry in entries):
        return exchange_premium_market_url()
    params = {
        "fltt": "2",
        "invt": "2",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "secids": ",".join(
            f"{entry['market_id']}.{entry['code']}" for entry in entries
        ),
        "fields": "f2,f3,f6,f12,f13,f14,f18,f124,f297,f402,f441",
    }
    return f"{ETF_QUOTE_API_URL}?{urllib.parse.urlencode(params)}"


def _finite_quote_number(value: Any, label: str, code: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{code} {label} is missing or non-numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{code} {label} is not finite")
    return number


def normalize_exchange_premium_quote(
    raw: dict[str, Any],
    catalog_entry: dict[str, Any],
    as_of: date,
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = catalog_entry["code"]
    if str(raw.get("f12", "")) != code:
        raise ValueError(f"Quote code differs from requested ETF {code}")
    price = _finite_quote_number(raw.get("f2"), "price", code)
    source_discount = _finite_quote_number(raw.get("f402"), "discount rate", code)
    change_pct = _finite_quote_number(raw.get("f3"), "change percentage", code)
    turnover_cny = _finite_quote_number(raw.get("f6"), "turnover", code)
    raw_iopv = raw.get("f441")
    if raw_iopv not in {None, "", "-"}:
        reference_value = _finite_quote_number(raw_iopv, "IOPV", code)
        reference_type = "iopv"
        reference_date: str | None = None
        reference_source_url = ETF_QUOTE_PAGE_URL
    elif reference and reference.get("reference_value_type") == "nav":
        reference_value = _finite_quote_number(
            reference.get("reference_value_cny"), "NAV", code
        )
        reference_type = "nav"
        reference_date = str(reference.get("reference_value_date") or "")
        reference_source_url = str(reference.get("reference_value_source_url") or "")
        if not reference_date or not reference_source_url.startswith("https://"):
            raise ValueError(f"{code} NAV reference metadata is incomplete")
    else:
        raise ValueError(f"{code} has neither IOPV nor a verified NAV reference")
    if price <= 0 or reference_value <= 0 or turnover_cny < 0:
        raise ValueError(
            f"{code} price, reference value, or turnover is outside its valid range"
        )
    try:
        quote_date = datetime.strptime(str(raw.get("f297")), "%Y%m%d").date()
        updated_timestamp = int(raw.get("f124"))
        updated_at = datetime.fromtimestamp(updated_timestamp, SHANGHAI_TZ)
    except (TypeError, ValueError, OSError) as exc:
        raise ValueError(f"{code} quote date or timestamp is invalid") from exc
    if quote_date > as_of or updated_at.date() > as_of:
        raise ValueError(f"{code} quote contains future data")
    if reference_type == "nav" and parse_date(reference_date) > as_of:
        raise ValueError(f"{code} NAV reference contains future data")
    premium_pct = -source_discount
    calculated_premium = (price / reference_value - 1) * 100
    tolerance_pct_points = max(0.05, 0.0005 / reference_value * 100 + 0.01)
    if abs(premium_pct - calculated_premium) > tolerance_pct_points:
        raise ValueError(
            f"{code} premium differs from price/{reference_type.upper()} calculation by "
            f"{abs(premium_pct - calculated_premium):.3f} percentage points"
        )
    return {
        **catalog_entry,
        "name": str(raw.get("f14") or catalog_entry["name"]).strip(),
        "market_price_cny": round(price, 4),
        "iopv_cny": round(reference_value, 4) if reference_type == "iopv" else None,
        "reference_value_type": reference_type,
        "reference_value_cny": round(reference_value, 4),
        "reference_value_date": reference_date,
        "reference_value_source_url": reference_source_url,
        "source_discount_pct": round(source_discount, 2),
        "premium_pct": round(premium_pct, 2),
        "change_pct": round(change_pct, 2),
        "turnover_cny": round(turnover_cny, 3),
        "quote_date": quote_date.isoformat(),
        "updated_at": updated_at.isoformat(timespec="seconds"),
        "quote_source_url": (
            f"https://quote.eastmoney.com/{'sh' if catalog_entry['market_id'] == 1 else 'sz'}{code}.html"
        ),
    }


def _cached_exchange_quote_valid(
    record: Any, entry: dict[str, Any], as_of: date
) -> bool:
    if not isinstance(record, dict) or record.get("code") != entry["code"]:
        return False
    try:
        quote_date = parse_date(str(record.get("quote_date", "")))
        updated_at = datetime.fromisoformat(str(record.get("updated_at", "")))
        reference_type = str(record.get("reference_value_type") or "iopv")
        reference_value = float(
            record.get("reference_value_cny", record.get("iopv_cny"))
        )
        reference_date = record.get("reference_value_date")
        reference_observed = (
            parse_date(str(reference_date)) if reference_date is not None else None
        )
        numeric = (
            float(record["market_price_cny"]),
            reference_value,
            float(record["source_discount_pct"]),
            float(record["premium_pct"]),
            float(record["change_pct"]),
            float(record["turnover_cny"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        quote_date <= as_of
        and updated_at.date() <= as_of
        and reference_type in {"iopv", "nav"}
        and (
            reference_type != "nav"
            or (
                reference_observed is not None
                and reference_observed <= as_of
                and str(record.get("reference_value_source_url") or "").startswith(
                    "https://"
                )
            )
        )
        and all(math.isfinite(value) for value in numeric)
        and numeric[0] > 0
        and numeric[1] > 0
        and numeric[5] >= 0
    )


def _load_exchange_premium_cache(
    path: Path, entries: list[dict[str, Any]], as_of: date
) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ETF_PREMIUM_CACHE_SCHEMA_VERSION
        or not isinstance(payload.get("records"), list)
    ):
        return {}
    entry_by_code = {entry["code"]: entry for entry in entries}
    cached: dict[str, dict[str, Any]] = {}
    for record in payload["records"]:
        code = str(record.get("code", "")) if isinstance(record, dict) else ""
        entry = entry_by_code.get(code)
        if entry and code not in cached and _cached_exchange_quote_valid(record, entry, as_of):
            normalized = {**entry, **record}
            normalized.setdefault("reference_value_type", "iopv")
            normalized.setdefault("reference_value_cny", normalized.get("iopv_cny"))
            normalized.setdefault("reference_value_date", None)
            normalized.setdefault("reference_value_source_url", ETF_QUOTE_PAGE_URL)
            cached[code] = normalized
    return cached


def _load_cached_qdii_exchange_premium_catalog(
    path: Path, metadata: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw_entries = payload.get("catalog") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, list):
        return None
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", ""))
        fund = metadata.get(code)
        if (
            not re.fullmatch(r"\d{6}", code)
            or code in seen
            or not is_qdii_fund_metadata(fund)
            or entry.get("category") != "qdii"
            or entry.get("market_id") not in {0, 1}
        ):
            continue
        seen.add(code)
        entries.append(dict(entry))
    if not entries:
        return None
    entries.sort(key=lambda item: item["code"])
    fingerprint = hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return entries, fingerprint


def exchange_premium_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    premium = record.get("premium_pct")
    return (
        premium is None,
        -float(premium) if premium is not None else math.inf,
        record["code"],
    )


def build_exchange_premium_snapshot(
    client: HttpClient,
    catalog_path: Path,
    cache_path: Path,
    as_of: date,
    *,
    catalog_entries: list[dict[str, Any]] | None = None,
    quote_rows: dict[str, dict[str, Any]] | None = None,
    catalog_fingerprint: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if catalog_entries is None:
        entries, loaded_fingerprint = load_exchange_premium_catalog(catalog_path)
        catalog_fingerprint = catalog_fingerprint or loaded_fingerprint
    else:
        entries = list(catalog_entries)
        catalog_fingerprint = catalog_fingerprint or hashlib.sha256(
            json.dumps(
                entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    cached = _load_exchange_premium_cache(cache_path, entries, as_of)
    dynamic_catalog = any(entry.get("category") == "qdii" for entry in entries)
    entry_by_code = {entry["code"]: entry for entry in entries}
    warnings: list[str] = []
    fresh: dict[str, dict[str, Any]] = {}
    quote_url = exchange_premium_quote_url(entries)
    try:
        if quote_rows is None:
            payload = client.get_json(quote_url, referer=ETF_QUOTE_PAGE_URL)
            rows = (
                ((payload.get("data") or {}).get("diff"))
                if isinstance(payload, dict)
                else None
            )
        else:
            rows = list(quote_rows.values())
        if not isinstance(rows, list):
            raise DataError("ETF quote response does not contain a record list")
        quote_references: dict[str, dict[str, Any]] = {}
        if dynamic_catalog:
            quote_references, reference_warnings = fetch_exchange_premium_lof_navs(
                client, entries, rows, as_of
            )
            warnings.extend(reference_warnings)
        invalid: list[str] = []
        seen_response: set[str] = set()
        for raw in rows:
            code = str(raw.get("f12", "")) if isinstance(raw, dict) else ""
            entry = entry_by_code.get(code)
            if entry is None:
                continue
            if code in seen_response:
                invalid.append(f"{code} duplicate response")
                fresh.pop(code, None)
                continue
            seen_response.add(code)
            if (
                any(
                    raw.get(field) in {None, "", "-"}
                    for field in ("f2", "f402", "f3", "f6")
                )
                and entry.get("category") == "qdii"
            ):
                continue
            try:
                fresh[code] = normalize_exchange_premium_quote(
                    raw, entry, as_of, quote_references.get(code)
                )
            except ValueError as exc:
                invalid.append(str(exc))
        missing = [entry["code"] for entry in entries if entry["code"] not in seen_response]
        if missing:
            invalid.append("missing " + ", ".join(missing))
        if invalid:
            warnings.append("场内溢价告警：部分行情未更新：" + "；".join(invalid))
    except (DataError, OSError, ValueError) as exc:
        warnings.append(f"场内溢价告警：行情刷新失败，使用缓存或空值：{exc}")

    records: list[dict[str, Any]] = []
    for entry in entries:
        code = entry["code"]
        if code in fresh:
            record = {**fresh[code], "quote_status": "fresh"}
        elif code in cached:
            record = {**entry, **cached[code], "quote_status": "stale"}
        else:
            record = {
                **entry,
                "market_price_cny": None,
                "iopv_cny": None,
                "reference_value_type": None,
                "reference_value_cny": None,
                "reference_value_date": None,
                "reference_value_source_url": None,
                "source_discount_pct": None,
                "premium_pct": None,
                "change_pct": None,
                "turnover_cny": None,
                "quote_date": None,
                "updated_at": None,
                "quote_source_url": None,
                "quote_status": "unavailable",
            }
        records.append(record)
    records.sort(key=exchange_premium_sort_key)
    discovered_count = len(records)
    if dynamic_catalog:
        records = [record for record in records if record["quote_status"] != "unavailable"]
    filtered_unavailable_count = discovered_count - len(records)
    fresh_count = sum(record["quote_status"] == "fresh" for record in records)
    stale_count = sum(record["quote_status"] == "stale" for record in records)

    if records and fresh_count == len(records):
        status = "fresh"
    elif fresh_count:
        status = "partial"
    elif stale_count:
        status = "stale"
    else:
        status = "unavailable"
    requested_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    cache_records = [record for record in records if record["premium_pct"] is not None]
    group_order = (
        sorted({str(entry["benchmark_group"]) for entry in entries})
        if dynamic_catalog
        else list(ETF_PREMIUM_GROUP_ORDER)
    )
    try:
        write_json(
            cache_path,
            {
                "schema_version": ETF_PREMIUM_CACHE_SCHEMA_VERSION,
                "catalog_fingerprint": catalog_fingerprint,
                "saved_at": requested_at,
                "catalog": entries,
                "records": cache_records,
            },
        )
    except OSError as exc:
        warnings.append(f"场内溢价告警：无法保存行情缓存：{exc}")
    return (
        {
            "schema_version": 1,
            "status": status,
            "requested_at": requested_at,
            "quote_delay_minutes": ETF_PREMIUM_DELAY_MINUTES,
            "discovered_count": discovered_count,
            "filtered_unavailable_count": filtered_unavailable_count,
            "expected_count": len(records),
            "fresh_count": fresh_count,
            "cache_hit_count": stale_count,
            "catalog_fingerprint": catalog_fingerprint,
            "group_order": group_order,
            "source_name": "东方财富场内基金行情",
            "source_url": ETF_QUOTE_PAGE_URL,
            "refresh_url": quote_url,
            "records": records,
        },
        warnings,
    )


def all_ranking_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *payload.get("records", []),
        *((payload.get("global_supplement") or {}).get("records") or []),
    ]


def write_csv(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "ranking_list",
        "routing_reason",
        "rank",
        "code",
        "name",
        "fund_type",
        "management_style",
        "product_structure_tags",
        "contract_benchmark_status",
        "contract_benchmark_name",
        "contract_benchmark_text",
        "contract_benchmark_weight_pct",
        "contract_benchmark_components",
        "contract_market_scope",
        "contract_market_label",
        "contract_asset_class",
        "contract_style_label",
        "contract_structure",
        "contract_prospectus_published_date",
        "contract_source_url",
        "product_summary_status",
        "product_summary_source_url",
        "holding_cost_status",
        "holding_cost_annualized_pct",
        "holding_cost_measurement_date",
        "holding_cost_source_url",
        "institution_holding_ratio_pct",
        "holder_report_date",
        "inception_date",
        "scale_billion_cny",
        "scale_report_date",
        "nav_history_start_date",
        "nav_history_end_date",
        "one_year_return_pct",
        "one_year_max_drawdown_pct",
        "one_year_performance_start_date",
        "one_year_performance_end_date",
        "three_year_return_pct",
        "three_year_max_drawdown_pct",
        "three_year_performance_start_date",
        "three_year_performance_end_date",
        "five_year_return_pct",
        "five_year_performance_start_date",
        "five_year_performance_end_date",
        "ten_year_return_pct",
        "ten_year_performance_start_date",
        "ten_year_performance_end_date",
        "nasdaq100_correlation",
        "nasdaq100_beta",
        "nasdaq100_tracking_error_pct",
        "nasdaq100_observations",
        "nasdaq100_start_date",
        "nasdaq100_end_date",
        "three_year_annualized_return_pct",
        "return_drawdown_ratio",
        "us_equity_confirmed_pct",
        "us_equity_possible_pct",
        "us_equity_status",
        "us_equity_report_date",
        "us_equity_source_url",
        "purchase_status",
        "direct_limit",
        "agency_limit",
        "share_class_rule",
        "channel_rule",
        "quota_confidence",
        "fund_page_url",
        "performance_source_url",
        "quota_source_urls",
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in all_ranking_records(payload):
            fit = item.get("nasdaq100_fit") or {}
            exposure = item.get("us_equity_exposure") or {}
            contract = item["contract_benchmark"]
            holding_cost = item["holding_cost"]
            writer.writerow(
                {
                    **{field: item.get(field) for field in fields},
                    "product_structure_tags": " | ".join(item["product_structure_tags"]),
                    "contract_benchmark_status": contract["status"],
                    "contract_benchmark_name": contract["benchmark_name"],
                    "contract_benchmark_text": contract["benchmark_text"],
                    "contract_benchmark_weight_pct": contract["benchmark_weight_pct"],
                    "contract_benchmark_components": json.dumps(
                        contract["components"], ensure_ascii=False, separators=(",", ":")
                    ),
                    "contract_market_scope": contract["market_scope"],
                    "contract_market_label": contract["market_label"],
                    "contract_asset_class": contract["asset_class"],
                    "contract_style_label": contract["style_label"],
                    "contract_structure": contract["structure"],
                    "contract_prospectus_published_date": contract[
                        "prospectus_published_date"
                    ],
                    "contract_source_url": contract["source_url"],
                    "product_summary_status": contract["product_summary_status"],
                    "product_summary_source_url": contract.get(
                        "product_summary_source_url"
                    ),
                    "holding_cost_status": holding_cost["status"],
                    "holding_cost_annualized_pct": holding_cost["annualized_pct"],
                    "holding_cost_measurement_date": holding_cost["measurement_date"],
                    "holding_cost_source_url": holding_cost["source_url"],
                    "nasdaq100_correlation": fit.get("correlation"),
                    "nasdaq100_beta": fit.get("beta"),
                    "nasdaq100_tracking_error_pct": fit.get("tracking_error_pct"),
                    "nasdaq100_observations": fit.get("observations"),
                    "nasdaq100_start_date": fit.get("start_date"),
                    "nasdaq100_end_date": fit.get("end_date"),
                    "us_equity_confirmed_pct": exposure.get("confirmed_pct"),
                    "us_equity_possible_pct": exposure.get("possible_pct"),
                    "us_equity_status": exposure.get("status"),
                    "us_equity_report_date": exposure.get("report_date"),
                    "us_equity_source_url": exposure.get("source_url"),
                    "direct_limit": format_limit(item["direct_limit"]),
                    "agency_limit": format_limit(item["agency_limit"]),
                    "quota_source_urls": " | ".join(item["quota_source_urls"]),
                }
            )
    temporary.replace(path)


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    records = payload["records"]
    global_records = payload["global_supplement"]["records"]
    combined = [*records, *global_records]
    scale_dates = "、".join(
        sorted({item["scale_report_date"] for item in combined})
    ) or "无"
    min_scale = payload["filters"]["min_scale_billion_cny"]
    scale_requirement = (
        "规模不限" if min_scale is None else f"规模 > {float(min_scale):g} 亿元"
    )
    lines = [
        "# QDII 美国主榜与全球补充榜",
        "",
        f"- 更新日期：{payload['run_date']}",
        f"- 机构持仓报告期：{payload['holder_report_date']}",
        f"- 规模报告期：{scale_dates}",
        f"- 近一年净值观察区间：{summarize_periods(combined, 'one_year')}",
        f"- 近三年净值观察区间：{summarize_periods(combined, 'three_year')}",
        f"- 申购额度评估日：{payload['run_date']}",
        f"- 筛选条件：{scale_requirement}；"
        f"成立超过 {payload['filters']['min_age_years']} 年；"
        f"近三年复权收益 >= {payload['filters']['min_three_year_return_pct']:g}%；"
        f"有完整五年历史时近五年收益 >= {payload['filters']['min_five_year_return_pct_if_available']:g}%；"
        f"有完整十年历史时近十年收益 >= {payload['filters']['min_ten_year_return_pct_if_available']:g}%；"
        f"直销额度 >= {payload['filters']['min_direct_limit_cny_inclusive']:,} 元；"
        "业绩基准仅展示、不参与筛选或分榜；"
        f"美国主榜名称排除 {'、'.join(payload['filters']['us_main_exclude_keywords']) or '无'}；"
        "全球补充榜名称地域不限；"
        "人民币 A 类或无 C/D 标记的人民币主份额；场外可申购",
        f"- 美国主榜：名称不含地域关键词且美股确认下限 >= {payload['filters']['min_us_equity_pct']:g}%；按纳指100相关性、Beta 接近 1、美股确认下限、机构持仓、近三年收益和基金代码排序",
        "- 全球补充榜：地域名称不限；名称命中美国主榜地域关键词或美股确认下限不足 50% 时进入；排除债券和商品；按三年年化收益回撤比、三年收益、较小回撤、机构持仓、规模和代码排序",
        f"- 全量筛选：基础候选 {payload['filters']['base_candidates_total']} 只；"
        f"业绩扫描 {payload['filters']['performance_candidates_scanned']} 只；"
        f"合同扫描 {payload['filters']['contract_candidates_scanned']} 只；"
        f"美国持仓扫描 {payload['filters']['us_equity_candidates_scanned']} 只；"
        f"美国额度扫描 {payload['filters']['us_quota_candidates_scanned']} 只；"
        f"全球额度扫描 {payload['filters']['global_quota_candidates_scanned']} 只",
        f"- 缓存：净值命中 {payload['cache']['performance']['hits']} 次；"
        f"基金穿透命中 {payload['cache']['fund_us_equity_exposures']['hits']} 次；"
        f"公告 PDF 命中 {payload['cache']['announcement_pdfs']['hits']} 次、"
        f"下载 {payload['cache']['announcement_pdfs']['downloads']} 次",
        "",
        "## 美国主榜",
        "",
        "| 排名 | 基金 | 分流原因 | 近三年 | 近五年 | 近十年 | 持有费率 | 纳指相关性 | Beta | 美股确认区间 | 直销额度 | 代销额度 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in payload["records"]:
        source = item["quota_source_urls"][-1] if item["quota_source_urls"] else item["fund_page_url"]
        rule = format_rule(item["share_class_rule"], item["channel_rule"])
        lines.append(
            f"| {item['rank']} | [{item['name']} {item['code']}]({source}) | "
            f"{routing_reason_label(item['routing_reason'])} | "
            f"{format_percentage(item['three_year_return_pct'], show_sign=True)} | "
            f"{format_optional_percentage(item['five_year_return_pct'], show_sign=True)} | "
            f"{format_optional_percentage(item['ten_year_return_pct'], show_sign=True)} | "
            f"{format_holding_cost(item['holding_cost'])} | "
            f"{format_correlation(item['nasdaq100_fit']['correlation'])} | "
            f"{format_beta(item['nasdaq100_fit']['beta'])} | "
            f"[{format_percentage(item['us_equity_exposure']['confirmed_pct'])}-"
            f"{format_percentage(item['us_equity_exposure']['possible_pct'])}]"
            f"({item['us_equity_exposure']['source_url']}) | "
            f"{format_limit(item['direct_limit'])} | {format_limit(item['agency_limit'])} |"
        )
        contract = item["contract_benchmark"]
        contract_source = contract.get("source_url") or item["fund_page_url"]
        lines.append(
            f"  - 合同基准：[{benchmark_display(contract)}]({contract_source})，"
            f"状态 {contract['status']}；产品标签：{' / '.join(item['product_structure_tags'])}；"
            f"额度计算：{rule}"
        )
    if not records:
        lines.append("| - | 暂无符合全部条件的基金 | - | - | - | - | - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 全球补充榜",
            "",
            "| 排名 | 基金 | 分流原因 | 合同基准 | 美股确认区间 | 近三年 | 近五年 | 近十年 | 持有费率 | 收益回撤比 | 直销额度 | 代销额度 |",
            "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in global_records:
        contract = item["contract_benchmark"]
        source = item["quota_source_urls"][-1] if item["quota_source_urls"] else item["fund_page_url"]
        contract_source = contract.get("source_url") or item["fund_page_url"]
        ratio = "∞" if item["return_drawdown_ratio"] is None else f"{item['return_drawdown_ratio']:.2f}"
        lines.append(
            f"| {item['rank']} | [{item['name']} {item['code']}]({source}) | "
            f"{routing_reason_label(item['routing_reason'])} | "
            f"[{benchmark_display(contract)}]({contract_source}) | "
            f"{format_percentage(item['us_equity_exposure']['confirmed_pct'])}-"
            f"{format_percentage(item['us_equity_exposure']['possible_pct'])} | "
            f"{format_percentage(item['three_year_return_pct'], show_sign=True)} | "
            f"{format_optional_percentage(item['five_year_return_pct'], show_sign=True)} | "
            f"{format_optional_percentage(item['ten_year_return_pct'], show_sign=True)} | "
            f"{format_holding_cost(item['holding_cost'])} | {ratio} | "
            f"{format_limit(item['direct_limit'])} | {format_limit(item['agency_limit'])} |"
        )
        lines.append(f"  - 产品标签：{' / '.join(item['product_structure_tags'])}")
    if not global_records:
        lines.append("| - | 暂无符合全部条件的基金 | - | - | - | - | - | - | - | - | - | - |")
    if payload["warnings"]:
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {warning}" for warning in payload["warnings"])
    lines.extend(
        [
            "",
            "额度为基金管理人层面的单日单基金账户上限；代销平台可能设置更低限制。",
            "持有费率为人民币产品概要披露的基金运作综合费率（年化），已反映在基金净值中，不从收益率重复扣减。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def html_source_link(label: str, source_url: str | None) -> str:
    escaped_label = html.escape(label)
    if not source_url:
        return f'<span class="source-value">{escaped_label}</span>'
    return (
        f'<a class="source-link" href="{html.escape(source_url, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">{escaped_label}'
        '<span class="external" aria-hidden="true">↗</span></a>'
    )


def write_html(path: Path, payload: dict[str, Any]) -> None:
    us_records = payload["records"]
    global_records = payload["global_supplement"]["records"]
    premium = payload["exchange_premium"]
    premium_records = premium["records"]
    dynamic_premium_catalog = (
        "discovered_count" in premium
        or any(item.get("category") == "qdii" for item in premium_records)
    )
    premium_product_label = "产品" if dynamic_premium_catalog else "ETF"
    premium_group_label = "类型" if dynamic_premium_catalog else "基准"
    combined = [*us_records, *global_records]
    filters = payload["filters"]
    scale_dates = "、".join(sorted({item["scale_report_date"] for item in combined})) or "无"
    min_scale = filters["min_scale_billion_cny"]
    scale_requirement = (
        "规模不限" if min_scale is None else f"规模 > {float(min_scale):g} 亿元"
    )
    filter_parts = (
        scale_requirement,
        f"成立 > {filters['min_age_years']} 年",
        f"三年收益 ≥ {filters['min_three_year_return_pct']:g}%",
        f"五年有数据 ≥ {filters['min_five_year_return_pct_if_available']:g}%",
        f"十年有数据 ≥ {filters['min_ten_year_return_pct_if_available']:g}%",
        f"直销 ≥ {filters['min_direct_limit_cny_inclusive']:,} 元",
        "业绩基准仅展示",
        f"美国榜排除 {' / '.join(filters['us_main_exclude_keywords'])}",
        "全球榜地域不限",
    )
    filter_html = "".join(
        f'<span class="filter-condition">{html.escape(part)}</span>'
        for part in filter_parts
    )

    def render_card(item: dict[str, Any]) -> str:
        is_us = item["ranking_list"] == "us_main"
        fit = item["nasdaq100_fit"]
        contract = item["contract_benchmark"]
        holding_cost = item["holding_cost"]
        exposure = item["us_equity_exposure"]
        direct_text = format_limit(item["direct_limit"])
        agency_text = format_limit(item["agency_limit"])
        direct_link = html_source_link(direct_text, item["direct_limit"].get("source_url"))
        agency_link = html_source_link(agency_text, item["agency_limit"].get("source_url"))
        tags = "".join(
            f'<span class="tag {"risk" if tag in {"杠杆", "反向", "波动率策略"} else ""}">{html.escape(tag)}</span>'
            for tag in item["product_structure_tags"]
        )
        if item["routing_reason"] == ROUTING_REASON_GEOGRAPHY_OVERRIDE:
            tags += '<span class="tag route">地域名称分流</span>'
        if is_us:
            primary_metrics = f"""
            <span class="summary-metric fit"><span class="metric-label">纳指相关 / β</span><span class="metric-value">{format_correlation(fit['correlation'])} · {format_beta(fit['beta'])}</span></span>
            <span class="summary-metric accent"><span class="metric-label">美股下限</span><span class="metric-value">{format_percentage(exposure['confirmed_pct'])}</span></span>"""
            list_details = f"""
            <div><dt>纳指相关性</dt><dd>{format_correlation(fit['correlation'])}</dd></div>
            <div><dt>Beta</dt><dd>{format_beta(fit['beta'])}</dd></div>
            <div><dt>跟踪误差</dt><dd>{format_percentage(fit['tracking_error_pct'])}</dd></div>
            <div><dt>相关样本</dt><dd>{fit['observations']} 周</dd></div>
            <div><dt>美股确认下限</dt><dd>{format_percentage(exposure['confirmed_pct'])}</dd></div>
            <div><dt>美股可能上限</dt><dd>{format_percentage(exposure['possible_pct'])}</dd></div>"""
            extra_sources = "".join(
                (
                    html_source_link(
                        f"定期报告 {exposure['report_date']}", exposure.get("source_url")
                    ),
                    html_source_link("Nasdaq XNDX", payload["benchmark"]["index_source_url"]),
                    html_source_link("美元兑人民币中间价", payload["benchmark"]["fx_source_url"]),
                )
            )
        else:
            ratio = "∞" if item["return_drawdown_ratio"] is None else f"{item['return_drawdown_ratio']:.2f}"
            primary_metrics = f"""
            <span class="summary-metric fit"><span class="metric-label">收益回撤比</span><span class="metric-value">{ratio}</span></span>
            <span class="summary-metric accent"><span class="metric-label">美股确认区间</span><span class="metric-value small">{format_percentage(exposure['confirmed_pct'])}-{format_percentage(exposure['possible_pct'])}</span></span>"""
            list_details = f"""
            <div><dt>三年年化收益</dt><dd class="positive-text">{format_percentage(item['three_year_annualized_return_pct'], show_sign=True)}</dd></div>
            <div><dt>收益回撤比</dt><dd>{ratio}</dd></div>
            <div><dt>美股确认下限</dt><dd>{format_percentage(exposure['confirmed_pct'])}</dd></div>
            <div><dt>美股可能上限</dt><dd>{format_percentage(exposure['possible_pct'])}</dd></div>"""
            extra_sources = html_source_link(
                f"定期报告 {exposure['report_date']}", exposure.get("source_url")
            )
        holding_cost_link = html_source_link(
            format_holding_cost(holding_cost), holding_cost.get("source_url")
        )
        prospectus_date = contract.get("prospectus_published_date") or "--"
        return f"""
      <details class="fund-item" data-code="{html.escape(item['code'], quote=True)}" data-list="{html.escape(item['ranking_list'], quote=True)}" data-routing-reason="{html.escape(item['routing_reason'], quote=True)}">
        <summary>
          <span class="rank" aria-label="排名 {item['rank']}">{item['rank']}</span>
          <span class="fund-identity">
            <span class="fund-name-row"><strong>{html.escape(item['name'])}</strong><span class="fund-code">{html.escape(item['code'])}</span></span>
            <span class="benchmark-label">{html.escape(benchmark_display(contract))}</span>
            <span class="tags">{tags}</span>
          </span>
          <span class="summary-metrics">
            {primary_metrics}
            <span class="summary-metric positive"><span class="metric-label">近三年</span><span class="metric-value">{format_percentage(item['three_year_return_pct'], show_sign=True)}</span></span>
            <span class="summary-metric"><span class="metric-label">持有费率</span><span class="metric-value quota">{format_holding_cost(holding_cost)}</span></span>
            <span class="summary-metric"><span class="metric-label">直销</span><span class="metric-value quota">{direct_text}</span></span>
          </span>
          <span class="chevron" aria-hidden="true"></span>
        </summary>
        <div class="fund-detail">
          <dl class="detail-grid">
            <div><dt>成立日</dt><dd>{html.escape(item['inception_date'])}</dd></div>
            <div><dt>机构持有</dt><dd>{format_percentage(item['institution_holding_ratio_pct'])}</dd></div>
            <div><dt>规模</dt><dd>{item['scale_billion_cny']:.2f} 亿元</dd></div>
            <div><dt>分流原因</dt><dd>{html.escape(routing_reason_label(item['routing_reason']))}</dd></div>
            <div><dt>近一年收益</dt><dd class="positive-text">{format_percentage(item['one_year_return_pct'], show_sign=True)}</dd></div>
            <div><dt>近一年回撤</dt><dd class="negative-text">{format_percentage(item['one_year_max_drawdown_pct'])}</dd></div>
            <div><dt>近三年收益</dt><dd class="positive-text">{format_percentage(item['three_year_return_pct'], show_sign=True)}</dd></div>
            <div><dt>近三年回撤</dt><dd class="negative-text">{format_percentage(item['three_year_max_drawdown_pct'])}</dd></div>
            <div><dt>近五年收益</dt><dd>{html.escape(format_long_return(item, 'five_year'))}</dd></div>
            <div><dt>近十年收益</dt><dd>{html.escape(format_long_return(item, 'ten_year'))}</dd></div>
            <div><dt>持有费率（年化）</dt><dd>{holding_cost_link}</dd></div>
            {list_details}
          </dl>
          <section class="benchmark-detail" aria-label="合同基准">
            <span>合同基准</span>
            <strong>{html.escape(contract['benchmark_text'])}</strong>
            <small>解析状态：{html.escape(contract['status'])}；仅作资料展示，不参与准入或分榜。</small>
          </section>
          <div class="quota-grid" aria-label="申购额度">
            <div><span>直销额度</span>{direct_link}</div>
            <div><span>代销额度</span>{agency_link}</div>
          </div>
          <dl class="rule-grid">
            <div><dt>额度计算</dt><dd>{html.escape(format_rule(item['share_class_rule'], item['channel_rule']))}</dd></div>
            <div><dt>完整净值历史</dt><dd>{html.escape(item['nav_history_start_date'])} 至 {html.escape(item['nav_history_end_date'])}</dd></div>
            <div><dt>招募说明书日期</dt><dd>{html.escape(prospectus_date)}</dd></div>
          </dl>
          <div class="source-row">
            {html_source_link('招募说明书', contract['source_url'])}
            {html_source_link('人民币产品概要', contract.get('product_summary_source_url'))}
            {html_source_link('基金主页', item['fund_page_url'])}
            {extra_sources}
          </div>
        </div>
      </details>"""

    def render_list(records: list[dict[str, Any]]) -> str:
        if not records:
            return '<p class="empty-state">暂无符合全部条件的基金</p>'
        return "".join(render_card(item) for item in records)

    def premium_band(value: float | None) -> tuple[str, str]:
        if value is None:
            return "unavailable", "--"
        if value < 0:
            return "discount", "折价"
        if value <= 2:
            return "normal", "0–2%"
        if value <= 5:
            return "elevated", "2–5%"
        return "high", ">5%高溢价"

    def format_premium_value(value: Any, digits: int = 2) -> str:
        if value is None:
            return "--"
        numeric = float(value)
        return f"{numeric:+.{digits}f}%"

    def format_turnover(value: Any) -> str:
        if value is None:
            return "--"
        numeric = float(value)
        if numeric >= 100_000_000:
            return f"{numeric / 100_000_000:.2f}亿元"
        if numeric >= 10_000:
            return f"{numeric / 10_000:.0f}万元"
        return f"{numeric:.0f}元"

    def format_quote_time(value: str | None) -> str:
        return "--" if not value else value[:16].replace("T", " ")

    def render_premium_row(item: dict[str, Any]) -> str:
        value = item["premium_pct"]
        reference_type = item["reference_value_type"]
        reference_value = item["reference_value_cny"]
        reference_label = (
            f"最新单位净值（{item['reference_value_date']}）"
            if reference_type == "nav"
            else "IOPV"
        )
        band_key, band_label = premium_band(value)
        quote_status = item["quote_status"]
        holding_cost = item["holding_cost"]
        holding_cost_text = format_holding_cost(holding_cost)
        holding_cost_status = holding_cost["status"]
        holding_cost_stale = (
            '<span class="stale-label">旧值</span>'
            if holding_cost_status == "stale"
            else ""
        )
        holding_cost_link = html_source_link(
            holding_cost_text, holding_cost.get("source_url")
        )
        holding_cost_date = holding_cost.get("source_published_date") or "--"
        row_classes = ["premium-row"]
        if quote_status == "stale":
            row_classes.append("is-stale")
        elif quote_status == "unavailable":
            row_classes.append("is-unavailable")
        stale_text = "旧值" if quote_status == "stale" else ("暂无行情" if quote_status == "unavailable" else "")
        source_url = item.get("quote_source_url") or item["source_url"]
        holding_cost_source = ""
        if holding_cost.get("source_url"):
            holding_cost_source = (
                f'<a class="premium-source-link" href="{html.escape(holding_cost["source_url"], quote=True)}" '
                'target="_blank" rel="noopener noreferrer">查看费率来源'
                '<span class="external" aria-hidden="true">↗</span></a>'
            )
        reference_source = ""
        if reference_type == "nav":
            reference_source = (
                f'<a class="premium-source-link" href="{html.escape(item["reference_value_source_url"], quote=True)}" '
                'target="_blank" rel="noopener noreferrer">查看净值来源'
                '<span class="external" aria-hidden="true">↗</span></a>'
            )
        premium_data = "NaN" if value is None else f"{float(value):.8g}"
        detail_id = f"premium-detail-{item['code']}"
        return f"""
          <tbody class="premium-item {' '.join(row_classes)}" data-etf-code="{item['code']}" data-premium="{premium_data}" data-quote-status="{quote_status}">
            <tr class="premium-summary-row">
              <td class="premium-name" data-label="{premium_product_label}"><button class="premium-row-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}"><span><strong data-field="name">{html.escape(item['name'])}</strong><span class="fund-code">{item['code']} · {item['exchange']}</span></span><span class="premium-chevron" aria-hidden="true"></span></button></td>
              <td data-label="{premium_group_label}"><span class="benchmark-compact">{html.escape(item['benchmark_group'])}</span></td>
              <td data-label="溢价"><span class="premium-value band-{band_key}" data-field="premium">{format_premium_value(value)}</span><span class="premium-band band-{band_key}" data-field="band">{band_label}</span><span class="stale-label" data-field="stale">{stale_text}</span></td>
              <td data-label="综合费率"><span class="premium-cost" data-field="holding-cost">{holding_cost_text}</span>{holding_cost_stale}</td>
              <td data-label="涨跌"><span data-field="change">{format_premium_value(item['change_pct'])}</span></td>
            </tr>
            <tr class="premium-detail-row" id="{detail_id}" hidden>
              <td colspan="5">
                <dl class="premium-detail-grid">
                  <div><dt>价格</dt><dd data-field="price">{'--' if item['market_price_cny'] is None else f"{float(item['market_price_cny']):.3f}"}</dd></div>
                  <div><dt data-field="reference-label">{html.escape(reference_label)}</dt><dd data-field="reference-value">{float(reference_value):.4f}</dd></div>
                  <div><dt>成交额</dt><dd data-field="turnover">{format_turnover(item['turnover_cny'])}</dd></div>
                  <div><dt>行情时间</dt><dd><time data-field="updated">{format_quote_time(item['updated_at'])}</time></dd></div>
                  <div><dt>交易所</dt><dd>{item['exchange']}</dd></div>
                  <div><dt>分类</dt><dd>{'QDII' if item['category'] == 'qdii' else ('行业主题' if item['category'] == 'sector_theme' else '宽基')}</dd></div>
                  <div><dt>综合费率（年化）</dt><dd>{holding_cost_link}{holding_cost_stale}</dd></div>
                  <div><dt>费率资料日期</dt><dd>{html.escape(holding_cost_date)}</dd></div>
                </dl>
                <div class="premium-source-row">
                  <a class="premium-source-link" href="{html.escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">查看行情来源<span class="external" aria-hidden="true">↗</span></a>
                  {reference_source}
                  {holding_cost_source}
                </div>
              </td>
            </tr>
          </tbody>"""

    premium_rows = "".join(render_premium_row(item) for item in premium_records)
    premium_status_labels = {
        "fresh": "日报快照完整",
        "partial": "部分ETF使用旧值",
        "stale": "当前显示缓存行情",
        "unavailable": "日报快照暂不可用",
    }
    premium_status_text = premium_status_labels.get(premium["status"], "行情状态未知")
    premium_config = {
        "refreshUrl": premium["refresh_url"],
        "refreshMode": "paged" if any(item.get("category") == "qdii" for item in premium_records) else "single",
        "refreshPageSize": ETF_MARKET_LIST_PAGE_SIZE,
        "entries": [
            {
                "code": item["code"],
                "name": item["name"],
                "benchmarkGroup": item["benchmark_group"],
                "category": item.get("category"),
                "referenceType": item.get("reference_value_type"),
                "referenceValueCny": item.get("reference_value_cny"),
                "referenceDate": item.get("reference_value_date"),
            }
            for item in premium_records
        ],
    }
    premium_config_json = json.dumps(
        premium_config, ensure_ascii=False, separators=(",", ":")
    ).replace("</", "<\\/")
    browser_script = (Path(__file__).with_name("premium_refresh.js")).read_text(
        encoding="utf-8"
    )

    warning_section = ""
    if payload["warnings"]:
        warning_items = "".join(
            f"<li>{html.escape(warning)}</li>" for warning in payload["warnings"]
        )
        warning_section = f"""
      <details class="warnings">
        <summary>数据警告 <span>{len(payload['warnings'])}</span></summary>
        <ul>{warning_items}</ul>
      </details>"""

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>QDII 榜单与场内溢价 · {html.escape(payload['run_date'])}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f3f5f7; --surface:#fff; --text:#18222c; --muted:#66727e; --border:#d7dde3; --accent:#086b58; --accent-soft:#e8f3ef; --blue:#195c9b; --blue-soft:#edf4fa; --positive:#147a4b; --negative:#b42318; --warning:#8a4b08; }}
    * {{ box-sizing:border-box; }}
    html {{ background:var(--bg); }}
    body {{ margin:0; min-width:280px; color:var(--text); background:var(--bg); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; font-size:15px; line-height:1.5; letter-spacing:0; overflow-wrap:anywhere; }}
    button {{ font:inherit; letter-spacing:0; }}
    a {{ color:var(--blue); text-decoration-thickness:1px; text-underline-offset:3px; }}
    .page {{ width:min(100%,980px); margin:0 auto; padding:max(16px,env(safe-area-inset-top)) max(14px,env(safe-area-inset-right)) max(28px,env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-left)); }}
    .page-header {{ padding:4px 2px 16px; }}
    .title-row {{ display:flex; align-items:baseline; justify-content:space-between; gap:16px; }}
    h1 {{ margin:0; font-size:22px; line-height:1.25; letter-spacing:0; }}
    .run-date,.metric-label,dt,.quota-grid span,.benchmark-detail>span {{ color:var(--muted); font-size:12px; }}
    .run-date {{ white-space:nowrap; }}
    .filter-line {{ display:flex; flex-wrap:wrap; gap:2px 8px; margin:10px 0 0; color:#35414d; font-size:14px; }}
    .filter-condition {{ white-space:nowrap; }}
    .filter-condition:not(:last-child)::after {{ content:" ·"; color:var(--muted); }}
    .meta-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 14px; margin:14px 0 0; }}
    .meta-grid div, .detail-grid div, .rule-grid div {{ min-width:0; }}
    dd {{ margin:2px 0 0; font-weight:650; }}
    .tabs {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:4px; margin:0 0 12px; padding:4px; border:1px solid var(--border); border-radius:6px; background:#e9edf1; }}
    .tab {{ display:grid; min-height:42px; place-content:center; border:0; border-radius:4px; color:#42505d; background:transparent; cursor:pointer; font-weight:700; text-align:center; text-decoration:none; }}
    .tab[aria-selected="true"] {{ color:var(--text); background:var(--surface); box-shadow:0 1px 2px rgba(20,32,44,.12); }}
    .tab-link {{ color:#284c69; }}
    .tab-count {{ margin-left:6px; color:var(--muted); font-variant-numeric:tabular-nums; }}
    .tab:focus-visible,.fund-item summary:focus-visible,.refresh-button:focus-visible {{ outline:3px solid #86b7e8; outline-offset:2px; }}
    .ranking-list {{ display:grid; gap:10px; }}
    .fund-item {{ border:1px solid var(--border); border-radius:6px; background:var(--surface); overflow:clip; }}
    .fund-item summary {{ display:grid; grid-template-columns:34px minmax(0,1fr) 18px; align-items:center; gap:0 10px; min-height:72px; padding:12px; cursor:pointer; list-style:none; -webkit-tap-highlight-color:transparent; }}
    .fund-item summary::-webkit-details-marker {{ display:none; }}
    .rank {{ display:grid; width:32px; height:32px; place-items:center; border-radius:4px; color:#fff; background:#263746; font-weight:750; font-variant-numeric:tabular-nums; }}
    .fund-identity {{ min-width:0; display:grid; gap:5px; }}
    .fund-name-row {{ display:flex; min-width:0; flex-wrap:wrap; align-items:baseline; gap:2px 8px; }}
    .fund-name-row strong {{ min-width:0; font-size:16px; line-height:1.35; }}
    .fund-code,.benchmark-label {{ color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; }}
    .tags {{ display:flex; flex-wrap:wrap; gap:4px; }}
    .tag {{ padding:1px 5px; border:1px solid #cbd3db; border-radius:3px; color:#4c5a67; background:#f8fafb; font-size:11px; }}
    .tag.risk {{ border-color:#e5a5a0; color:var(--negative); background:#fff5f4; }}
    .tag.route {{ border-color:#d6a04a; color:#755015; background:#fff8e8; }}
    .chevron {{ width:9px; height:9px; border-right:2px solid #7a8793; border-bottom:2px solid #7a8793; transform:rotate(45deg) translate(-2px,2px); transition:transform 150ms ease; }}
    .fund-item[open] .chevron {{ transform:rotate(225deg) translate(-1px,-1px); }}
    .summary-metrics {{ grid-column:2/-1; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-top:12px; }}
    .summary-metric {{ display:flex; min-width:0; min-height:46px; flex-direction:column; justify-content:center; padding:6px 8px; border-left:3px solid #c7cfd7; background:#f7f8fa; }}
    .summary-metric.fit {{ border-color:var(--blue); background:var(--blue-soft); }}
    .summary-metric.accent {{ border-color:var(--accent); background:var(--accent-soft); }}
    .summary-metric.positive {{ border-color:var(--positive); }}
    .metric-value {{ font-weight:750; font-variant-numeric:tabular-nums; white-space:nowrap; }}
    .metric-value.small,.metric-value.quota {{ font-size:13px; }}
    .fund-detail {{ border-top:1px solid var(--border); padding:14px 12px 16px; }}
    .detail-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px 16px; margin:0; }}
    .positive-text {{ color:var(--positive); }} .negative-text {{ color:var(--negative); }}
    .benchmark-detail {{ display:grid; gap:3px; margin-top:16px; padding:12px 0; border-top:1px solid var(--border); }}
    .benchmark-detail strong {{ font-size:13px; font-weight:650; }}
    .quota-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; padding:12px 0; border-top:1px solid var(--border); border-bottom:1px solid var(--border); }}
    .quota-grid div {{ display:flex; min-width:0; flex-direction:column; gap:3px; }}
    .source-link,.source-value {{ font-weight:700; }} .external {{ margin-left:4px; font-size:11px; }}
    .rule-grid {{ display:grid; gap:10px; margin:14px 0 0; }}
    .source-row {{ display:flex; flex-wrap:wrap; gap:10px 18px; margin-top:14px; }}
    .empty-state {{ margin:0; padding:28px 4px; color:var(--muted); text-align:center; border-top:1px solid var(--border); border-bottom:1px solid var(--border); }}
    .premium-panel {{ display:grid; gap:14px; }}
    .premium-toolbar {{ display:flex; align-items:flex-start; justify-content:space-between; gap:16px; padding:4px 2px 0; }}
    .premium-toolbar h2 {{ margin:0; font-size:17px; }}
    .premium-toolbar p {{ margin:3px 0 0; color:var(--muted); font-size:12px; }}
    .refresh-button {{ display:inline-flex; min-width:116px; min-height:40px; align-items:center; justify-content:center; gap:7px; padding:7px 12px; border:1px solid #176451; border-radius:5px; color:#fff; background:#176451; cursor:pointer; font-weight:700; white-space:nowrap; }}
    .refresh-button:disabled {{ cursor:wait; opacity:.65; }}
    .refresh-icon {{ font-size:18px; line-height:1; }}
    .refresh-button[aria-busy="true"] .refresh-icon {{ animation:spin 900ms linear infinite; }}
    .premium-status {{ min-height:22px; margin:0; padding:7px 10px; border-left:3px solid var(--blue); color:#42505d; background:var(--blue-soft); font-size:12px; }}
    .premium-table-wrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:6px; background:var(--surface); }}
    .premium-table {{ width:100%; min-width:760px; border-collapse:collapse; table-layout:fixed; font-variant-numeric:tabular-nums; }}
    .premium-table th,.premium-summary-row td {{ padding:8px 10px; border-bottom:1px solid #e5e9ed; text-align:right; vertical-align:middle; white-space:nowrap; }}
    .premium-table th {{ color:#52606d; background:#f7f8fa; font-size:12px; font-weight:700; }}
    .premium-table th:first-child,.premium-summary-row td:first-child {{ width:34%; text-align:left; }}
    .premium-table th:nth-child(2),.premium-summary-row td:nth-child(2) {{ width:14%; }}
    .premium-table th:nth-child(3),.premium-summary-row td:nth-child(3) {{ width:22%; }}
    .premium-table th:nth-child(4),.premium-summary-row td:nth-child(4) {{ width:18%; }}
    .premium-table th:last-child,.premium-summary-row td:last-child {{ width:12%; }}
    .premium-row-toggle {{ display:flex; width:100%; min-height:38px; align-items:center; justify-content:space-between; gap:12px; padding:0; border:0; color:var(--text); background:transparent; cursor:pointer; text-align:left; }}
    .premium-row-toggle>span:first-child {{ display:grid; min-width:0; gap:1px; }}
    .premium-row-toggle strong {{ overflow:hidden; text-overflow:ellipsis; }}
    .premium-chevron {{ width:8px; height:8px; flex:0 0 auto; border-right:2px solid #7a8793; border-bottom:2px solid #7a8793; transform:rotate(45deg); transition:transform .16s ease; }}
    .premium-row-toggle[aria-expanded="true"] .premium-chevron {{ transform:rotate(225deg); }}
    .premium-row-toggle:focus-visible {{ outline:3px solid #86b7e8; outline-offset:3px; }}
    .benchmark-compact {{ color:#4f5d69; font-size:13px; }}
    .premium-detail-row td {{ padding:0; border-bottom:1px solid #d9e0e6; background:#f8fafb; }}
    .premium-detail-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:0; padding:12px 14px 8px; }}
    .premium-detail-grid dd {{ font-size:13px; }}
    .premium-source-row {{ display:flex; flex-wrap:wrap; gap:10px 18px; margin:0 14px 12px; }}
    .premium-source-link {{ display:inline-block; font-size:12px; font-weight:700; }}
    .premium-cost {{ font-weight:750; }}
    .premium-value {{ margin-right:6px; font-weight:800; }}
    .premium-band,.stale-label {{ display:inline-block; padding:1px 4px; border-radius:3px; font-size:10px; line-height:1.5; }}
    .premium-band.band-discount {{ color:#176451; background:#e8f3ef; }}
    .premium-band.band-normal {{ color:#40505d; background:#edf0f2; }}
    .premium-band.band-elevated {{ color:#7a4b08; background:#fff4da; }}
    .premium-band.band-high {{ color:var(--negative); background:#fff0ee; }}
    .premium-value.band-discount {{ color:#176451; }}
    .premium-value.band-elevated {{ color:#8a4b08; }}
    .premium-value.band-high {{ color:var(--negative); }}
    .stale-label {{ margin-left:5px; color:#755015; background:#fff4da; }}
    .premium-item.is-unavailable {{ color:var(--muted); }}
    .warnings {{ margin-top:18px; border-top:1px solid var(--border); }}
    .warnings summary {{ display:flex; min-height:48px; align-items:center; justify-content:space-between; color:var(--warning); cursor:pointer; font-weight:700; }}
    .warnings ul {{ margin:0; padding:0 0 0 22px; color:#4c5661; }} .warnings li {{ margin:0 0 9px; }}
    footer {{ margin-top:18px; color:var(--muted); font-size:12px; }}
    [hidden] {{ display:none !important; }}
    @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
    @media (max-width:700px) {{
      .title-row {{ gap:8px; }} h1 {{ font-size:20px; }}
      .tab {{ min-height:50px; padding:5px 2px; font-size:13px; }} .tab-count {{ display:block; margin-left:0; font-size:11px; }}
      .premium-toolbar {{ align-items:stretch; flex-direction:column; gap:9px; }} .refresh-button {{ align-self:flex-start; }}
      .premium-table-wrap {{ overflow:visible; border:0; background:transparent; }} .premium-table {{ min-width:0; table-layout:auto; }}
      .premium-table thead {{ display:none; }} .premium-table,.premium-item {{ display:block; }}
      .premium-item {{ margin-bottom:7px; border:1px solid var(--border); border-radius:6px; background:var(--surface); overflow:hidden; }}
      .premium-summary-row {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 14px; padding:9px 11px; }}
      .premium-summary-row td,.premium-summary-row td:first-child,.premium-summary-row td:nth-child(2),.premium-summary-row td:nth-child(3),.premium-summary-row td:nth-child(4),.premium-summary-row td:last-child {{ display:grid; width:auto; min-width:0; padding:0; border:0; text-align:left; white-space:normal; }}
      .premium-summary-row td::before {{ content:attr(data-label); color:var(--muted); font-size:10px; }}
      .premium-summary-row td:first-child {{ grid-column:1/-1; padding-bottom:6px; border-bottom:1px solid #e8ebee; }} .premium-summary-row td:first-child::before {{ display:none; }}
      .premium-detail-row {{ display:table-row; }} .premium-detail-row[hidden] {{ display:none; }} .premium-detail-row td {{ display:block; padding:0; border:0; }}
      .premium-detail-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px 16px; padding:11px 12px 8px; border-top:1px solid #e5e9ed; }} .premium-source-row {{ margin:0 12px 11px; }}
    }}
    @media (min-width:820px) {{ .page {{ padding-top:28px; }} .meta-grid {{ grid-template-columns:repeat(4,minmax(0,1fr)); }} .fund-item summary {{ grid-template-columns:38px minmax(210px,1fr) minmax(520px,560px) 18px; gap:12px; padding:14px 16px; }} .summary-metrics {{ grid-column:3; grid-row:1; grid-template-columns:repeat(5,minmax(0,1fr)); margin-top:0; }} .chevron {{ grid-column:4; }} .fund-detail {{ padding:18px 66px 20px; }} .detail-grid {{ grid-template-columns:repeat(4,minmax(0,1fr)); }} .rule-grid {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} }}
    @media (prefers-reduced-motion:reduce) {{ .chevron {{ transition:none; }} .refresh-button[aria-busy="true"] .refresh-icon {{ animation:none; }} }}
  </style>
</head>
<body>
  <div class="page">
    <header class="page-header">
      <div class="title-row"><h1>QDII 榜单与场内溢价</h1><time class="run-date" datetime="{html.escape(payload['run_date'], quote=True)}">{html.escape(payload['run_date'])}</time></div>
      <p class="filter-line">{filter_html}</p>
      <dl class="meta-grid">
        <div><dt>机构持仓报告期</dt><dd>{html.escape(payload['holder_report_date'])}</dd></div>
        <div><dt>规模报告期</dt><dd>{html.escape(scale_dates)}</dd></div>
        <div><dt>净值区间</dt><dd>{html.escape(summarize_periods(combined, 'three_year'))}</dd></div>
        <div><dt>纳指基准更新</dt><dd>XNDX {html.escape(payload['benchmark']['index_latest_date'])} · 汇率 {html.escape(payload['benchmark']['fx_latest_date'])}</dd></div>
        <div><dt>基础候选</dt><dd>{filters['base_candidates_total']} 只</dd></div>
        <div><dt>合同扫描</dt><dd>{filters['contract_candidates_scanned']} 只</dd></div>
        <div><dt>美国 / 全球入榜</dt><dd>{len(us_records)} / {len(global_records)} 只</dd></div>
        <div><dt>数据警告</dt><dd>{len(payload['warnings'])} 项</dd></div>
      </dl>
    </header>
    <main>
      <div class="tabs" role="tablist" aria-label="榜单切换">
        <button class="tab" id="tab-us" type="button" role="tab" aria-selected="true" aria-controls="panel-us" data-panel="panel-us">美国主榜<span class="tab-count">{len(us_records)}</span></button>
        <button class="tab" id="tab-global" type="button" role="tab" aria-selected="false" aria-controls="panel-global" data-panel="panel-global">全球补充榜<span class="tab-count">{len(global_records)}</span></button>
        <button class="tab" id="tab-premium" type="button" role="tab" aria-selected="false" aria-controls="panel-premium" data-panel="panel-premium">场内溢价<span class="tab-count">{len(premium_records)}</span></button>
        <a class="tab tab-link" id="tab-valuation" href="valuation/" role="tab" aria-selected="false">估值代理<span class="tab-count">研究版</span></a>
      </div>
      <section id="panel-us" class="ranking-list" role="tabpanel" aria-labelledby="tab-us">{render_list(us_records)}</section>
      <section id="panel-global" class="ranking-list" role="tabpanel" aria-labelledby="tab-global" hidden>{render_list(global_records)}</section>
      <section id="panel-premium" class="premium-panel" role="tabpanel" aria-labelledby="tab-premium" hidden>
        <div class="premium-toolbar">
          <div><h2>场内 QDII</h2><p>按溢价从高到低排列；点击产品展开行情、综合费率和来源详情。</p></div>
          <button class="refresh-button" id="premium-refresh" type="button" aria-busy="false"><span class="refresh-icon" aria-hidden="true">↻</span><span>刷新行情</span></button>
        </div>
        <p class="premium-status" id="premium-refresh-status" role="status" aria-live="polite">{html.escape(premium_status_text)}；日报请求于 {html.escape(format_quote_time(premium['requested_at']))}，行情约延迟 {premium['quote_delay_minutes']} 分钟。</p>
        <div class="premium-table-wrap">
          <table class="premium-table">
            <thead><tr><th>{premium_product_label}</th><th>{premium_group_label}</th><th>溢价</th><th>综合费率</th><th>涨跌</th></tr></thead>
            {premium_rows}
          </table>
        </div>
      </section>
      {warning_section}
    </main>
    <footer>额度为基金管理人层面的单日单基金账户上限；综合费率已从基金资产中扣除，不含场内券商佣金；场内溢价按约15分钟延迟价格相对 ETF 的 IOPV 或 LOF 的最新单位净值计算。</footer>
  </div>
  <script>
    const tabs = Array.from(document.querySelectorAll('[role="tab"][data-panel]'));
    const selectTab = (tab) => {{
      tabs.forEach((item) => {{
        const active = item === tab;
        item.setAttribute('aria-selected', String(active));
        document.getElementById(item.dataset.panel).hidden = !active;
      }});
    }};
    tabs.forEach((tab) => tab.addEventListener('click', () => selectTab(tab)));
    const requestedTab = new URLSearchParams(window.location.search).get('tab');
    const initialTab = tabs.find((tab) => tab.id === `tab-${{requestedTab}}`);
    if (initialTab) selectTab(initialTab);
    globalThis.__ETF_PREMIUM_CONFIG__ = {premium_config_json};
{browser_script}
  </script>
</body>
</html>
"""
    document = "\n".join(line.rstrip() for line in document.splitlines()) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(path)


def product_structure_tags(profile: dict[str, Any], fund_type: str) -> list[str]:
    labels = ["被动" if profile["management_style"] == "passive" else "主动"]
    type_label = {
        "QDII-REITs": "REIT",
        "QDII-FOF": "FOF",
        "指数型-海外股票": "指数",
    }.get(fund_type, "股票/混合")
    labels.append(type_label)
    if profile["style_label"] not in {"未识别", "复合风格"}:
        labels.append(str(profile["style_label"]))
    elif profile["status"] == "composite":
        labels.append("复合风格")
    special = {
        "leveraged": "杠杆",
        "inverse": "反向",
        "volatility": "波动率策略",
    }.get(str(profile["structure"]))
    if special and special not in labels:
        labels.append(special)
    return labels


def build_output_record(
    fund: dict[str, Any], rank: int, ranking_list: str, holder_report_date: str
) -> dict[str, Any]:
    record = {
        "rank": rank,
        "ranking_list": ranking_list,
        "routing_reason": fund["routing_reason"],
        "code": fund["code"],
        "name": fund["name"],
        "fund_type": fund["fund_type"],
        "management_style": fund["contract_benchmark"]["management_style"],
        "product_structure_tags": product_structure_tags(
            fund["contract_benchmark"], fund["fund_type"]
        ),
        "contract_benchmark": fund["contract_benchmark"],
        "holding_cost": fund["holding_cost"],
        "institution_holding_ratio_pct": fund["institution_holding_ratio_pct"],
        "holder_report_date": holder_report_date,
        "inception_date": fund["inception_date"],
        "scale_billion_cny": fund["scale_billion_cny"],
        "scale_report_date": fund["scale_report_date"],
        "purchase_status": fund["purchase_status"],
        "purchase_status_text": fund["purchase_status_text"],
        "fund_page_url": fund["fund_page_url"],
        "performance_source_url": fund["performance_source_url"],
        "nav_history_start_date": fund["nav_history_start_date"],
        "nav_history_end_date": fund["nav_history_end_date"],
        "one_year_return_pct": fund["one_year_return_pct"],
        "one_year_max_drawdown_pct": fund["one_year_max_drawdown_pct"],
        "one_year_performance_start_date": fund["one_year_performance_start_date"],
        "one_year_performance_end_date": fund["one_year_performance_end_date"],
        "three_year_return_pct": fund["three_year_return_pct"],
        "three_year_max_drawdown_pct": fund["three_year_max_drawdown_pct"],
        "three_year_performance_start_date": fund["three_year_performance_start_date"],
        "three_year_performance_end_date": fund["three_year_performance_end_date"],
        "five_year_return_pct": fund["five_year_return_pct"],
        "five_year_performance_start_date": fund["five_year_performance_start_date"],
        "five_year_performance_end_date": fund["five_year_performance_end_date"],
        "ten_year_return_pct": fund["ten_year_return_pct"],
        "ten_year_performance_start_date": fund["ten_year_performance_start_date"],
        "ten_year_performance_end_date": fund["ten_year_performance_end_date"],
        "nasdaq100_fit": fund["nasdaq100_fit"],
        "us_equity_exposure": fund["us_equity_exposure"],
        **{key: fund[key] for key in (
            "quota_status",
            "quota_confidence",
            "direct_limit",
            "agency_limit",
            "share_class_rule",
            "channel_rule",
            "quota_source_urls",
        )},
    }
    if ranking_list == "global_supplement":
        record["three_year_annualized_return_pct"] = round(
            float(fund["_three_year_annualized_return_pct"]), 2
        )
        score = fund.get("_return_drawdown_ratio")
        record["return_drawdown_ratio"] = None if score is None else round(float(score), 4)
    return record


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
        if performance_qualified:
            with ThreadPoolExecutor(
                max_workers=min(DOCUMENT_WORKERS, len(performance_qualified))
            ) as executor:
                futures = {
                    executor.submit(evaluate_documents, fund): index
                    for index, fund in enumerate(performance_qualified)
                }
                for future in as_completed(futures):
                    document_results[futures[future]] = future.result()

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

    us_routed_candidates: list[dict[str, Any]] = []
    global_routed_candidates: list[dict[str, Any]] = []
    for fund in classified_candidates:
        result = fund["_document_result"]
        exposure = result["exposure"]
        exposure_warnings = result["exposure_warnings"]
        warnings.extend(f"{fund['code']} {warning}" for warning in exposure_warnings)
        ranking_list, routing_reason = ranking_route(
            fund["name"],
            exposure,
            args.min_us_equity_pct,
            args.us_main_exclude_keywords,
        )
        routed = {
            **fund,
            "us_equity_exposure": exposure,
            "routing_reason": routing_reason,
        }
        if ranking_list == "us_main":
            if not isinstance(fund.get("nasdaq100_fit"), dict):
                detail = fund.get("nasdaq100_fit_error") or "unknown calculation error"
                raise DataError(
                    f"Nasdaq-100 fit is unavailable for US-main fund {fund['code']}: {detail}"
                )
            us_routed_candidates.append(routed)
        else:
            global_routed_candidates.append(routed)

    def apply_quota_gate(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        qualified: list[dict[str, Any]] = []
        for fund in pool:
            result = fund["_document_result"]
            quota = result["quota"]
            quota_warnings = result["quota_warnings"]
            quota_error = result["quota_error"]
            if quota_error is not None or quota is None:
                warnings.append(
                    f"额度剔除 {fund['code']}：{quota_error or '额度结果缺失'}"
                )
                record_exclusion(
                    "quota_unresolved", "申购额度无法可靠解析", fund["code"]
                )
                continue
            warnings.extend(quota_warnings)
            if any("quota notice could not be parsed" in warning for warning in quota_warnings):
                warnings.append(f"额度剔除 {fund['code']}：存在无法解析的有效期内额度公告。")
                record_exclusion(
                    "quota_unresolved", "申购额度无法可靠解析", fund["code"]
                )
                continue
            direct = quota["direct_limit"]
            if direct.get("status") == "unknown":
                warnings.append(f"额度剔除 {fund['code']}：直销额度无法可靠解析。")
                record_exclusion(
                    "quota_unresolved", "申购额度无法可靠解析", fund["code"]
                )
                continue
            if not direct_limit_qualifies(direct, args.min_direct_limit_cny):
                record_exclusion(
                    "direct_limit_below_threshold",
                    f"直销额度低于 {args.min_direct_limit_cny:,} 元",
                    fund["code"],
                )
                continue
            qualified.append({**fund, **quota})
        return qualified

    us_quota_qualified = apply_quota_gate(us_routed_candidates)
    global_quota_qualified = apply_quota_gate(global_routed_candidates)
    for fund in global_quota_qualified:
        score, annualized = calculate_return_drawdown_ratio(fund)
        fund["_return_drawdown_ratio"] = score
        fund["_three_year_annualized_return_pct"] = annualized

    us_ranked = sorted(us_quota_qualified, key=us_main_sort_key)[: args.top]
    global_ranked = sorted(
        global_quota_qualified, key=global_supplement_sort_key
    )[: args.top]
    for fund in us_quota_qualified:
        if fund not in us_ranked:
            record_exclusion(
                "ranking_cap", f"超过每榜前 {args.top} 只上限", fund["code"]
            )
    for fund in global_quota_qualified:
        if fund not in global_ranked:
            record_exclusion(
                "ranking_cap", f"超过每榜前 {args.top} 只上限", fund["code"]
            )
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
        "schema_version": 12,
        "run_date": as_of.isoformat(),
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "holder_report_date": selected.report_date,
        "holder_period_fund_count": selected.fund_count,
        "filters": {
            "top": args.top,
            "min_scale_billion_cny": args.min_scale,
            "min_age_years": args.min_age_years,
            "min_three_year_return_pct": args.min_three_year_return_pct,
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


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=10, help="Maximum result count")
    parser.add_argument(
        "--min-scale",
        type=float,
        default=None,
        help="Optional strict minimum scale in CNY 100m; omitted by default",
    )
    parser.add_argument(
        "--min-age-years",
        type=int,
        default=3,
        help="Require inception strictly earlier than this many years before --as-of",
    )
    parser.add_argument(
        "--min-three-year-return-pct",
        type=float,
        default=DEFAULT_MIN_THREE_YEAR_RETURN_PCT,
        help="Minimum trailing three-year adjusted return percentage",
    )
    parser.add_argument(
        "--min-five-year-return-pct",
        type=float,
        default=DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
        help="Minimum five-year adjusted return when a complete five-year window exists",
    )
    parser.add_argument(
        "--min-ten-year-return-pct",
        type=float,
        default=DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
        help="Minimum ten-year adjusted return when a complete ten-year window exists",
    )
    parser.add_argument(
        "--min-us-equity-pct",
        type=float,
        default=50.0,
        help="Minimum confirmed US-equity exposure percentage",
    )
    parser.add_argument(
        "--min-direct-limit-cny",
        type=int,
        default=DEFAULT_MIN_DIRECT_LIMIT_CNY,
        help="Inclusive minimum manager direct-sale daily limit in CNY",
    )
    parser.add_argument(
        "--us-main-exclude-keywords",
        "--exclude-keywords",
        dest="us_main_exclude_keywords",
        nargs="*",
        default=DEFAULT_EXCLUDE_KEYWORDS,
        help="Fund-name keywords routed away from the US main list",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / "output" / "qdii-ranking",
        help="Output directory",
    )
    parser.add_argument(
        "--publish-dir",
        type=Path,
        default=Path.cwd() / "public",
        help="Static-site directory that receives index.html",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="Persistent cache directory (defaults to <output-dir>/cache)",
    )
    parser.add_argument(
        "--us-equity-catalog",
        type=Path,
        default=DEFAULT_US_EQUITY_CATALOG,
        help="Underlying instrument classification catalog",
    )
    parser.add_argument(
        "--contract-benchmark-catalog",
        type=Path,
        default=DEFAULT_CONTRACT_BENCHMARK_CATALOG,
        help="Contract benchmark classification catalog",
    )
    parser.add_argument(
        "--us-equity-etf-catalog",
        type=Path,
        default=None,
        help="Optional legacy static ETF catalog; the default discovers all listed QDII funds",
    )
    parser.add_argument("--as-of", help="Evaluation date in YYYY-MM-DD format")
    parser.add_argument(
        "--allow-partial-holder-period",
        action="store_true",
        help="Use the newest holder period even when coverage is below 95%%",
    )
    args = parser.parse_args(argv)
    if args.top <= 0:
        parser.error("--top must be positive")
    if args.min_scale is not None and args.min_scale < 0:
        parser.error("--min-scale must be non-negative")
    if args.min_age_years < 0:
        parser.error("--min-age-years must be non-negative")
    if args.min_five_year_return_pct < -100:
        parser.error("--min-five-year-return-pct must be at least -100")
    if args.min_ten_year_return_pct < -100:
        parser.error("--min-ten-year-return-pct must be at least -100")
    if not 0 <= args.min_us_equity_pct <= 100:
        parser.error("--min-us-equity-pct must be between 0 and 100")
    if args.min_direct_limit_cny < 0:
        parser.error("--min-direct-limit-cny must be non-negative")
    if args.as_of:
        try:
            parse_date(args.as_of)
        except ValueError:
            parser.error("--as-of must use YYYY-MM-DD")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    metrics = RunMetrics()
    client = HttpClient()
    started = time.perf_counter()
    output_dir = args.output_dir.resolve()
    publish_dir = args.publish_dir.resolve()
    try:
        payload = build_payload(args, client, metrics)
        with metrics.phase("artifact_rendering"):
            write_json(output_dir / "latest.json", payload)
            write_csv(output_dir / "latest.csv", payload)
            write_markdown(output_dir / "latest.md", payload)
            write_html(output_dir / "latest.html", payload)
            write_html(publish_dir / "index.html", payload)
            write_json(output_dir / "history" / f"{payload['run_date']}.json", payload)
    except (DataError, OSError, ValueError) as exc:
        try:
            write_json(
                output_dir / "run-metrics.json",
                {
                    "schema_version": 1,
                    "status": "failure",
                    "run_date": (
                        parse_date(args.as_of).isoformat()
                        if args.as_of
                        else current_shanghai_date().isoformat()
                    ),
                    "refresh_seconds": round(time.perf_counter() - started, 3),
                    **metrics.snapshot(),
                    "http": client.metrics(),
                    "error": str(exc),
                },
            )
        except (OSError, ValueError):
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    refresh_seconds = round(time.perf_counter() - started, 3)
    run_metrics = {
        "schema_version": 1,
        "status": "success",
        "run_date": payload["run_date"],
        "generated_at": payload.get("generated_at"),
        "refresh_seconds": refresh_seconds,
        **metrics.snapshot(),
        "http": client.metrics(),
        "cache": payload.get("cache", {}),
        "candidate_counts": {
            key: payload.get("filters", {}).get(key)
            for key in (
                "base_candidates_total",
                "performance_candidates_scanned",
                "performance_qualified_count",
                "contract_candidates_scanned",
                "us_equity_candidates_scanned",
                "us_quota_candidates_scanned",
                "global_quota_candidates_scanned",
            )
        },
    }
    write_json(output_dir / "run-metrics.json", run_metrics)
    print(
        f"Wrote {len(payload['records'])} US records and "
        f"{len(payload['global_supplement']['records'])} global records to {output_dir} "
        f"and static site to {publish_dir} in {refresh_seconds:.1f}s"
    )
    for warning in payload["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
