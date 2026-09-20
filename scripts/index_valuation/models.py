"""Valuation models and asset assembly."""

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Callable

from .common import (
    DQYDJ_SOURCE_ID,
    FetchResponse,
    GOLD_SOURCE_ID,
    NDXTMC_SOURCE_ID,
    SHANGHAI_TZ,
    SNOWBALL_SOURCE_ID,
    ValuationError,
    WINDOW_MONTHS,
    add_months,
    source_cache_id,
)
from .sources import (
    merge_price_points,
    monthly_average,
    parse_dqydj_pe,
    parse_gold_snapshot,
    parse_nasdaq_history,
    parse_normalized_ndxtmc,
    parse_snowball_snapshot,
)

def run_parallel_requests(
    requests: dict[str, Callable[[], FetchResponse]],
) -> tuple[dict[str, FetchResponse], dict[str, str], float]:
    responses: dict[str, FetchResponse] = {}
    failures: dict[str, str] = {}
    started = time.perf_counter()
    with ThreadPoolExecutor(
        max_workers=len(requests), thread_name_prefix="valuation-source"
    ) as pool:
        futures = {
            pool.submit(fetcher): source_id
            for source_id, fetcher in requests.items()
        }
        for future in as_completed(futures):
            source_id = futures[future]
            try:
                responses[source_id] = future.result()
            except Exception as exc:  # Independently handled with cache or unavailable state.
                failures[source_id] = str(exc)
    return responses, failures, time.perf_counter() - started


def select_contiguous_window(
    target: dict[str, float], baseline: dict[str, float], pe: dict[str, float], through: str
) -> list[str]:
    eligible = sorted(month for month in set(target) & set(baseline) & set(pe) if month <= through)
    for end in reversed(eligible):
        expected = [add_months(end, offset) for offset in range(1 - WINDOW_MONTHS, 1)]
        if all(month in target and month in baseline and month in pe for month in expected):
            return expected
    raise ValuationError(
        f"Sources do not contain {WINDOW_MONTHS} consecutive complete common months"
    )


def percentile_midrank(values: list[float], current: float) -> float:
    below = sum(value < current for value in values)
    equal = sum(value == current for value in values)
    return 100.0 * (below + 0.5 * equal) / len(values)


def quantile(values: list[float], probability: float) -> float:
    if not values or not 0 <= probability <= 1:
        raise ValueError("Invalid quantile input")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def iso_age_hours(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return round(max(0.0, (now - parsed.astimezone(now.tzinfo)).total_seconds() / 3600), 2)


def _parse_response(
    source_id: str,
    response: FetchResponse,
    cached: dict[str, Any] | None,
    catalog: dict[str, Any],
    as_of: date,
    refresh_mode: str,
) -> Any:
    if response.status == 304:
        if cached is None:
            raise ValuationError("source returned 304 without a valid cache")
        return cached["data"]
    if source_id == SNOWBALL_SOURCE_ID:
        expected = {
            asset["source_code"]
            for asset in catalog["assets"]
            if asset["source_mode"] == "direct"
        }
        return parse_snowball_snapshot(response.body, expected)
    if source_id == GOLD_SOURCE_ID:
        return parse_gold_snapshot(response.body, as_of)
    if source_id == DQYDJ_SOURCE_ID:
        return parse_dqydj_pe(response.body)
    if source_id == NDXTMC_SOURCE_ID:
        return parse_normalized_ndxtmc(response.body)
    ticker = source_id.removeprefix("nasdaq-").upper()
    refreshed = parse_nasdaq_history(response.body, ticker)
    if cached is not None:
        # Nasdaq limits each full response to a rolling ten-year horizon.  A
        # month-boundary full scan can therefore start one month later than a
        # lagging DQYDJ series.  Merge instead of replacing the cache so the
        # preceding run's boundary observations remain available for the
        # 120-month common window (and retain the same behavior for tails).
        return merge_price_points(cached["data"], refreshed)
    if refresh_mode == "tail":
        raise ValuationError(f"{ticker} tail response has no full cache to merge")
    return refreshed


def _source_name(source_id: str, catalog: dict[str, Any]) -> str:
    if source_id == NDXTMC_SOURCE_ID:
        return catalog["sources"]["ndxtmc"]["name"]
    if source_id.startswith("nasdaq-"):
        return f"Nasdaq {source_id.removeprefix('nasdaq-').upper()} 历史行情"
    return catalog["sources"][source_id]["name"]


def _source_page_url(source_id: str, catalog: dict[str, Any]) -> str:
    if source_id == NDXTMC_SOURCE_ID:
        return catalog["sources"]["ndxtmc"]["page_url"]
    if source_id.startswith("nasdaq-"):
        ticker = source_id.removeprefix("nasdaq-")
        return catalog["sources"]["nasdaq"]["page_url"].format(ticker=ticker)
    source = catalog["sources"][source_id]
    return source.get("page_url", source["url"])


def _asset_status(source_ids: list[str], source_states: dict[str, str]) -> str:
    statuses = [source_states.get(source_id, "unavailable") for source_id in source_ids]
    if any(status == "unavailable" for status in statuses):
        return "unavailable"
    if any(status == "cached_stale" for status in statuses):
        return "cached_stale"
    return "fresh"


def _unavailable_asset(config: dict[str, Any], source_ids: list[str], warning: str) -> dict[str, Any]:
    return {
        "id": config["id"],
        "asset_class": config["asset_class"],
        "source_mode": config["source_mode"],
        "name": config["name"],
        "code": config["code"],
        "region": config["region"],
        "frequency": "monthly" if config["source_mode"] == "proxy" else "snapshot",
        "status": "unavailable",
        "as_of": None,
        "current": {},
        "history": [],
        "history_status": "unavailable",
        "method": {"id": config.get("method_id", "snowball_index_eva_snapshot_v1")},
        "source_ids": source_ids,
        "warnings": [warning],
    }


def build_direct_asset(
    config: dict[str, Any], data: Any, source_states: dict[str, str]
) -> dict[str, Any]:
    source_ids = [SNOWBALL_SOURCE_ID]
    status = _asset_status(source_ids, source_states)
    if status == "unavailable" or not isinstance(data, list):
        return _unavailable_asset(config, source_ids, "雪球当前快照不可用且无有效缓存。")
    item = next((row for row in data if row.get("code") == config["source_code"]), None)
    if item is None:
        return _unavailable_asset(config, source_ids, "雪球当前快照缺少该指数。")
    current = {
        key: round(float(item[key]), 4)
        for key in (
            "pe_ttm",
            "pe_percentile_10y",
            "pb_mrq",
            "pb_percentile_10y",
            "roe_pct",
            "dividend_yield_pct",
        )
    }
    current["source_rating"] = item["source_rating"]
    current["history_since"] = item["history_since"]
    return {
        "id": config["id"],
        "asset_class": config["asset_class"],
        "source_mode": config["source_mode"],
        "name": config["name"],
        "code": config["code"],
        "region": config["region"],
        "frequency": "snapshot",
        "status": status,
        "as_of": item["as_of"],
        "current": current,
        "history": [],
        "history_status": "not_provided_by_source",
        "method": {
            "id": "snowball_index_eva_snapshot_v1",
            "label": "雪球指数估值当前快照",
            "limitations": [
                "10 年百分位由雪球提供，本页不重新计算。",
                "来源未提供可发布的历史估值序列，因此不展示曲线。",
            ],
        },
        "source_ids": source_ids,
        "warnings": [],
    }


def build_proxy_asset(
    config: dict[str, Any], data: dict[str, Any], source_states: dict[str, str], through: str
) -> dict[str, Any]:
    target_id = source_cache_id(config["ticker"])
    baseline_id = source_cache_id(config["baseline_ticker"])
    source_ids = [target_id, baseline_id, DQYDJ_SOURCE_ID]
    status = _asset_status(source_ids, source_states)
    if status == "unavailable" or any(data.get(source_id) is None for source_id in source_ids):
        return _unavailable_asset(config, source_ids, "代理模型的行情或盈利数据不可用且无有效缓存。")
    target = monthly_average(data[target_id])
    baseline = monthly_average(data[baseline_id])
    pe = {str(item["month"]): float(item["pe_ttm"]) for item in data[DQYDJ_SOURCE_ID]}
    anchor = config["anchor"]
    anchor_month = anchor["month"]
    try:
        anchor_ratio = target[anchor_month] / baseline[anchor_month]
        calibration = pe[anchor_month] * anchor_ratio / float(anchor["pe_ttm"])
    except (KeyError, ZeroDivisionError) as exc:
        return _unavailable_asset(config, source_ids, f"代理锚点 {anchor_month} 无法复现。")
    if not math.isfinite(calibration) or calibration <= 0:
        return _unavailable_asset(config, source_ids, "代理锚点生成了无效校准常数。")
    try:
        months = select_contiguous_window(target, baseline, pe, through)
    except ValuationError as exc:
        return _unavailable_asset(config, source_ids, str(exc))
    values: list[float] = []
    history: list[dict[str, Any]] = []
    for month in months:
        value = round(pe[month] * (target[month] / baseline[month]) / calibration, 4)
        values.append(value)
        history.append({"month": month, "proxy_pe_ttm": value})
    current_value = values[-1]
    method = {
        "id": config["method_id"],
        "label": f"{config['ticker']}/SPY 相对价格校准代理",
        "formula": (
            f"PE_proxy_m = S&P500_PE_m × ({config['ticker']}_m / SPY_m) / K"
        ),
        "price_aggregation": "calendar_month_mean_close",
        "percentile": "midrank_120_complete_months",
        "quantile": "linear_interpolation_inclusive",
        "definition_url": config["definition_url"],
        "experimental": bool(config.get("experimental", False)),
        "anchor": {
            **anchor,
            "sp500_pe_ttm": round(pe[anchor_month], 6),
            "relative_price_ratio": round(anchor_ratio, 8),
            "calibration_constant": round(calibration, 8),
            "reproduced_pe_ttm": round(
                pe[anchor_month] * anchor_ratio / calibration, 6
            ),
        },
        "limitations": [
            *config["limitations"],
            "结果不构成投资建议，也不输出低估或高估判断。",
        ],
    }
    return {
        "id": config["id"],
        "asset_class": config["asset_class"],
        "source_mode": config["source_mode"],
        "name": config["name"],
        "code": config["code"],
        "region": config["region"],
        "frequency": "monthly",
        "status": status,
        "as_of": months[-1],
        "current": {
            "proxy_pe_ttm": current_value,
            "proxy_percentile_10y": round(
                percentile_midrank(values, current_value), 2
            ),
            "sample_count": len(values),
            "reference_levels": {
                "p30": round(quantile(values, 0.30), 4),
                "p50": round(quantile(values, 0.50), 4),
                "p70": round(quantile(values, 0.70), 4),
            },
        },
        "history": history,
        "history_status": "available",
        "method": method,
        "source_ids": source_ids,
        "warnings": [],
    }


def build_relative_proxy_asset(
    config: dict[str, Any], data: dict[str, Any], source_states: dict[str, str], through: str
) -> dict[str, Any]:
    target_id = NDXTMC_SOURCE_ID
    baseline_id = source_cache_id(config["baseline_ticker"])
    source_ids = [target_id, baseline_id, DQYDJ_SOURCE_ID]
    status = _asset_status(source_ids, source_states)
    if status == "unavailable" or any(data.get(source_id) is None for source_id in source_ids):
        return _unavailable_asset(config, source_ids, "相对 PE 分位模型的指数、行情或盈利数据不可用且无有效缓存。")
    target = monthly_average(data[target_id])
    baseline = monthly_average(data[baseline_id])
    pe = {str(item["month"]): float(item["pe_ttm"]) for item in data[DQYDJ_SOURCE_ID]}
    try:
        months = select_contiguous_window(target, baseline, pe, through)
    except ValuationError as exc:
        return _unavailable_asset(config, source_ids, str(exc))
    values: list[float] = []
    history: list[dict[str, Any]] = []
    for month in months:
        value = round(pe[month] * (target[month] / baseline[month]), 6)
        values.append(value)
        history.append({"month": month, "relative_score": value})
    current_value = values[-1]
    return {
        "id": config["id"],
        "asset_class": config["asset_class"],
        "source_mode": config["source_mode"],
        "name": config["name"],
        "code": config["code"],
        "region": config["region"],
        "frequency": "monthly",
        "status": status,
        "as_of": months[-1],
        "current": {
            "relative_percentile_10y": round(
                percentile_midrank(values, current_value), 2
            ),
            "sample_count": len(values),
            "window_start": months[0],
            "window_end": months[-1],
            "reference_levels": {
                "p30": round(quantile(values, 0.30), 6),
                "p50": round(quantile(values, 0.50), 6),
                "p70": round(quantile(values, 0.70), 6),
            },
        },
        "history": history,
        "history_status": "available",
        "method": {
            "id": config["method_id"],
            "label": "NDXTMC/SPY 相对 PE 分位代理",
            "value_kind": "relative_score",
            "formula": "relative_score_m = S&P500_PE_m × (NDXTMC_m / SPY_m)",
            "price_aggregation": "calendar_month_mean_index_and_close",
            "percentile": "midrank_120_complete_months",
            "quantile": "linear_interpolation_inclusive",
            "definition_url": config["definition_url"],
            "experimental": False,
            "limitations": [
                *config["limitations"],
                "相对分数仅用于计算自身历史分位，不是 PE 倍数。",
                "结果不构成投资建议，也不输出低估或高估判断。",
            ],
        },
        "source_ids": source_ids,
        "warnings": [],
    }


def build_gold_asset(
    config: dict[str, Any],
    data: Any,
    source_states: dict[str, str],
    catalog: dict[str, Any],
    as_of: date,
) -> dict[str, Any]:
    source_ids = [GOLD_SOURCE_ID]
    status = _asset_status(source_ids, source_states)
    if status == "unavailable" or not isinstance(data, dict):
        return _unavailable_asset(config, source_ids, "黄金估值快照不可用且无有效缓存。")
    factors = {
        key: {
            **factor,
            "lag_days": max(
                0,
                (as_of - datetime.strptime(factor["date"], "%Y-%m-%d").date()).days,
            ),
        }
        for key, factor in data["factors"].items()
    }
    current = {
        "spot_usd_oz": round(float(data["spot_usd_oz"]), 4),
        "percentiles": {
            key: round(float(value), 2) for key, value in data["percentiles"].items()
        },
        "source_rating_1y": {
            "label": data["source_ratings"]["1y"],
            "provider": "中美双锚三因子黄金估值模型",
        },
        "source_rating_all": {
            "label": data["source_ratings"]["all"],
            "provider": "中美双锚三因子黄金估值模型",
        },
        "residual": round(float(data["residual"]), 6),
        "factors": factors,
    }
    source = catalog["sources"][GOLD_SOURCE_ID]
    return {
        "id": config["id"],
        "asset_class": config["asset_class"],
        "source_mode": config["source_mode"],
        "name": config["name"],
        "code": config["code"],
        "region": config["region"],
        "frequency": "snapshot",
        "status": status,
        "as_of": data["as_of"],
        "current": current,
        "history": [],
        "history_status": "not_published_by_source",
        "method": {
            "id": config["method_id"],
            "label": "外部中美双锚三因子模型当前快照",
            "attribution_url": source["url"],
            "repository_url": source["repository_url"],
            "limitations": [
                "本页仅转述来源模型的当前数值、分位、评级与因子新鲜度。",
                "不复制来源图表、回测、仓位建议或买卖策略。",
                "来源评级不代表本站判断，结果不构成投资建议。",
            ],
        },
        "source_ids": source_ids,
        "warnings": [],
    }


def aggregate_status(assets: list[dict[str, Any]]) -> str:
    statuses = [asset["status"] for asset in assets]
    if all(status == "fresh" for status in statuses):
        return "fresh"
    if all(status == "unavailable" for status in statuses):
        return "unavailable"
    available = [status for status in statuses if status != "unavailable"]
    if available and all(status == "cached_stale" for status in available) and len(available) == len(statuses):
        return "stale"
    return "partial"
