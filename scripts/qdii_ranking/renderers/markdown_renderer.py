"""Markdown renderer for the ranking public contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from send_qdii_email import format_three_year_boundary

from ..atomic import atomic_write_text
from .common import (
    benchmark_display,
    format_beta,
    format_correlation,
    format_holding_cost,
    format_limit,
    format_optional_percentage,
    format_percentage,
    format_rule,
    routing_reason_label,
    summarize_periods,
)


def render_markdown(path: Path, payload: dict[str, Any]) -> None:
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
        f"- 三年起点边界容差：最多 {payload['filters']['three_year_boundary_tolerance_days']} 个自然日，仅限首条净值与成立日一致且成立严格超过三年的基金。",
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
        if item.get("three_year_boundary_shortfall_days"):
            lines.append(f"  - {format_three_year_boundary(item)}")
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
        if item.get("three_year_boundary_shortfall_days"):
            lines.append(f"  - {format_three_year_boundary(item)}")
    if not global_records:
        lines.append("| - | 暂无符合全部条件的基金 | - | - | - | - | - | - | - | - | - | - |")
    nasdaq_records = (payload.get("nasdaq100_otc") or {}).get("records") or []
    lines.extend(
        [
            "",
            "## 场外纳指100",
            "",
            "按名称匹配纳斯达克100、纳指100或 NASDAQ 100 的场外人民币 A 类份额；不以合同基准、成立年限、收益门槛、额度或当前申购状态筛除产品。",
            "",
            "排序：近两年收益降序、年化综合费率升序、两年纳指跟踪误差升序、近三年收益降序、规模降序、代码升序；缺失值排在有数据记录之后。",
            "",
            "| 排名 | 基金 | 申购状态 | 近两年 | 两年回撤 | 综合费率 | 两年跟踪误差 | 近三年 | 规模 | 合同基准 |",
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in nasdaq_records:
        fit_2y = item.get("nasdaq100_fit_2y") or {}
        contract = item["contract_benchmark"]
        contract_source = contract.get("source_url") or item["fund_page_url"]
        scale_text = (
            "--"
            if item.get("scale_billion_cny") is None
            else f"{float(item['scale_billion_cny']):.2f} 亿元"
        )
        lines.append(
            f"| {item['rank']} | {item['name']} {item['code']} | "
            f"{item.get('purchase_status_text') or item.get('purchase_status') or '待核实'} | "
            f"{format_optional_percentage(item.get('two_year_return_pct'), show_sign=True)} | "
            f"{format_optional_percentage(item.get('two_year_max_drawdown_pct'))} | "
            f"{format_holding_cost(item['holding_cost'])} | "
            f"{format_optional_percentage(fit_2y.get('tracking_error_pct'))} | "
            f"{format_optional_percentage(item.get('three_year_return_pct'), show_sign=True)} | "
            f"{scale_text} | "
            f"[{benchmark_display(contract)}]({contract_source}) |"
        )
        if item.get("contract_name_match_warning"):
            lines.append(f"  - {item['contract_name_match_warning']}")
    if not nasdaq_records:
        lines.append("| - | 暂无名称匹配的场外纳指100产品 | - | - | - | - | - | - | - | - |")
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
    atomic_write_text(path, "\n".join(lines))
