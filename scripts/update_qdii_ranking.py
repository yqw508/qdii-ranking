#!/usr/bin/env python3
"""Build an on-demand ranking of purchasable RMB A-class QDII funds."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
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
DEFAULT_EXCLUDE_KEYWORDS = ["债", "亚洲", "中国", "港"]
DEFAULT_US_EQUITY_CATALOG = Path(__file__).resolve().parents[1] / "references" / "us-equity-instruments.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
SHANGHAI_TZ = timezone(timedelta(hours=8))
PERFORMANCE_WORKERS = 6
PERFORMANCE_CACHE_SCHEMA_VERSION = 1
FUND_EXPOSURE_CACHE_SCHEMA_VERSION = 1
US_EQUITY_METHOD_VERSION = 1
NOTICE_TITLE_RE = re.compile(
    r"大额申购|申购.{0,12}限额|限额.{0,12}申购|恢复.{0,12}申购"
)
REPORT_TITLE_EXCLUDE_RE = re.compile(r"摘要|提示性公告")
DIRECT_CHANNEL_PATTERN = (
    r"(?<!非)直销销售机构|(?<!非)直销机构|直销渠道|直销中心柜台|电子直销平台|网上直销平台"
)
AGENCY_CHANNEL_PATTERN = r"非直销销售机构|代销机构|代销渠道"


class DataError(RuntimeError):
    """Raised when source data is incomplete enough to invalidate the ranking."""


class HttpClient:
    def __init__(self, retries: int = 4, timeout: int = 30) -> None:
        self.retries = retries
        self.timeout = timeout

    def get_bytes(self, url: str, referer: str | None = None) -> bytes:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, headers=headers)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (2**attempt))
        raise DataError(f"Failed to fetch {url}: {last_error}")

    def get_text(
        self, url: str, referer: str | None = None, encoding: str = "utf-8-sig"
    ) -> str:
        return self.get_bytes(url, referer=referer).decode(encoding, errors="replace")

    def get_json(self, url: str, referer: str | None = None) -> dict[str, Any]:
        try:
            return json.loads(self.get_text(url, referer=referer))
        except json.JSONDecodeError as exc:
            raise DataError(f"Invalid JSON from {url}: {exc}") from exc


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
    if not meta["fund_type"].startswith("QDII"):
        return False
    if re.search(
        r"美元|港币|后端|人民币[CD]|(?:\(|（|/|\s)[CD](?:类|份额|\)|）|$)|[CD](?:类|份额|\)|）|$)",
        name,
    ):
        return False
    if re.search(r"人民币A|A类|A1(?:\(|$)|A(?:\(|$)", name):
        return True
    return "人民币" in name


def build_holder_candidates(
    rows: list[list[str]], metadata: dict[str, dict[str, str]], keywords: list[str]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 6 or row[0] not in metadata or not row[2]:
            continue
        meta = metadata[row[0]]
        if not is_rmb_a_share(meta):
            continue
        if meta["fund_type"] == "QDII-纯债":
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
    return {
        "inception_date": inception_match.group(1),
        "scale_billion_cny": round(scale, 4),
        "scale_report_date": scale_match.group(3),
        "purchase_status": purchase_status,
        "purchase_status_text": state_text,
        "page_agency_limit_cny": page_limit,
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
    min_scale: float,
    top: int,
    exclude_keywords: Iterable[str] = (),
    as_of: date | None = None,
    min_age_years: int = 0,
) -> list[dict[str, Any]]:
    eligible = [
        item
        for item in candidates
        if item["scale_billion_cny"] > min_scale
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


def fetch_trailing_performance(
    client: HttpClient, fund: dict[str, Any], as_of: date
) -> tuple[dict[str, Any], list[str]]:
    code = fund["code"]
    url = PERFORMANCE_DATA_URL.format(code=code, cache_buster=as_of.strftime("%Y%m%d"))
    payload = client.get_text(url, referer=fund["fund_page_url"])
    points = parse_performance_page(payload, code)
    output: dict[str, Any] = {"performance_source_url": url}
    warnings: list[str] = []
    for years, prefix, label in (
        (1, "one_year", "近一年"),
        (3, "three_year", "近三年"),
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
    return output, warnings


class PerformanceResultCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()

    @staticmethod
    def _validate(
        payload: dict[str, Any], code: str, as_of: date
    ) -> tuple[dict[str, Any], list[str]]:
        if (
            payload.get("schema_version") != PERFORMANCE_CACHE_SCHEMA_VERSION
            or payload.get("code") != code
            or payload.get("as_of") != as_of.isoformat()
        ):
            raise DataError("Cached performance identity does not match the request")
        performance = payload.get("performance")
        warnings = payload.get("warnings")
        if not isinstance(performance, dict) or not isinstance(warnings, list):
            raise DataError("Cached performance payload is incomplete")
        required_fields = {
            "performance_source_url",
            "one_year_return_pct",
            "one_year_max_drawdown_pct",
            "one_year_performance_start_date",
            "one_year_performance_end_date",
            "three_year_return_pct",
            "three_year_max_drawdown_pct",
            "three_year_performance_start_date",
            "three_year_performance_end_date",
        }
        if not required_fields.issubset(performance):
            raise DataError("Cached performance fields are incomplete")
        if not all(isinstance(warning, str) for warning in warnings):
            raise DataError("Cached performance warnings are invalid")
        for field in required_fields:
            value = performance[field]
            if field.endswith("_date"):
                if value is not None and parse_date(str(value)) > as_of:
                    raise DataError("Cached performance contains a future observation")
            elif field != "performance_source_url" and value is not None and not isinstance(
                value, (int, float)
            ):
                raise DataError("Cached performance metric is invalid")
        return dict(performance), list(warnings)

    def get(
        self, client: HttpClient, fund: dict[str, Any], as_of: date
    ) -> tuple[dict[str, Any], list[str]]:
        code = fund["code"]
        path = self.directory / as_of.isoformat() / f"{code}.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                result = self._validate(payload, code, as_of)
                with self._stats_lock:
                    self.hits += 1
                return result
            except (DataError, OSError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
        with self._stats_lock:
            self.misses += 1
        performance, warnings = fetch_trailing_performance(client, fund, as_of)
        write_json(
            path,
            {
                "schema_version": PERFORMANCE_CACHE_SCHEMA_VERSION,
                "code": code,
                "as_of": as_of.isoformat(),
                "performance": performance,
                "warnings": warnings,
            },
        )
        return performance, warnings

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }


def filter_performance_full_scan(
    client: HttpClient,
    candidates: list[dict[str, Any]],
    as_of: date,
    min_three_year_return_pct: float,
    top: int,
) -> tuple[list[dict[str, Any]], list[str], int]:
    selected: list[dict[str, Any]] = []
    warnings: list[str] = []
    scanned = 0
    for fund in candidates:
        scanned += 1
        performance, performance_warnings = fetch_trailing_performance(client, fund, as_of)
        warnings.extend(performance_warnings)
        three_year_return = performance["three_year_return_pct"]
        if three_year_return is None or three_year_return < min_three_year_return_pct:
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
) -> tuple[list[dict[str, Any]], list[str], int, int, int, int]:
    warnings: list[str] = []
    performance_results: list[tuple[dict[str, Any], list[str]] | None] = [
        None
    ] * len(candidates)

    def evaluate_performance(
        fund: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        if performance_cache is not None:
            return performance_cache.get(client, fund, as_of)
        return fetch_trailing_performance(client, fund, as_of)

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
        three_year_return = performance["three_year_return_pct"]
        if three_year_return is None or three_year_return < min_three_year_return_pct:
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
        exposure_qualified.append(
            {**fund, "us_equity_exposure": exposure}
        )
    exposure_qualified.sort(
        key=lambda item: (
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


def normalize_notice_text(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


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
        r"(?:累计申购|申购金额)[^。；]{0,80}?(?:不超过|不得超过|上限调整为)\s*([\d,.]+)\s*(万元|元)",
        r"超过\s*([\d,.]+)\s*(万元|元)[^。；]{0,50}?(?:申购|大额申购)",
        r"业务限额为\s*([\d,.]+)\s*(万元|元)",
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
    client: HttpClient, code: str, as_of: date
) -> PeriodicReport:
    params = {"fundcode": code, "pageIndex": "1", "pageSize": "100", "type": "0"}
    url = f"{ANNOUNCEMENT_API_URL}?{urllib.parse.urlencode(params)}"
    referer = f"https://fundf10.eastmoney.com/jjgg_{code}.html"
    payload = client.get_json(url, referer=referer)
    reports: list[PeriodicReport] = []
    for item in payload.get("Data") or []:
        report_date = parse_periodic_report_date(str(item.get("TITLE") or ""))
        if report_date is None:
            continue
        published = parse_date(str(item["PUBLISHDATEDesc"]))
        if published > as_of or report_date > as_of:
            continue
        announcement_id = str(item["ID"])
        reports.append(
            PeriodicReport(
                announcement_id=announcement_id,
                title=str(item["TITLE"]),
                report_date=report_date,
                published_date=published,
                source_url=ANNOUNCEMENT_PDF_URL.format(announcement_id=announcement_id),
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

    @staticmethod
    def _validate(pdf_bytes: bytes) -> str:
        if len(pdf_bytes) < 1000 or not pdf_bytes.lstrip().startswith(b"%PDF-"):
            raise DataError("Downloaded periodic report is not a valid PDF")
        text = extract_pdf_text(pdf_bytes)
        if not text.strip():
            raise DataError("Downloaded periodic report has no extractable text")
        return text

    def get_text(
        self, client: HttpClient, report: PeriodicReport, referer: str
    ) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{report.announcement_id}.pdf"
        if path.exists():
            try:
                text = self._validate(path.read_bytes())
                self.hits += 1
                return text
            except (DataError, OSError):
                self.corrupt_redownloads += 1
        pdf_bytes = client.get_bytes(report.source_url, referer=referer)
        text = self._validate(pdf_bytes)
        temporary = path.with_suffix(".pdf.tmp")
        temporary.write_bytes(pdf_bytes)
        temporary.replace(path)
        self.downloads += 1
        return text

    def stats(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "downloads": self.downloads,
            "corrupt_redownloads": self.corrupt_redownloads,
        }


def normalize_instrument_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", value.upper().replace("V AN", "VAN"))


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
        r"(\d{1,3}(?:,\d{3})+,\d{1,2})\s+(\d\.\d{2})(?=\s)", r"\1\2", text
    )
    return re.sub(r"(\d{1,3}(?:,\d{3})+)\s+(\.\d{2})(?=\s)", r"\1\2", text)


def parse_fund_investment_rows(text: str, code: str) -> list[dict[str, Any]]:
    heading = re.search(r"前十名基金投资明\s*细", text)
    if not heading:
        raise DataError(f"Could not locate top fund investments for fund {code}")
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
            name_match = re.match(
                r"(.+?)\s+(?:债\s*券\s*型|股\s*票\s*型|混\s*合\s*型|商\s*品\s*型|权\s*益\s*类)\s+",
                body,
            )
        if not name_match and "基金" not in body:
            continue
        percentages = [
            float(value)
            for value in re.findall(r"(?<![\d,])(\d{1,3}\.\d{2})(?!\d)", body)
            if float(value) <= 100
        ]
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
    direct_heading = re.search(
        r"(?:报告期末|期末)在各个国家（地区）证券市场的"
        r"(?:股票及存托凭证|权益)投资分\s*布",
        text,
    )
    if not direct_heading:
        raise DataError(f"Could not locate country equity distribution for fund {code}")
    direct_segment = text[direct_heading.end() : direct_heading.end() + 1200]
    next_heading = re.search(r"\n\s*\d+(?:\.\d+)+\s*", direct_segment)
    if next_heading:
        direct_segment = direct_segment[: next_heading.start()]
    direct_match = re.search(
        r"美国\s+([\d,.]+)\s+(\d+(?:\.\d+)?)", re.sub(r"\s+", " ", direct_segment)
    )
    direct_us_pct = float(direct_match.group(2)) if direct_match else 0.0

    asset_heading = re.search(r"报告期末基金资产组合情况", text)
    if not asset_heading:
        raise DataError(f"Could not locate asset allocation table for fund {code}")
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
            r"期末基金资产净值(.{0,350}?)5\.\s*期末基金份额净值", text, re.S
        )
        if not net_match:
            raise DataError(f"Could not parse fund net assets for fund {code}")
        net_values = [
            float(value.replace(",", ""))
            for value in re.findall(r"(?<!\d)(\d[\d,]*\.\d{2})(?!\d)", net_match.group(1))
        ]
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
            "按保守规则排除。"
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
    ) -> tuple[dict[str, Any], list[str]]:
        code = fund["code"]
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
                self.hits += 1
                classified, threshold_warnings = apply_us_equity_threshold(
                    exposure, threshold
                )
                return classified, [*warnings, *threshold_warnings]
            except (DataError, OSError, TypeError, ValueError, json.JSONDecodeError):
                self.corrupt_rebuilds += 1
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
) -> tuple[dict[str, Any], list[str]]:
    if exposure_cache is not None:
        return exposure_cache.get(
            client, fund, as_of, report_cache, resolver, threshold
        )
    report = fetch_latest_periodic_report(client, fund["code"], as_of)
    text = report_cache.get_text(client, report, fund["fund_page_url"])
    parsed = parse_us_equity_report(text, fund["code"])
    return calculate_us_equity_exposure(parsed, report, resolver, threshold)


def fetch_announcements(client: HttpClient, code: str, as_of: date) -> list[dict[str, Any]]:
    params = {"fundcode": code, "pageIndex": "1", "pageSize": "100", "type": "0"}
    url = f"{ANNOUNCEMENT_API_URL}?{urllib.parse.urlencode(params)}"
    payload = client.get_json(url, referer=f"https://fundf10.eastmoney.com/jjgg_{code}.html")
    notices = []
    cutoff = as_of - timedelta(days=550)
    for item in payload.get("Data") or []:
        published = parse_date(item["PUBLISHDATEDesc"])
        if published > as_of or published < cutoff or not NOTICE_TITLE_RE.search(item["TITLE"]):
            continue
        announcement_id = item["ID"]
        notices.append(
            {
                "id": announcement_id,
                "title": item["TITLE"],
                "published": published,
                "url": ANNOUNCEMENT_PDF_URL.format(announcement_id=announcement_id),
            }
        )
    notices.sort(key=lambda item: item["published"])
    return notices[-12:]


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
    client: HttpClient, fund: dict[str, Any], as_of: date
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
    notices = fetch_announcements(client, fund["code"], as_of)
    for notice in notices:
        try:
            pdf = client.get_bytes(notice["url"], referer=fund["fund_page_url"])
            text = extract_pdf_text(pdf)
            transitions.extend(parse_quota_notice(text, notice["published"], notice["url"]))
        except DataError as exc:
            warnings.append(f"{fund['code']} quota notice could not be parsed: {exc}")

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
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "rank",
        "code",
        "name",
        "fund_type",
        "institution_holding_ratio_pct",
        "holder_report_date",
        "inception_date",
        "scale_billion_cny",
        "scale_report_date",
        "one_year_return_pct",
        "one_year_max_drawdown_pct",
        "one_year_performance_start_date",
        "one_year_performance_end_date",
        "three_year_return_pct",
        "three_year_max_drawdown_pct",
        "three_year_performance_start_date",
        "three_year_performance_end_date",
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
        for item in records:
            writer.writerow(
                {
                    **{field: item.get(field) for field in fields},
                    "us_equity_confirmed_pct": item["us_equity_exposure"]["confirmed_pct"],
                    "us_equity_possible_pct": item["us_equity_exposure"]["possible_pct"],
                    "us_equity_status": item["us_equity_exposure"]["status"],
                    "us_equity_report_date": item["us_equity_exposure"]["report_date"],
                    "us_equity_source_url": item["us_equity_exposure"]["source_url"],
                    "direct_limit": format_limit(item["direct_limit"]),
                    "agency_limit": format_limit(item["agency_limit"]),
                    "quota_source_urls": " | ".join(item["quota_source_urls"]),
                }
            )
    temporary.replace(path)


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    records = payload["records"]
    scale_dates = "、".join(
        sorted({item["scale_report_date"] for item in records})
    )
    lines = [
        "# QDII 美股含量榜单",
        "",
        f"- 更新日期：{payload['run_date']}",
        f"- 机构持仓报告期：{payload['holder_report_date']}",
        f"- 规模报告期：{scale_dates}",
        f"- 近一年净值观察区间：{summarize_periods(records, 'one_year')}",
        f"- 近三年净值观察区间：{summarize_periods(records, 'three_year')}",
        f"- 申购额度评估日：{payload['run_date']}",
        f"- 筛选条件：规模 > {payload['filters']['min_scale_billion_cny']:g} 亿元；"
        f"成立超过 {payload['filters']['min_age_years']} 年；"
        f"近三年复权收益 >= {payload['filters']['min_three_year_return_pct']:g}%；"
        f"美股占比确认下限 >= {payload['filters']['min_us_equity_pct']:g}%；"
        f"名称排除 {'、'.join(payload['filters']['exclude_keywords']) or '无'}；"
        "人民币 A 类或无 C/D 标记的人民币主份额；场外可申购",
        "- 排名规则：美股确认下限降序；同值时依次按机构持仓、近三年收益和基金代码排序",
        f"- 全量筛选：基础候选 {payload['filters']['base_candidates_total']} 只；"
        f"业绩扫描 {payload['filters']['performance_candidates_scanned']} 只；"
        f"业绩达标及美股扫描 {payload['filters']['us_equity_candidates_scanned']} 只；"
        f"美股达标 {payload['filters']['us_equity_qualified_count']} 只",
        f"- 缓存：净值命中 {payload['cache']['performance']['hits']} 次；"
        f"基金穿透命中 {payload['cache']['fund_us_equity_exposures']['hits']} 次；"
        f"报告缓存命中 {payload['cache']['periodic_reports']['hits']} 次、"
        f"下载 {payload['cache']['periodic_reports']['downloads']} 次",
        "",
        "| 排名 | 基金 | 成立日 | 机构持有 | 规模 | 近一年涨幅 | 近一年最大回撤 | 近三年涨幅 | 近三年最大回撤 | 美股占比确认下限 | 直销额度 | 代销额度 | 计算规则 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in payload["records"]:
        source = item["quota_source_urls"][-1] if item["quota_source_urls"] else item["fund_page_url"]
        rule = format_rule(item["share_class_rule"], item["channel_rule"])
        lines.append(
            f"| {item['rank']} | [{item['name']} {item['code']}]({source}) | "
            f"{item['inception_date']} | {item['institution_holding_ratio_pct']:.2f}% | "
            f"{item['scale_billion_cny']:.2f}亿元 | "
            f"{format_percentage(item['one_year_return_pct'], show_sign=True)} | "
            f"{format_percentage(item['one_year_max_drawdown_pct'])} | "
            f"{format_percentage(item['three_year_return_pct'], show_sign=True)} | "
            f"{format_percentage(item['three_year_max_drawdown_pct'])} | "
            f"[{format_percentage(item['us_equity_exposure']['confirmed_pct'])}]"
            f"({item['us_equity_exposure']['source_url']}) | "
            f"{format_limit(item['direct_limit'])} | {format_limit(item['agency_limit'])} | {rule} |"
        )
    if payload["warnings"]:
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {warning}" for warning in payload["warnings"])
    lines.extend(
        [
            "",
            "额度为基金管理人层面的单日单基金账户上限；代销平台可能设置更低限制。",
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
    records = payload["records"]
    filters = payload["filters"]
    scale_dates = "、".join(sorted({item["scale_report_date"] for item in records}))
    filter_parts = (
        f"规模 > {filters['min_scale_billion_cny']:g} 亿元",
        f"成立超过 {filters['min_age_years']} 年",
        f"近三年收益 ≥ {filters['min_three_year_return_pct']:g}%",
        f"美股确认下限 ≥ {filters['min_us_equity_pct']:g}%",
    )
    filter_html = "".join(
        f'<span class="filter-condition">{html.escape(part)}</span>'
        for part in filter_parts
    )
    cards: list[str] = []
    for item in records:
        exposure = item["us_equity_exposure"]
        direct_text = format_limit(item["direct_limit"])
        agency_text = format_limit(item["agency_limit"])
        direct_link = html_source_link(direct_text, item["direct_limit"].get("source_url"))
        agency_link = html_source_link(agency_text, item["agency_limit"].get("source_url"))
        report_link = html_source_link(
            f"定期报告 {exposure['report_date']}", exposure.get("source_url")
        )
        fund_link = html_source_link("基金主页", item["fund_page_url"])
        cards.append(
            f"""
      <details class="fund-item" data-code="{html.escape(item['code'], quote=True)}">
        <summary>
          <span class="rank" aria-label="排名 {item['rank']}">{item['rank']}</span>
          <span class="fund-identity">
            <strong>{html.escape(item['name'])}</strong>
            <span class="fund-code">{html.escape(item['code'])}</span>
          </span>
          <span class="summary-metrics">
            <span class="summary-metric accent">
              <span class="metric-label">美股下限</span>
              <span class="metric-value">{format_percentage(exposure['confirmed_pct'])}</span>
            </span>
            <span class="summary-metric positive">
              <span class="metric-label">近三年</span>
              <span class="metric-value">{format_percentage(item['three_year_return_pct'], show_sign=True)}</span>
            </span>
            <span class="summary-metric">
              <span class="metric-label">直销</span>
              <span class="metric-value quota">{direct_text}</span>
            </span>
            <span class="summary-metric">
              <span class="metric-label">代销</span>
              <span class="metric-value quota">{agency_text}</span>
            </span>
          </span>
          <span class="chevron" aria-hidden="true"></span>
        </summary>
        <div class="fund-detail">
          <dl class="detail-grid">
            <div><dt>成立日</dt><dd>{html.escape(item['inception_date'])}</dd></div>
            <div><dt>机构持有</dt><dd>{format_percentage(item['institution_holding_ratio_pct'])}</dd></div>
            <div><dt>规模</dt><dd>{item['scale_billion_cny']:.2f} 亿元</dd></div>
            <div><dt>规模报告期</dt><dd>{html.escape(item['scale_report_date'])}</dd></div>
            <div><dt>近一年收益</dt><dd class="positive-text">{format_percentage(item['one_year_return_pct'], show_sign=True)}</dd></div>
            <div><dt>近一年最大回撤</dt><dd class="negative-text">{format_percentage(item['one_year_max_drawdown_pct'])}</dd></div>
            <div><dt>近三年收益</dt><dd class="positive-text">{format_percentage(item['three_year_return_pct'], show_sign=True)}</dd></div>
            <div><dt>近三年最大回撤</dt><dd class="negative-text">{format_percentage(item['three_year_max_drawdown_pct'])}</dd></div>
            <div><dt>美股可能上限</dt><dd>{format_percentage(exposure['possible_pct'])}</dd></div>
            <div><dt>未解析仓位</dt><dd>{format_percentage(exposure['unresolved_pct'])}</dd></div>
          </dl>
          <div class="quota-grid" aria-label="申购额度">
            <div><span>直销额度</span>{direct_link}</div>
            <div><span>代销额度</span>{agency_link}</div>
          </div>
          <dl class="rule-grid">
            <div><dt>额度计算</dt><dd>{html.escape(format_rule(item['share_class_rule'], item['channel_rule']))}</dd></div>
            <div><dt>净值区间</dt><dd>{html.escape(item['three_year_performance_start_date'])} 至 {html.escape(item['three_year_performance_end_date'])}</dd></div>
          </dl>
          <div class="source-row">
            {report_link}
            {fund_link}
          </div>
        </div>
      </details>"""
        )

    warning_items = "".join(
        f"<li>{html.escape(warning)}</li>" for warning in payload["warnings"]
    )
    warning_section = ""
    if payload["warnings"]:
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
  <title>QDII 美股含量榜单 · {html.escape(payload['run_date'])}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f6f8;
      --surface: #ffffff;
      --text: #17202a;
      --muted: #65717d;
      --border: #d9dfe6;
      --accent: #086b58;
      --accent-soft: #e7f4f0;
      --positive: #147a4b;
      --negative: #b42318;
      --link: #1457a6;
      --warning: #8a4b08;
    }}
    * {{ box-sizing: border-box; }}
    html {{ background: var(--bg); }}
    body {{
      margin: 0;
      min-width: 280px;
      color: var(--text);
      background: var(--bg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      font-size: 15px;
      line-height: 1.5;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }}
    a {{ color: var(--link); text-decoration-thickness: 1px; text-underline-offset: 3px; }}
    .page {{
      width: min(100%, 880px);
      margin: 0 auto;
      padding: max(16px, env(safe-area-inset-top)) max(14px, env(safe-area-inset-right)) max(28px, env(safe-area-inset-bottom)) max(14px, env(safe-area-inset-left));
    }}
    .page-header {{ padding: 4px 2px 18px; }}
    .title-row {{ display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }}
    h1 {{ margin: 0; font-size: 22px; line-height: 1.25; letter-spacing: 0; }}
    .run-date {{ color: var(--muted); font-size: 13px; white-space: nowrap; }}
    .filter-line {{ display: flex; flex-wrap: wrap; gap: 2px 8px; margin: 10px 0 0; color: #35414d; font-size: 14px; }}
    .filter-condition {{ white-space: nowrap; }}
    .filter-condition:not(:last-child)::after {{ content: " ·"; color: var(--muted); }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px 14px;
      margin: 14px 0 0;
    }}
    .meta-grid div {{ min-width: 0; }}
    dt, .metric-label, .quota-grid span {{ color: var(--muted); font-size: 12px; }}
    dd {{ margin: 2px 0 0; font-weight: 650; }}
    .ranking-list {{ display: grid; gap: 10px; }}
    .fund-item {{
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface);
      overflow: clip;
    }}
    .fund-item summary {{
      display: grid;
      grid-template-columns: 34px minmax(0, 1fr) 18px;
      align-items: center;
      gap: 0 10px;
      min-height: 64px;
      padding: 12px;
      cursor: pointer;
      list-style: none;
      -webkit-tap-highlight-color: transparent;
    }}
    .fund-item summary::-webkit-details-marker {{ display: none; }}
    .fund-item summary:focus-visible {{ outline: 3px solid #86b7e8; outline-offset: -3px; }}
    .rank {{
      display: grid;
      width: 32px;
      height: 32px;
      place-items: center;
      border-radius: 4px;
      color: #fff;
      background: #263746;
      font-weight: 750;
      font-variant-numeric: tabular-nums;
    }}
    .fund-identity {{ min-width: 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 8px; }}
    .fund-identity strong {{ min-width: 0; font-size: 16px; line-height: 1.35; }}
    .fund-code {{ color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }}
    .chevron {{
      width: 9px;
      height: 9px;
      border-right: 2px solid #7a8793;
      border-bottom: 2px solid #7a8793;
      transform: rotate(45deg) translate(-2px, 2px);
      transition: transform 150ms ease;
    }}
    .fund-item[open] .chevron {{ transform: rotate(225deg) translate(-1px, -1px); }}
    .summary-metrics {{
      grid-column: 2 / -1;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 12px;
    }}
    .summary-metric {{
      display: flex;
      min-width: 0;
      min-height: 46px;
      flex-direction: column;
      justify-content: center;
      padding: 6px 8px;
      border-left: 3px solid #c7cfd7;
      background: #f7f8fa;
    }}
    .summary-metric.accent {{ border-color: var(--accent); background: var(--accent-soft); }}
    .summary-metric.positive {{ border-color: var(--positive); }}
    .metric-value {{ font-weight: 750; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .metric-value.quota {{ font-size: 14px; }}
    .fund-detail {{ border-top: 1px solid var(--border); padding: 14px 12px 16px; }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 16px;
      margin: 0;
    }}
    .detail-grid div, .rule-grid div {{ min-width: 0; }}
    .positive-text {{ color: var(--positive); }}
    .negative-text {{ color: var(--negative); }}
    .quota-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin: 16px 0 0;
      padding: 14px 0;
      border-top: 1px solid var(--border);
      border-bottom: 1px solid var(--border);
    }}
    .quota-grid div {{ display: flex; min-width: 0; flex-direction: column; gap: 3px; }}
    .source-link, .source-value {{ font-weight: 700; }}
    .external {{ margin-left: 4px; font-size: 12px; }}
    .rule-grid {{ display: grid; gap: 10px; margin: 14px 0 0; }}
    .source-row {{ display: flex; flex-wrap: wrap; gap: 10px 18px; margin-top: 14px; }}
    .warnings {{ margin-top: 18px; border-top: 1px solid var(--border); }}
    .warnings summary {{
      display: flex;
      min-height: 48px;
      align-items: center;
      justify-content: space-between;
      color: var(--warning);
      cursor: pointer;
      font-weight: 700;
    }}
    .warnings ul {{ margin: 0; padding: 0 0 0 22px; color: #4c5661; }}
    .warnings li {{ margin: 0 0 9px; }}
    footer {{ margin-top: 18px; color: var(--muted); font-size: 12px; }}
    @media (min-width: 820px) {{
      .page {{ padding-top: 28px; }}
      .meta-grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
      .fund-item summary {{ grid-template-columns: 38px minmax(220px, 1fr) minmax(390px, 430px) 18px; gap: 12px; padding: 14px 16px; }}
      .summary-metrics {{ grid-column: 3; grid-row: 1; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 0; }}
      .summary-metric {{ padding-inline: 8px; }}
      .chevron {{ grid-column: 4; }}
      .fund-detail {{ padding: 18px 66px 20px; }}
      .detail-grid {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }}
      .rule-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (prefers-reduced-motion: reduce) {{ .chevron {{ transition: none; }} }}
  </style>
</head>
<body>
  <div class="page">
    <header class="page-header">
      <div class="title-row">
        <h1>QDII 美股含量榜单</h1>
        <time class="run-date" datetime="{html.escape(payload['run_date'], quote=True)}">{html.escape(payload['run_date'])}</time>
      </div>
      <p class="filter-line">{filter_html}</p>
      <dl class="meta-grid">
        <div><dt>机构持仓报告期</dt><dd>{html.escape(payload['holder_report_date'])}</dd></div>
        <div><dt>规模报告期</dt><dd>{html.escape(scale_dates)}</dd></div>
        <div><dt>近一年净值区间</dt><dd>{html.escape(summarize_periods(records, 'one_year'))}</dd></div>
        <div><dt>全量扫描</dt><dd>{filters.get('base_candidates_total', len(records))} 只基础候选</dd></div>
        <div><dt>数据警告</dt><dd>{len(payload['warnings'])} 项</dd></div>
      </dl>
    </header>
    <main>
      <section class="ranking-list" aria-label="美股含量基金榜单">
        {''.join(cards)}
      </section>
      {warning_section}
    </main>
    <footer>额度为基金管理人层面的单日单基金账户上限；代销平台可能设置更低限制。</footer>
  </div>
</body>
</html>
"""
    document = "\n".join(line.rstrip() for line in document.splitlines()) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(path)


def build_payload(args: argparse.Namespace, client: HttpClient) -> dict[str, Any]:
    as_of = parse_date(args.as_of) if args.as_of else current_shanghai_date()
    metadata = fetch_fund_metadata(client)
    periods = fetch_holder_periods(client)
    selected, warnings = select_holder_period(periods, args.allow_partial_holder_period)
    holder_rows = fetch_holder_rows(client, selected)
    pre_rank_keywords = [keyword for keyword in args.exclude_keywords if keyword == "债"]
    post_rank_keywords = [keyword for keyword in args.exclude_keywords if keyword != "债"]
    candidates = build_holder_candidates(holder_rows, metadata, pre_rank_keywords)
    enriched = enrich_fund_pages(client, candidates)
    preliminary = filter_and_rank(
        enriched,
        args.min_scale,
        len(enriched),
        exclude_keywords=post_rank_keywords,
        as_of=as_of,
        min_age_years=args.min_age_years,
    )
    cache_root = (args.cache_dir or (args.output_dir / "cache")).resolve()
    report_cache = PeriodicReportCache(cache_root / "periodic-reports")
    resolver = LookthroughResolver(
        args.us_equity_catalog.resolve(), cache_root / "us-equity-lookthrough.json"
    )
    performance_cache = PerformanceResultCache(cache_root / "performance")
    exposure_cache = FundExposureResultCache(cache_root / "fund-us-equity-exposures")
    (
        ranked,
        performance_warnings,
        performance_scanned_count,
        performance_qualified_count,
        us_equity_scanned_count,
        us_equity_qualified_count,
    ) = filter_performance_and_us_exposure_full_scan(
        client,
        preliminary,
        as_of,
        args.min_three_year_return_pct,
        args.min_us_equity_pct,
        args.top,
        report_cache,
        resolver,
        performance_cache,
        exposure_cache,
    )
    warnings.extend(performance_warnings)
    records: list[dict[str, Any]] = []
    for rank, fund in enumerate(ranked, start=1):
        quota, quota_warnings = resolve_quota(client, fund, as_of)
        warnings.extend(quota_warnings)
        records.append(
            {
                "rank": rank,
                "code": fund["code"],
                "name": fund["name"],
                "fund_type": fund["fund_type"],
                "institution_holding_ratio_pct": fund["institution_holding_ratio_pct"],
                "holder_report_date": selected.report_date,
                "inception_date": fund["inception_date"],
                "scale_billion_cny": fund["scale_billion_cny"],
                "scale_report_date": fund["scale_report_date"],
                "purchase_status": fund["purchase_status"],
                "purchase_status_text": fund["purchase_status_text"],
                "fund_page_url": fund["fund_page_url"],
                "performance_source_url": fund["performance_source_url"],
                "one_year_return_pct": fund["one_year_return_pct"],
                "one_year_max_drawdown_pct": fund["one_year_max_drawdown_pct"],
                "one_year_performance_start_date": fund["one_year_performance_start_date"],
                "one_year_performance_end_date": fund["one_year_performance_end_date"],
                "three_year_return_pct": fund["three_year_return_pct"],
                "three_year_max_drawdown_pct": fund["three_year_max_drawdown_pct"],
                "three_year_performance_start_date": fund["three_year_performance_start_date"],
                "three_year_performance_end_date": fund["three_year_performance_end_date"],
                "us_equity_exposure": fund["us_equity_exposure"],
                **quota,
            }
        )
    if len(records) < args.top:
        warnings.append(
            f"仅 {len(records)} 只基金符合全部条件；已向下补位但候选池仍不足 {args.top} 只。"
        )
    return {
        "schema_version": 6,
        "run_date": as_of.isoformat(),
        "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "holder_report_date": selected.report_date,
        "holder_period_fund_count": selected.fund_count,
        "filters": {
            "top": args.top,
            "min_scale_billion_cny": args.min_scale,
            "min_age_years": args.min_age_years,
            "min_three_year_return_pct": args.min_three_year_return_pct,
            "min_us_equity_pct": args.min_us_equity_pct,
            "base_candidates_total": len(preliminary),
            "performance_candidates_scanned": performance_scanned_count,
            "performance_qualified_count": performance_qualified_count,
            "us_equity_candidates_scanned": us_equity_scanned_count,
            "us_equity_qualified_count": us_equity_qualified_count,
            "full_scan_completed": (
                performance_scanned_count == len(preliminary)
                and us_equity_scanned_count == performance_qualified_count
            ),
            "ranking_method": (
                "us_equity_confirmed_pct desc, institution_holding_ratio_pct desc, "
                "three_year_return_pct desc, code asc"
            ),
            "us_equity_method": "conservative confirmed lower bound; unresolved positions only increase possible upper bound",
            "exclude_keywords": args.exclude_keywords,
            "pre_rank_exclude_keywords": pre_rank_keywords,
            "post_enrichment_exclude_keywords": post_rank_keywords,
            "exclude_fund_types": ["QDII-纯债"],
            "share_class": "RMB A or explicit RMB primary share without C/D marker",
            "purchasable_only": True,
        },
        "cache": {
            "performance": performance_cache.stats(),
            "fund_us_equity_exposures": exposure_cache.stats(),
            "periodic_reports": report_cache.stats(),
            "underlying_exposures": resolver.stats(),
        },
        "records": records,
        "warnings": list(dict.fromkeys(warnings)),
        "sources": {
            "fund_list": FUND_LIST_URL,
            "holder_data": HOLDER_API_URL,
            "performance": PERFORMANCE_DATA_URL,
            "announcements": ANNOUNCEMENT_API_URL,
            "periodic_reports": ANNOUNCEMENT_API_URL,
            "us_equity_instrument_catalog": str(args.us_equity_catalog.resolve()),
        },
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=10, help="Maximum result count")
    parser.add_argument(
        "--min-scale", type=float, default=3.0, help="Strict minimum scale in CNY 100m"
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
        default=50.0,
        help="Minimum trailing three-year adjusted return percentage",
    )
    parser.add_argument(
        "--min-us-equity-pct",
        type=float,
        default=50.0,
        help="Minimum confirmed US-equity exposure percentage",
    )
    parser.add_argument(
        "--exclude-keywords",
        nargs="*",
        default=DEFAULT_EXCLUDE_KEYWORDS,
        help="Fund-name keywords to exclude",
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
    parser.add_argument("--as-of", help="Evaluation date in YYYY-MM-DD format")
    parser.add_argument(
        "--allow-partial-holder-period",
        action="store_true",
        help="Use the newest holder period even when coverage is below 95%%",
    )
    args = parser.parse_args(argv)
    if args.top <= 0:
        parser.error("--top must be positive")
    if args.min_scale < 0:
        parser.error("--min-scale must be non-negative")
    if args.min_age_years < 0:
        parser.error("--min-age-years must be non-negative")
    if not 0 <= args.min_us_equity_pct <= 100:
        parser.error("--min-us-equity-pct must be between 0 and 100")
    if args.as_of:
        try:
            parse_date(args.as_of)
        except ValueError:
            parser.error("--as-of must use YYYY-MM-DD")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = build_payload(args, HttpClient())
        output_dir = args.output_dir.resolve()
        publish_dir = args.publish_dir.resolve()
        write_json(output_dir / "latest.json", payload)
        write_csv(output_dir / "latest.csv", payload["records"])
        write_markdown(output_dir / "latest.md", payload)
        write_html(output_dir / "latest.html", payload)
        write_html(publish_dir / "index.html", payload)
        write_json(output_dir / "history" / f"{payload['run_date']}.json", payload)
    except (DataError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Wrote {len(payload['records'])} records to {output_dir} "
        f"and static site to {publish_dir}"
    )
    for warning in payload["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
