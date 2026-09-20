"""Nasdaq and SAFE benchmark source adapters."""

from __future__ import annotations

import html
import math
import re
import urllib.parse
from datetime import date, datetime, timezone
from typing import Any

from ..cache.base import parse_cache_date as parse_source_date
from ..config import (
    NASDAQ100_HISTORY_DATA_URL,
    NASDAQ100_HISTORY_PAGE_URL,
    SAFE_USD_CNY_HISTORY_URL,
)
from ..errors import DataError


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
            observed = parse_source_date(cells[0])
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
