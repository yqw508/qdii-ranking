"""HTML and exchange-premium validation."""

import math
import re
import urllib.parse
from datetime import date, datetime
from typing import Any

from .common import (
    ETF_MARKET_LIST_API_URL,
    ETF_QUOTE_API_URL,
    EXPECTED_PREMIUM_CODES,
    EXPECTED_PREMIUM_GROUP_ORDER,
    RankingHtmlParser,
    ValidationError,
    as_number,
    format_beta,
    format_correlation,
    format_limit,
    format_percentage,
    mailer,
    parse_date,
    require,
)
from .schema import validate_holding_cost
from .otc_shares import validate_share_evidence_html

def parse_html(document: str) -> RankingHtmlParser:
    parser = RankingHtmlParser()
    try:
        parser.feed(document)
        parser.close()
    except (ValidationError, ValueError) as exc:
        raise ValidationError(f"Could not parse ranking HTML: {exc}") from exc
    return parser


def premium_band(value: float | None) -> str:
    if value is None:
        return "--"
    if value < 0:
        return "折价"
    if value <= 2:
        return "0–2%"
    if value <= 5:
        return "2–5%"
    return ">5%高溢价"


def format_premium_value(value: Any) -> str:
    if value is None:
        return "--"
    numeric = as_number(value, "premium value")
    return f"{numeric:+.2f}%"


def format_premium_turnover(value: Any) -> str:
    if value is None:
        return "--"
    numeric = as_number(value, "premium turnover")
    if numeric >= 100_000_000:
        return f"{numeric / 100_000_000:.2f}亿元"
    if numeric >= 10_000:
        return f"{numeric / 10_000:.0f}万元"
    return f"{numeric:.0f}元"


def _premium_catalog_context(
    section: dict[str, Any], run_date: date
) -> tuple[str, int, bool, list[dict[str, Any]], list[str], dict[str, int]]:
    require(section.get("schema_version") == 2, "Unexpected exchange premium schema")
    status = section.get("status")
    require(status in {"fresh", "partial", "stale", "unavailable"}, "Invalid exchange premium status")
    require(section.get("quote_delay_minutes") == 15, "Unexpected ETF quote delay")
    expected_count = section.get("expected_count")
    require(isinstance(expected_count, int) and expected_count >= 0, "ETF premium expected count is invalid")
    groups = section.get("group_order")
    require(
        isinstance(groups, list) and groups
        and all(isinstance(group, str) and group for group in groups),
        "ETF premium group order is invalid",
    )
    try:
        requested = datetime.fromisoformat(str(section.get("requested_at", "")))
    except ValueError as exc:
        raise ValidationError("ETF premium requested_at is invalid") from exc
    require(requested.date() == run_date, "ETF premium request date differs from ranking date")
    require(
        isinstance(section.get("catalog_fingerprint"), str)
        and re.fullmatch(r"[0-9a-f]{64}", section["catalog_fingerprint"]) is not None,
        "ETF premium catalog fingerprint is invalid",
    )
    parsed_url = urllib.parse.urlparse(str(section.get("refresh_url", "")))
    refresh_base = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
    query = urllib.parse.parse_qs(parsed_url.query)
    if refresh_base == ETF_QUOTE_API_URL:
        dynamic = False
        secids = query.get("secids", [""])[0].split(",")
        refresh_codes = {item.split(".", 1)[-1] for item in secids if "." in item}
        require(refresh_codes == EXPECTED_PREMIUM_CODES, "ETF premium static codes differ")
        require(groups == list(EXPECTED_PREMIUM_GROUP_ORDER), "ETF premium static group order differs")
    else:
        dynamic = True
        refresh_codes = set()
        require(refresh_base == ETF_MARKET_LIST_API_URL, "ETF premium refresh URL differs")
        require(query.get("fs") == ["m:1+t:9,m:0+t:10"], "ETF premium market filter differs")
        require(query.get("pn") == ["1"], "ETF premium refresh must start at page one")
        require(query.get("pz") == ["100"], "ETF premium refresh page size differs")
        require(query.get("fid") == ["f12"], "ETF premium refresh sort field differs")
        require("secids" not in query, "ETF premium market refresh must not use a static code list")
        discovered = section.get("discovered_count")
        filtered = section.get("filtered_unavailable_count")
        require(isinstance(discovered, int) and discovered >= expected_count, "ETF premium discovered count is invalid")
        require(isinstance(filtered, int) and filtered == discovered - expected_count, "ETF premium filtered count differs")
        require(
            all(group.startswith("QDII") or group == "指数型-海外股票" for group in groups),
            "ETF premium dynamic groups are outside the QDII scope",
        )
    records = section.get("records")
    require(isinstance(records, list) and len(records) == expected_count, f"ETF premium record count differs from expected count ({expected_count} records)")
    codes = [str(record.get("code", "")) for record in records if isinstance(record, dict)]
    require(len(codes) == expected_count and len(set(codes)) == expected_count, "ETF premium codes differ")
    if refresh_codes:
        require(set(codes) == refresh_codes, "ETF premium refresh URL codes differ")
    return status, expected_count, dynamic, records, codes, {
        group: index for index, group in enumerate(groups)
    }


def _validate_premium_reference(
    record: dict[str, Any], code: str, reference_type: str, reference_value: float, run_date: date
) -> None:
    if reference_type == "iopv":
        iopv = as_number(record.get("iopv_cny"), f"ETF {code} IOPV")
        require(math.isclose(iopv, reference_value, rel_tol=0, abs_tol=1e-8), f"ETF {code} IOPV reference differs")
        require(record.get("reference_value_date") is None, f"ETF {code} IOPV reference date must be null")
        return
    require(record.get("iopv_cny") is None, f"ETF {code} LOF must not contain IOPV")
    reference_date = parse_date(str(record.get("reference_value_date", "")))
    require(reference_date <= run_date, f"ETF {code} NAV reference contains future data")
    require(str(record.get("reference_value_source_url") or "").startswith("https://"), f"ETF {code} NAV reference source is invalid")


def _validate_premium_record(
    record: dict[str, Any], run_date: date, dynamic: bool, group_order: dict[str, int]
) -> str:
    require(isinstance(record, dict), "ETF premium records must be objects")
    code = record["code"]
    require(isinstance(record.get("name"), str) and record["name"].strip(), f"ETF {code} name is missing")
    exchange = record.get("exchange")
    require(exchange in {"SSE", "SZSE"}, f"ETF {code} exchange is invalid")
    require((exchange == "SSE") == (record.get("market_id") == 1), f"ETF {code} market ID differs")
    group = record.get("benchmark_group")
    category = record.get("category")
    require(group in group_order, f"ETF {code} benchmark group is invalid")
    require(category in {"broad_market", "sector_theme", "qdii"}, f"ETF {code} category is invalid")
    if dynamic:
        fund_type = record.get("fund_type")
        require(category == "qdii", f"ETF {code} is not a dynamic QDII record")
        require(
            fund_type == group and isinstance(fund_type, str)
            and (fund_type.startswith("QDII") or fund_type == "指数型-海外股票"),
            f"ETF {code} is outside the QDII fund-type scope",
        )
    if category in {"broad_market", "sector_theme"}:
        require((group == "行业主题") == (category == "sector_theme"), f"ETF {code} category and group differ")
    require(str(record.get("source_url", "")).startswith("https://"), f"ETF {code} source URL is invalid")
    validate_holding_cost(record.get("holding_cost"), f"ETF {code}", run_date, allow_stale=True)
    quote_status = record.get("quote_status")
    require(quote_status in {"fresh", "stale", "unavailable"}, f"ETF {code} quote status is invalid")
    if dynamic:
        require(quote_status in {"fresh", "stale"}, f"ETF {code} unavailable quote was not filtered")
    value_fields = (
        "market_price_cny", "iopv_cny", "reference_value_type", "reference_value_cny",
        "reference_value_date", "reference_value_source_url", "source_discount_pct",
        "premium_pct", "change_pct", "turnover_cny", "quote_date", "updated_at",
    )
    if quote_status == "unavailable":
        require(all(record.get(field) is None for field in value_fields), f"ETF {code} unavailable quote has values")
        return quote_status
    price = as_number(record.get("market_price_cny"), f"ETF {code} price")
    reference_type = record.get("reference_value_type")
    require(reference_type in {"iopv", "nav"}, f"ETF {code} reference value type is invalid")
    reference_value = as_number(record.get("reference_value_cny"), f"ETF {code} reference value")
    _validate_premium_reference(record, code, reference_type, reference_value, run_date)
    source_discount = as_number(record.get("source_discount_pct"), f"ETF {code} discount")
    premium = as_number(record.get("premium_pct"), f"ETF {code} premium")
    as_number(record.get("change_pct"), f"ETF {code} change")
    turnover = as_number(record.get("turnover_cny"), f"ETF {code} turnover")
    require(price > 0 and reference_value > 0 and turnover >= 0, f"ETF {code} quote values are outside range")
    require(abs(source_discount + premium) <= 0.011, f"ETF {code} discount/premium sign differs")
    calculated = (price / reference_value - 1) * 100
    tolerance = max(0.05, 0.0005 / reference_value * 100 + 0.01)
    require(abs(premium - calculated) <= tolerance, f"ETF {code} premium differs from price/{reference_type.upper()}")
    try:
        quote_date = parse_date(str(record.get("quote_date", "")))
        updated_at = datetime.fromisoformat(str(record.get("updated_at", "")))
    except ValueError as exc:
        raise ValidationError(f"ETF {code} quote timestamp is invalid") from exc
    require(quote_date <= run_date and updated_at.date() <= run_date, f"ETF {code} quote contains future data")
    require(str(record.get("quote_source_url", "")).startswith("https://"), f"ETF {code} quote URL is invalid")
    return quote_status


def validate_exchange_premium(section: Any, run_date: date) -> list[dict[str, Any]]:
    require(isinstance(section, dict), "exchange_premium must be an object")
    status, expected_count, dynamic, records, codes, group_order = _premium_catalog_context(section, run_date)
    quote_statuses = [
        _validate_premium_record(record, run_date, dynamic, group_order)
        for record in records
    ]
    fresh_count = quote_statuses.count("fresh")
    stale_count = quote_statuses.count("stale")
    expected_order = sorted(
        records,
        key=lambda item: (
            item.get("premium_pct") is None,
            -float(item["premium_pct"]) if item.get("premium_pct") is not None else math.inf,
            item["code"],
        ),
    )
    require(codes == [item["code"] for item in expected_order], "ETF premium record order differs")
    require(section.get("fresh_count") == fresh_count, "ETF premium fresh count differs")
    require(section.get("cache_hit_count") == stale_count, "ETF premium cache-hit count differs")
    expected_status = (
        "fresh" if expected_count > 0 and fresh_count == expected_count else
        "partial" if fresh_count else "stale" if stale_count else "unavailable"
    )
    require(status == expected_status, "ETF premium aggregate status differs")
    return records



def validate_html_document(
    document: str, payload: dict[str, Any], records: list[dict[str, Any]], label: str
) -> None:
    parser = parse_html(document)
    require(payload["run_date"] in " ".join(parser.all_text), f"{label} run date differs")
    expected_codes = [record["code"] for record in records]
    require(parser.codes == expected_codes, f"{label} fund order differs from JSON")
    require(
        parser.lists == [record["ranking_list"] for record in records],
        f"{label} ranking-list assignments differ from JSON",
    )
    require(
        parser.routing_reasons == [record["routing_reason"] for record in records],
        f"{label} routing reasons differ from JSON",
    )
    all_text = " ".join(parser.all_text)
    for warning in payload["warnings"]:
        if warning.startswith("三年边界容差 "):
            require(warning in all_text, f"{label} boundary warning is missing")
    require(
        "美国主榜" in all_text and "全球补充榜" in all_text and "场外纳指100" in all_text and "场内溢价" in all_text,
        f"{label} tabs are missing",
    )
    nasdaq_records = (payload.get("nasdaq100_otc") or {}).get("records") or []
    require(
        document.count('id="tab-nasdaq100-otc"') == 1
        and document.count('id="panel-nasdaq100-otc"') == 1,
        f"{label} OTC Nasdaq-100 tab is missing or duplicated",
    )
    for record in nasdaq_records:
        validate_share_evidence_html(document, record)
        require(
            record["code"] in all_text and record["name"] in all_text,
            f"{label} OTC Nasdaq-100 record is missing for {record['code']}",
        )
    require(
        parser.nasdaq_item_count == len(nasdaq_records),
        f"{label} OTC Nasdaq-100 detail rows differ",
    )
    premium_records = payload["exchange_premium"]["records"]
    require(
        parser.premium_codes == [record["code"] for record in premium_records],
        f"{label} ETF premium order differs from JSON",
    )
    require(parser.premium_tab_count == 1, f"{label} premium tab is missing or duplicated")
    require(parser.refresh_button_count == 1, f"{label} premium refresh button is missing or duplicated")
    require(parser.premium_table_count == 1, f"{label} must contain one premium table")
    require(parser.premium_toggle_count == len(premium_records), f"{label} ETF detail toggles differ")
    require(parser.valuation_link_count == 1, f"{label} valuation-page entry is missing or duplicated")
    require("估值代理" in all_text, f"{label} valuation-page label is missing")
    require(parser.reference_tab_count == 1, f"{label} platform-reference tab is missing or duplicated")
    require(parser.fund_hot_reference_count == 1, f"{label} platform-reference panel is missing or duplicated")
    require(parser.fund_hot_link_count == 1, f"{label} fund-hot external link is missing or duplicated")
    require(parser.fund_hot_safe_link_count == 1, f"{label} fund-hot external link lacks safe attributes")
    require(parser.overview_details_count == 1, f"{label} collapsible overview is missing or duplicated")
    require(parser.all_text.count("筛选与数据概览") == 1, f"{label} overview toggle label is missing or duplicated")
    require(parser.all_text.count("平台参考") == 1, f"{label} platform-reference tab label is missing or duplicated")
    require(parser.all_text.count("第三方平台参考") == 1, f"{label} fund-hot reference label is missing or duplicated")
    require(parser.all_text.count("天天基金月销量总榜") == 1, f"{label} fund-hot title is missing or duplicated")
    require(
        document.index('class="tabs"') < document.index('id="panel-reference"'),
        f"{label} platform-reference panel must follow the tabs",
    )
    footer = re.search(r"<footer\b.*?</footer>", document, re.S)
    require(footer is not None, f"{label} footer is missing")
    require("fundhot8.html" not in footer.group(0), f"{label} footer still contains the fund-hot link")
    require("约15分钟" in all_text or "约 15 分钟" in all_text, f"{label} quote delay disclosure is missing")
    for record in premium_records:
        code = record["code"]
        block = parser.premium_blocks[code]
        expected = [
            record["name"],
            code,
            record["exchange"],
            record["benchmark_group"],
            (
                "--"
                if record["market_price_cny"] is None
                else f"{float(record['market_price_cny']):.3f}"
            ),
            (
                f"最新单位净值（{record['reference_value_date']}）"
                if record["reference_value_type"] == "nav"
                else "IOPV"
            ),
            f"{float(record['reference_value_cny']):.4f}",
            format_premium_value(record["premium_pct"]),
            premium_band(record["premium_pct"]),
            format_premium_value(record["change_pct"]),
            format_premium_turnover(record["turnover_cny"]),
            mailer.format_holding_cost(record["holding_cost"]),
            str(record["holding_cost"].get("source_published_date") or "--"),
            (
                "--"
                if not record["updated_at"]
                else str(record["updated_at"])[:16].replace("T", " ")
            ),
        ]
        if record["quote_status"] == "stale":
            expected.append("旧值")
        elif record["quote_status"] == "unavailable":
            expected.append("暂无行情")
        if record["holding_cost"]["status"] == "stale":
            expected.append("旧值")
        if record["holding_cost"].get("source_url"):
            expected.append("查看费率来源")
        if record["reference_value_type"] == "nav":
            expected.append("查看净值来源")
        require(all(value in block for value in expected), f"{label} ETF premium metrics differ for {code}")
    for record in records:
        code = record["code"]
        block = parser.blocks[code]
        require(mailer.format_three_year_boundary(record) in block, f"{label} boundary differs for {code}")
        expected_values = [
            record["name"],
            mailer.format_routing_reason(record["routing_reason"]),
            record["contract_benchmark"]["benchmark_name"],
            record["contract_benchmark"]["benchmark_text"],
            f"{float(record['scale_billion_cny']):.2f} 亿元",
            format_percentage(record["one_year_return_pct"], show_sign=True),
            format_percentage(record["one_year_max_drawdown_pct"]),
            format_percentage(record["three_year_return_pct"], show_sign=True),
            format_percentage(record["three_year_max_drawdown_pct"]),
            (
                format_percentage(record["five_year_return_pct"], show_sign=True)
                if record["five_year_return_pct"] is not None
                else f"--（净值始于 {record['nav_history_start_date']}）"
            ),
            (
                format_percentage(record["ten_year_return_pct"], show_sign=True)
                if record["ten_year_return_pct"] is not None
                else f"--（净值始于 {record['nav_history_start_date']}）"
            ),
            mailer.format_holding_cost(record["holding_cost"]),
            format_percentage(record["us_equity_exposure"]["confirmed_pct"]),
            format_percentage(record["us_equity_exposure"]["possible_pct"]),
            format_limit(record["direct_limit"]),
            format_limit(record["agency_limit"]),
            *record["product_structure_tags"],
        ]
        if record["ranking_list"] == "us_main":
            expected_values.extend(
                (
                    format_correlation(record["nasdaq100_fit"]["correlation"]),
                    format_beta(record["nasdaq100_fit"]["beta"]),
                    format_percentage(record["nasdaq100_fit"]["tracking_error_pct"]),
                    f"{record['nasdaq100_fit']['observations']} 周",
                )
            )
        else:
            ratio = "∞" if record["return_drawdown_ratio"] is None else f"{record['return_drawdown_ratio']:.2f}"
            expected_values.extend(
                (
                    format_percentage(record["three_year_annualized_return_pct"], show_sign=True),
                    ratio,
                )
            )
        require(all(value in block for value in expected_values), f"{label} metrics differ for {code}")
