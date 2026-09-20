"""Fund-list and fund-page source adapter."""

from __future__ import annotations

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..config import FUND_LIST_URL, FUND_PAGE_URL
from ..errors import DataError


def _parse_cny_amount(value: str, unit: str) -> int:
    number = float(value.replace(",", ""))
    if unit == "万元":
        number *= 10000
    return int(round(number))


def extract_data_array(payload: str) -> list[list[str]]:
    match = re.search(r"data:(\[.*?\]),record:", payload, re.S)
    if not match:
        raise DataError("Could not locate data array in Eastmoney response")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise DataError(f"Could not parse Eastmoney data array: {exc}") from exc



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
        page_limit = _parse_cny_amount(page_limit_match.group(1), page_limit_match.group(2))
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
