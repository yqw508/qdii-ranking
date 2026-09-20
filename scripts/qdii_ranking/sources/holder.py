"""Holder-period and holder-row source adapter."""

from __future__ import annotations

import math
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..cache.base import parse_cache_date as parse_source_date
from ..config import HOLDER_API_URL
from ..errors import DataError
from ..models import HolderPeriod
from .fund import extract_data_array


def period_key(report_date: str) -> str:
    parsed = parse_source_date(report_date)
    if parsed.month == 6:
        quarter = 2
    elif parsed.month == 12:
        quarter = 4
    else:
        raise DataError(f"Unsupported holder report date: {report_date}")
    return f"{parsed.year}_{quarter}"





def extract_page_count(payload: str) -> int:
    match = re.search(r'pages:"(\d+)"', payload)
    if not match:
        raise DataError("Could not locate page count in Eastmoney response")
    return int(match.group(1))







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
