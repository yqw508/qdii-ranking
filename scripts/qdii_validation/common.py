#!/usr/bin/env python3
"""Validate generated QDII ranking artifacts and an optional deployment."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import send_qdii_email as mailer
from qdii_ranking.config import RANKING_SCHEMA_VERSION


SHANGHAI_TZ = timezone(timedelta(hours=8))
EXPECTED_FILTERS = {
    "top": 10,
    "min_scale_billion_cny": None,
    "min_age_years": 3,
    "min_three_year_return_pct": 30.0,
    "three_year_boundary_tolerance_days": 7,
    "min_five_year_return_pct_if_available": 40.0,
    "min_ten_year_return_pct_if_available": 100.0,
    "min_us_equity_pct": 50.0,
    "min_direct_limit_cny_inclusive": 200,
}
EXPECTED_US_MAIN_EXCLUDE_KEYWORDS = {"亚洲", "中国", "港"}
ROUTING_REASON_CONFIRMED_US = "confirmed_us_exposure"
ROUTING_REASON_BELOW_US_THRESHOLD = "us_exposure_below_threshold"
ROUTING_REASON_GEOGRAPHY_OVERRIDE = "us_main_name_geography_override"
EXPECTED_RANKING_METHOD = (
    "nasdaq100_correlation desc, abs(nasdaq100_beta - 1) asc, "
    "us_equity_confirmed_pct desc, institution_holding_ratio_pct desc, "
    "three_year_return_pct desc, code asc"
)
EXPECTED_GLOBAL_RANKING_METHOD = (
    "three_year_return_drawdown_ratio desc, three_year_return_pct desc, "
    "three_year_max_drawdown_pct desc, institution_holding_ratio_pct desc, "
    "scale_billion_cny desc, code asc"
)
EXPECTED_NASDAQ100_OTC_RANKING_METHOD = (
    "common_period_return_pct desc, holding_cost.annualized_pct asc, "
    "nasdaq100_fit_common_period.tracking_error_pct asc, "
    "common_period_max_drawdown_pct desc, "
    "scale_billion_cny desc, code asc; missing values last"
)
NASDAQ100_OTC_NAME_RE = re.compile(r"(?:纳斯达克\s*100|纳指\s*100|NASDAQ\s*[-－]?\s*100)", re.I)
NASDAQ100_MIN_OBSERVATIONS = 140
NASDAQ100_MIN_SPAN_DAYS = 1000
EXPECTED_PREMIUM_GROUP_ORDER = ("标普500", "纳指100", "美国50", "道琼斯", "行业主题")
EXPECTED_PREMIUM_CODES = {
    "513500", "159612", "159655", "513650",
    "159513", "159659", "159632", "513300", "513390", "513870",
    "159941", "513100", "513110", "159660", "159501", "159696",
    "159577", "513850", "513400", "159509", "513290", "159502",
    "159529", "513350", "159518",
}
ETF_QUOTE_API_URL = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
ETF_MARKET_LIST_API_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
MARKDOWN_ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|\s*\[(.+)\s+(\d{6})\]\([^)]+\)\s*\|"
)
BOUNDARY_WARNING_RE = re.compile(
    r"三年边界容差 (?P<code>\d{6})：成立日 (?P<inception>\d{4}-\d{2}-\d{2})；"
    r"(?P<start>\d{4}-\d{2}-\d{2}) 至 (?P<end>\d{4}-\d{2}-\d{2})，"
    r"距完整三年少 (?P<days>[1-7]) 天。"
)


class ValidationError(RuntimeError):
    """Raised when an artifact cannot be published safely."""


class RankingHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.codes: list[str] = []
        self.lists: list[str] = []
        self.routing_reasons: list[str] = []
        self.blocks: dict[str, str] = {}
        self.premium_codes: list[str] = []
        self.premium_blocks: dict[str, str] = {}
        self.refresh_button_count = 0
        self.premium_tab_count = 0
        self.premium_table_count = 0
        self.premium_toggle_count = 0
        self.nasdaq_item_count = 0
        self.valuation_link_count = 0
        self.reference_tab_count = 0
        self.fund_hot_reference_count = 0
        self.fund_hot_link_count = 0
        self.fund_hot_safe_link_count = 0
        self.overview_details_count = 0
        self.all_text: list[str] = []
        self._current_code: str | None = None
        self._current_text: list[str] = []
        self._current_premium_code: str | None = None
        self._current_premium_text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "button" and attributes.get("id") == "premium-refresh":
            self.refresh_button_count += 1
        if tag == "button" and attributes.get("id") == "tab-premium":
            self.premium_tab_count += 1
        if tag == "button" and attributes.get("id") == "tab-reference":
            self.reference_tab_count += 1
        if tag == "table" and "premium-table" in classes:
            self.premium_table_count += 1
        if tag == "button" and "premium-row-toggle" in classes:
            self.premium_toggle_count += 1
        if tag == "details" and "nasdaq-item" in classes:
            self.nasdaq_item_count += 1
            if "open" in attributes:
                raise ValidationError("OTC Nasdaq-100 details must be closed by default")
        if tag == "a" and attributes.get("href") in {"valuation/", "/valuation/"}:
            self.valuation_link_count += 1
        if tag == "section" and attributes.get("id") == "panel-reference":
            self.fund_hot_reference_count += 1
        if tag == "details" and "overview-details" in classes:
            self.overview_details_count += 1
            if "open" in attributes:
                raise ValidationError("Collapsible overview must be closed by default")
        if tag == "a" and attributes.get("href") == "https://fund.eastmoney.com/fundhot8.html":
            self.fund_hot_link_count += 1
            rel = set((attributes.get("rel") or "").split())
            if attributes.get("target") == "_blank" and {"noopener", "noreferrer"} <= rel:
                self.fund_hot_safe_link_count += 1
        if tag == "details" and "fund-item" in classes:
            code = attributes.get("data-code")
            if not code or self._current_code is not None:
                raise ValidationError("HTML contains an invalid nested fund record")
            self._current_code = code
            self.lists.append(attributes.get("data-list") or "")
            self.routing_reasons.append(attributes.get("data-routing-reason") or "")
            self._current_text = []
        if tag == "tbody" and "premium-item" in classes:
            code = attributes.get("data-etf-code")
            if not code or self._current_premium_code is not None:
                raise ValidationError("HTML contains an invalid nested premium record")
            self._current_premium_code = code
            self._current_premium_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "details" and self._current_code is not None:
            text = " ".join(" ".join(self._current_text).split())
            self.codes.append(self._current_code)
            self.blocks[self._current_code] = text
            self._current_code = None
            self._current_text = []
        if tag == "tbody" and self._current_premium_code is not None:
            text = " ".join(" ".join(self._current_premium_text).split())
            self.premium_codes.append(self._current_premium_code)
            self.premium_blocks[self._current_premium_code] = text
            self._current_premium_code = None
            self._current_premium_text = []

    def handle_data(self, data: str) -> None:
        self.all_text.append(data)
        if self._current_code is not None:
            self._current_text.append(data)
        if self._current_premium_code is not None:
            self._current_premium_text.append(data)


def current_shanghai_date() -> str:
    return datetime.now(SHANGHAI_TZ).date().isoformat()


def parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def as_number(value: Any, label: str) -> float:
    require(isinstance(value, (int, float)), f"{label} must be numeric")
    number = float(value)
    require(math.isfinite(number), f"{label} must be finite")
    return number


def format_percentage(value: Any, show_sign: bool = False) -> str:
    number = as_number(value, "percentage")
    return f"{number:+.2f}%" if show_sign else f"{number:.2f}%"


def format_correlation(value: Any) -> str:
    return f"{as_number(value, 'correlation') * 100:.1f}%"


def format_beta(value: Any) -> str:
    return f"{as_number(value, 'beta'):.2f}"


def format_limit(limit: dict[str, Any]) -> str:
    status = limit.get("status")
    if status == "unlimited":
        return "正常开放"
    if status == "suspended":
        return "暂停申购"
    amount = limit.get("amount_cny")
    if status == "unknown" or amount is None:
        return "待核实"
    amount = int(amount)
    if amount >= 10000 and amount % 10000 == 0:
        return f"{amount // 10000:,}万元"
    return f"{amount:,}元"


def is_reportable_warning(warning: str) -> bool:
    if warning.startswith("跳过未完整披露的持有人报告期"):
        return True
    if "无法按 " in warning and "仓位仅计入可能上限" in warning:
        return True
    if "前十大基金之外尚有" in warning and "仅计入可能上限" in warning:
        return True
    if "美股占比区间" in warning and "按确认下限进入全球补充榜" in warning:
        return True
    if warning.startswith("纳指100基准更新失败，使用完整缓存："):
        return True
    if "的基金主页净值已更新至 " in warning and "已强制重新验证完整历史并按后者计算" in warning:
        return True
    if warning.startswith(
        ("合同基准告警 ", "产品概要告警 ", "持有费率告警 ", "额度剔除 ")
    ):
        return True
    if "的申购额度无法确定；引用前请核对关联公告" in warning:
        return True
    if "quota notice could not be parsed" in warning:
        return True
    if warning.startswith(("美国主榜仅 ", "全球补充榜仅 ")):
        return True
    if warning.startswith(("场内溢价告警：", "场内费率告警 ", "场外纳指100告警 ")):
        return True
    if warning in {
        "美国主榜当前没有符合全部条件的基金。",
        "全球补充榜当前没有符合全部条件的基金。",
    }:
        return True
    return False


def validate_three_year_window(record: dict[str, Any], run_date: date) -> None:
    code = record["code"]
    days = record.get("three_year_boundary_shortfall_days")
    require(type(days) is int and 0 <= days <= 7, f"{code} boundary shortfall is invalid")
    start = parse_date(str(record.get("three_year_performance_start_date")))
    end = parse_date(str(record.get("three_year_performance_end_date")))
    nav_start = parse_date(str(record.get("nav_history_start_date")))
    nav_end = parse_date(str(record.get("nav_history_end_date")))
    require(nav_start <= start < end == nav_end <= run_date, f"{code} three-year dates are invalid")
    shortfall = max(0, (start - years_ago(end, 3)).days)
    require(days == shortfall, f"{code} boundary shortfall differs from dates")
    if days:
        inception = parse_date(str(record.get("inception_date")))
        require(start == nav_start == inception, f"{code} boundary must start at inception")
        require(inception < years_ago(run_date, 3), f"{code} boundary fund is not older than three years")


def classify_warnings(warnings: Any, run_date: date | None = None) -> tuple[list[str], list[str]]:
    require(isinstance(warnings, list), "warnings must be a list")
    require(
        all(isinstance(warning, str) and warning.strip() for warning in warnings),
        "warnings must contain non-empty strings",
    )
    boundary_warnings = set()
    for warning in warnings:
        if not warning.startswith("三年边界容差 "):
            continue
        match = BOUNDARY_WARNING_RE.fullmatch(warning)
        require(match is not None and run_date is not None, "Invalid boundary warning")
        validate_three_year_window({
            "code": match["code"],
            "inception_date": match["inception"],
            "nav_history_start_date": match["start"],
            "nav_history_end_date": match["end"],
            "three_year_performance_start_date": match["start"],
            "three_year_performance_end_date": match["end"],
            "three_year_boundary_shortfall_days": int(match["days"]),
        }, run_date)
        boundary_warnings.add(warning)
    reportable = [warning for warning in warnings if warning in boundary_warnings or is_reportable_warning(warning)]
    blocking = [warning for warning in warnings if warning not in reportable]
    return reportable, blocking
