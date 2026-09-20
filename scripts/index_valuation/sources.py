"""External valuation source parsers."""

import io
import json
import math
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .common import (
    NDXTMC_LIVE_START,
    SOURCE_RATING_LABELS,
    WINDOW_MONTHS,
    TableParser,
    ValuationError,
    finite_number,
    positive_number,
)

def parse_nasdaq_history(body: bytes, ticker: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
        rows = payload["data"]["tradesTable"]["rows"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValuationError(f"{ticker} Nasdaq response has an unsupported shape") from exc
    if not isinstance(rows, list) or not rows:
        raise ValuationError(f"{ticker} Nasdaq response has no price rows")
    points: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            trading_date = datetime.strptime(str(row.get("date")), "%m/%d/%Y").date()
            close = positive_number(row.get("close"), f"{ticker} close")
        except (ValueError, ValuationError):
            continue
        points[trading_date.isoformat()] = close
    if not points:
        raise ValuationError(f"{ticker} Nasdaq response has no valid price rows")
    return [{"date": key, "close": points[key]} for key in sorted(points)]


def _store_price_point(
    points: dict[str, float], trading_date: str, close: float, label: str
) -> None:
    previous = points.get(trading_date)
    if previous is not None and not math.isclose(previous, close, rel_tol=0, abs_tol=1e-9):
        raise ValuationError(
            f"{label} contains conflicting values for {trading_date}: "
            f"{previous} and {close}"
        )
    points[trading_date] = close


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    except ET.ParseError as exc:
        raise ValuationError("NDXTMC workbook shared strings are invalid") from exc
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [
        "".join(node.text or "" for node in item.findall(".//x:t", namespace))
        for item in root.findall("x:si", namespace)
    ]


def parse_ndxtmc_workbook(body: bytes) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            shared = _xlsx_shared_strings(archive)
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    except (KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValuationError("NDXTMC official workbook is not a supported XLSX file") from exc
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[dict[str, str]] = []
    for row in sheet.findall(".//x:sheetData/x:row", namespace):
        values: dict[str, str] = {}
        for cell in row.findall("x:c", namespace):
            reference = str(cell.get("r") or "")
            column_match = re.match(r"[A-Z]+", reference)
            if not column_match:
                continue
            value_node = cell.find("x:v", namespace)
            if value_node is None or value_node.text is None:
                continue
            value = value_node.text
            if cell.get("t") == "s":
                try:
                    value = shared[int(value)]
                except (IndexError, ValueError) as exc:
                    raise ValuationError("NDXTMC workbook has an invalid shared string") from exc
            values[column_match.group(0)] = value
        if values:
            rows.append(values)
    if not rows or rows[0].get("A") != "Date" or rows[0].get("B") != "NDXTMC":
        raise ValuationError("NDXTMC workbook headers must be Date and NDXTMC")
    points: dict[str, float] = {}
    excel_epoch = date(1899, 12, 30)
    for row in rows[1:]:
        if "A" not in row or "B" not in row:
            continue
        try:
            raw_date = row["A"]
            if re.fullmatch(r"\d+(?:\.0+)?", raw_date):
                trading_date = excel_epoch + timedelta(days=int(float(raw_date)))
            else:
                trading_date = datetime.fromisoformat(raw_date).date()
            close = positive_number(row["B"], "NDXTMC workbook value")
        except (OverflowError, ValueError, ValuationError) as exc:
            raise ValuationError(f"NDXTMC workbook contains an invalid row: {row!r}") from exc
        _store_price_point(points, trading_date.isoformat(), close, "NDXTMC workbook")
    if not points:
        raise ValuationError("NDXTMC workbook contains no valid index values")
    return [{"date": key, "close": points[key]} for key in sorted(points)]


def parse_ndxtmc_history(body: bytes) -> list[dict[str, Any]]:
    try:
        rows = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValuationError("NDXTMC history response is not valid JSON") from exc
    if not isinstance(rows, list) or not rows:
        raise ValuationError("NDXTMC history response has no rows")
    points: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValuationError("NDXTMC history response contains a non-object row")
        symbol = row.get("FPSymbol")
        if symbol not in {None, "NDXTMC"}:
            raise ValuationError(f"NDXTMC history returned unexpected symbol {symbol!r}")
        try:
            timestamp = finite_number(row.get("x"), "NDXTMC timestamp")
            trading_date = datetime.fromtimestamp(timestamp / 1000, timezone.utc).date()
            close = positive_number(row.get("y"), "NDXTMC index value")
        except (OSError, OverflowError, ValueError, ValuationError) as exc:
            raise ValuationError(f"NDXTMC history contains an invalid row: {row!r}") from exc
        _store_price_point(points, trading_date.isoformat(), close, "NDXTMC history")
    return [{"date": key, "close": points[key]} for key in sorted(points)]


def merge_ndxtmc_points(*series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points: dict[str, float] = {}
    for rows in series:
        for row in rows:
            try:
                trading_date = datetime.strptime(str(row["date"]), "%Y-%m-%d").date()
                close = positive_number(row["close"], "NDXTMC normalized value")
            except (KeyError, ValueError, ValuationError) as exc:
                raise ValuationError(f"Invalid NDXTMC normalized point: {row!r}") from exc
            _store_price_point(points, trading_date.isoformat(), close, "NDXTMC merge")
    if not points:
        raise ValuationError("NDXTMC merged history is empty")
    return [{"date": key, "close": points[key]} for key in sorted(points)]


def validate_ndxtmc_boundary(
    workbook_points: list[dict[str, Any]], live_points: list[dict[str, Any]]
) -> None:
    workbook_end = datetime.strptime(workbook_points[-1]["date"], "%Y-%m-%d").date()
    live_start = datetime.strptime(live_points[0]["date"], "%Y-%m-%d").date()
    gap_days = (live_start - workbook_end).days
    if gap_days < 1 or gap_days > 4:
        raise ValuationError(
            "NDXTMC workbook/live boundary is not a normal trading-day transition: "
            f"{workbook_end.isoformat()} to {live_start.isoformat()}"
        )


def ndxtmc_workbook_matches_cache(
    workbook_points: list[dict[str, Any]], cached_points: list[dict[str, Any]]
) -> bool:
    try:
        workbook = {
            str(item["date"]): positive_number(item["close"], "NDXTMC workbook cache comparison")
            for item in workbook_points
        }
        cached = {
            str(item["date"]): positive_number(item["close"], "NDXTMC cached static comparison")
            for item in cached_points
            if datetime.strptime(str(item["date"]), "%Y-%m-%d").date() < NDXTMC_LIVE_START
        }
    except (KeyError, ValueError, ValuationError):
        return False
    return len(workbook) == len(cached) and all(
        math.isclose(value, cached.get(trading_date, math.nan), rel_tol=0, abs_tol=1e-9)
        for trading_date, value in workbook.items()
    )


def parse_normalized_ndxtmc(body: bytes) -> list[dict[str, Any]]:
    try:
        rows = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValuationError("NDXTMC normalized response is invalid JSON") from exc
    if not isinstance(rows, list):
        raise ValuationError("NDXTMC normalized response is not a list")
    return merge_ndxtmc_points(rows)


def parse_dqydj_pe(body: bytes) -> list[dict[str, Any]]:
    try:
        document = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValuationError("DQYDJ response is not UTF-8 HTML") from exc
    parser = TableParser()
    parser.feed(document)
    points: dict[str, float] = {}
    for row in parser.rows:
        if len(row) < 2:
            continue
        match = re.fullmatch(r"(0?[1-9]|1[0-2])[-/](\d{4})", row[0].strip())
        if not match:
            continue
        month = f"{match.group(2)}-{int(match.group(1)):02d}"
        candidates = row[3:4] if len(row) >= 4 else row[-1:]
        try:
            points[month] = positive_number(candidates[0], "S&P 500 PE")
        except ValuationError:
            continue
    if len(points) < WINDOW_MONTHS:
        raise ValuationError(
            f"DQYDJ response has only {len(points)} valid monthly PE observations"
        )
    return [{"month": key, "pe_ttm": points[key]} for key in sorted(points)]


def parse_snowball_snapshot(
    body: bytes, expected_codes: set[str]
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
        rows = payload["data"]["items"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValuationError("雪球指数估值响应结构不受支持") from exc
    normalized: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("index_code")) not in expected_codes:
            continue
        code = str(row["index_code"])
        rating_code = str(row.get("eva_type", "")).lower()
        timestamp = int(finite_number(row.get("ts"), f"{code} timestamp"))
        begin_at = int(finite_number(row.get("begin_at"), f"{code} begin timestamp"))
        normalized.append(
            {
                "code": code,
                "name": str(row.get("name") or code),
                "pe_ttm": positive_number(row.get("pe"), f"{code} PE"),
                "pe_percentile_10y": finite_number(
                    row.get("pe_percentile"), f"{code} PE percentile"
                )
                * 100,
                "pb_mrq": positive_number(row.get("pb"), f"{code} PB"),
                "pb_percentile_10y": finite_number(
                    row.get("pb_percentile"), f"{code} PB percentile"
                )
                * 100,
                "roe_pct": finite_number(row.get("roe"), f"{code} ROE") * 100,
                "dividend_yield_pct": finite_number(
                    row.get("yeild"), f"{code} dividend yield"
                )
                * 100,
                "as_of": datetime.fromtimestamp(timestamp / 1000, timezone.utc)
                .date()
                .isoformat(),
                "history_since": datetime.fromtimestamp(begin_at / 1000, timezone.utc)
                .date()
                .isoformat(),
                "source_rating": {
                    "code": rating_code,
                    "label": SOURCE_RATING_LABELS.get(rating_code, rating_code or "未提供"),
                    "provider": "雪球",
                },
            }
        )
    found = {item["code"] for item in normalized}
    if found != expected_codes:
        raise ValuationError(
            f"雪球指数估值缺少白名单项目：{', '.join(sorted(expected_codes - found))}"
        )
    return sorted(normalized, key=lambda item: item["code"])


def _match_text(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        raise ValuationError(f"黄金估值页面缺少{label}")
    return match.group(1)


def parse_gold_snapshot(body: bytes, fetched_on: date) -> dict[str, Any]:
    try:
        document = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValuationError("黄金估值页面不是 UTF-8 HTML") from exc
    document = re.sub(r"<img\b[^>]*>", " ", document, flags=re.IGNORECASE)
    parser = TableParser()
    parser.feed(document)
    text = " ".join(" ".join(parser.text).split())
    updated = _match_text(r"更新于\s*(\d{4}-\d{2}-\d{2})", text, "更新时间")
    spot = positive_number(
        _match_text(r"最新金价\s*([0-9,.]+)\s*美元", text, "最新金价"),
        "gold spot",
    )
    tips = finite_number(_match_text(r"TIPS:\s*([+-]?[0-9.]+)%", text, "TIPS"), "TIPS")
    spread = finite_number(
        _match_text(r"中美利差:\s*([+-]?[0-9.]+)%", text, "中美利差"),
        "China-US spread",
    )
    gold_oil = positive_number(
        _match_text(r"金油比:\s*([0-9.]+)", text, "金油比"), "gold-oil ratio"
    )
    residual = finite_number(
        _match_text(r"当前残差为\s*([+-]?[0-9.]+)", text, "当前残差"),
        "gold residual",
    )
    labels = {
        "1y": "近1年",
        "3y": "近3年",
        "5y": "近5年",
        "10y": "近10年",
        "all": "全部历史",
    }
    percentiles: dict[str, float] = {}
    ratings: dict[str, str] = {}
    for key, source_label in labels.items():
        match = re.search(
            rf"{source_label}\s*([0-9.]+)%\s*([^\s]+)", text, re.IGNORECASE
        )
        if not match:
            raise ValuationError(f"黄金估值页面缺少{source_label}分位")
        percentiles[key] = finite_number(match.group(1), f"gold {key} percentile")
        ratings[key] = match.group(2)

    def factor_date(label: str) -> str:
        return _match_text(rf"{label}\s*(\d{{4}}-\d{{2}}-\d{{2}})", text, label)

    tips_date = factor_date("TIPS 实际利率")
    cn_date = factor_date("中国10年期国债")
    us_date = factor_date("美国10年期国债")
    gold_oil_date = factor_date("金油比")
    spread_date = min(cn_date, us_date)

    def lag_days(value: str) -> int:
        return max(0, (fetched_on - datetime.strptime(value, "%Y-%m-%d").date()).days)

    return {
        "as_of": updated,
        "spot_usd_oz": spot,
        "percentiles": percentiles,
        "source_ratings": ratings,
        "residual": residual,
        "factors": {
            "tips_real_yield": {
                "label": "TIPS 实际利率",
                "value": tips,
                "unit": "%",
                "date": tips_date,
                "lag_days": lag_days(tips_date),
            },
            "china_us_spread": {
                "label": "中美利差",
                "value": spread,
                "unit": "%",
                "date": spread_date,
                "lag_days": lag_days(spread_date),
            },
            "gold_oil_ratio": {
                "label": "金油比",
                "value": gold_oil,
                "unit": "ratio",
                "date": gold_oil_date,
                "lag_days": lag_days(gold_oil_date),
            },
        },
    }


def monthly_average(points: list[dict[str, Any]]) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for point in points:
        try:
            parsed = datetime.strptime(str(point["date"]), "%Y-%m-%d").date()
            close = positive_number(point["close"], "normalized close")
        except (KeyError, ValueError, ValuationError) as exc:
            raise ValuationError(f"Invalid normalized price point: {point!r}") from exc
        grouped.setdefault(parsed.strftime("%Y-%m"), []).append(close)
    return {month: sum(values) / len(values) for month, values in grouped.items()}


def validate_price_points(
    points: list[dict[str, Any]], ticker: str, anchor_months: set[str]
) -> None:
    averages = monthly_average(points)
    if len(averages) < WINDOW_MONTHS:
        raise ValuationError(
            f"{ticker} cache has only {len(averages)} monthly price observations"
        )
    missing = anchor_months - set(averages)
    if missing:
        raise ValuationError(f"{ticker} cache lacks anchor months {sorted(missing)}")


def validate_pe_points(points: list[dict[str, Any]], anchor_months: set[str]) -> None:
    months: set[str] = set()
    for point in points:
        month = str(point.get("month", ""))
        if not re.fullmatch(r"\d{4}-\d{2}", month) or month in months:
            raise ValuationError("PE cache contains an invalid or duplicate month")
        positive_number(point.get("pe_ttm"), "cached S&P 500 PE")
        months.add(month)
    if len(months) < WINDOW_MONTHS or not anchor_months.issubset(months):
        raise ValuationError("PE cache is incomplete or lacks a proxy anchor month")


def merge_price_points(
    cached: list[dict[str, Any]], refreshed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = {str(item["date"]): float(item["close"]) for item in cached}
    merged.update({str(item["date"]): float(item["close"]) for item in refreshed})
    return [{"date": key, "close": merged[key]} for key in sorted(merged)]
