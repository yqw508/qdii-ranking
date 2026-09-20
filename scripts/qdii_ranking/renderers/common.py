"""Shared presentation helpers for ranking artifacts."""

from __future__ import annotations

import html
from typing import Any

from ..config import ROUTING_REASON_LABELS


def routing_reason_label(reason: str) -> str:
    return ROUTING_REASON_LABELS[reason]


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


def html_source_link(label: str, source_url: str | None) -> str:
    escaped_label = html.escape(label)
    if not source_url:
        return f'<span class="source-value">{escaped_label}</span>'
    return (
        f'<a class="source-link" href="{html.escape(source_url, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">{escaped_label}'
        '<span class="external" aria-hidden="true">↗</span></a>'
    )
