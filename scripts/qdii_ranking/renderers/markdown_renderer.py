"""Markdown renderer for the ranking public contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from send_qdii_email import format_three_year_boundary

from ..atomic import atomic_write_text
from ..sources.otc_shares import EVIDENCE_LABEL
from .common import (
    benchmark_display,
    format_beta,
    format_correlation,
    format_fee_components,
    format_holding_cost,
    format_summary_holding_cost,
    format_limit,
    format_optional_percentage,
    format_percentage,
    format_rule,
    routing_reason_label,
    summarize_periods,
)


def _fee_line(item: dict[str, Any]) -> str:
    return (
        f"  - 年费率构成：{format_fee_components(item['holding_cost'])}；"
        f"产品概要综合费率（参考）：{format_summary_holding_cost(item.get('summary_holding_cost'))}"
    )


def _nasdaq_tag_lines(item: dict[str, Any]) -> list[str]:
    lines = []
    if item.get("us_main_rank"):
        lines.append(f"  - 美国主榜第 {int(item['us_main_rank'])} 名")
    if item.get("manual_include"):
        lines.append("  - 指定对比")
    lines.append(_fee_line(item))
    return lines


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
        f"直销额度（个人） >= {payload['filters']['min_direct_limit_cny_inclusive']:,} 元；"
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
        "| 排名 | 基金 | 分流原因 | 近三年 | 近五年 | 近十年 | 年费率 | 纳指相关性 | Beta | 美股确认区间 | 直销额度（个人） | 代销额度 |",
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
        lines.append(_fee_line(item))
        if item.get("three_year_boundary_shortfall_days"):
            lines.append(f"  - {format_three_year_boundary(item)}")
        institution = item.get('institutional_direct_limit', {})
        if institution.get('status') not in (None, 'unknown') and format_limit(institution) != format_limit(item['direct_limit']):
            lines.append(f"  - 机构直销：[{format_limit(institution)}]({institution.get('source_url')})；个人额度见表。")
    if not records:
        lines.append("| - | 暂无符合全部条件的基金 | - | - | - | - | - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 全球补充榜",
            "",
        "| 排名 | 基金 | 分流原因 | 合同基准 | 美股确认区间 | 近三年 | 近五年 | 近十年 | 年费率 | 收益回撤比 | 直销额度（个人） | 代销额度 |",
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
        lines.append(_fee_line(item))
        institution = item.get('institutional_direct_limit', {})
        if institution.get('status') not in (None, 'unknown') and format_limit(institution) != format_limit(item['direct_limit']):
            lines.append(f"  - 机构直销：[{format_limit(institution)}]({institution.get('source_url')})；个人额度见表。")
        if item.get("three_year_boundary_shortfall_days"):
            lines.append(f"  - {format_three_year_boundary(item)}")
    if not global_records:
        lines.append("| - | 暂无符合全部条件的基金 | - | - | - | - | - | - | - | - | - | - |")
    nasdaq_records = (payload.get("nasdaq100_otc") or {}).get("records") or []
    nasdaq_window = (payload.get("nasdaq100_otc") or {}).get("comparison_window") or {}
    window_text = (
        f"{nasdaq_window.get('start_date')} 至 {nasdaq_window.get('end_date')}"
        if nasdaq_window.get("status") == "available"
        else "暂不可用"
    )
    lines.extend(
        [
            "",
            "## 场外纳指100",
            "",
            "对比名称匹配纳斯达克100、纳指100或 NASDAQ 100 的场外人民币各类份额（A/C/D/E/F/I 等）及经产品概要确认的无标记人民币主份额，以及当天美国主榜前 3 名和指定对比的天弘标普500 D 类；排除美元、港币、后端和独立 ETF 份额；不以合同基准、收益门槛、额度或当前申购状态筛除产品。",
            "",
            f"共同区间：{window_text}（最近两年）；没有区间收益的产品不列入。结束日随最新共同净值滚动。",
            "排序：公共区间收益降序、年费率升序、公共区间跟踪误差升序、公共区间最大回撤较小者优先、规模降序、代码升序；缺失值排在有数据记录之后。",
            "",
            "| 排名 | 基金 | 成立日 | 申购状态 | 区间收益 | 区间回撤 | 年费率 | 区间跟踪误差 | 规模 | 合同基准 |",
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in nasdaq_records:
        contract = item["contract_benchmark"]
        contract_source = contract.get("source_url") or item["fund_page_url"]
        scale_text = (
            "--"
            if item.get("scale_billion_cny") is None
            else f"{float(item['scale_billion_cny']):.2f} 亿元"
        )
        lines.append(
            f"| {item['rank']} | {item['name']} {item['code']} | "
            f"{item.get('inception_date') or '--'} | "
            f"{item.get('purchase_status_text') or item.get('purchase_status') or '待核实'} | "
            f"{format_optional_percentage(item.get('common_period_return_pct'), show_sign=True)} | "
            f"{format_optional_percentage(item.get('common_period_max_drawdown_pct'))} | "
            f"{format_holding_cost(item['holding_cost'])} | "
            f"{format_optional_percentage((item.get('nasdaq100_fit_common_period') or {}).get('tracking_error_pct'))} | "
            f"{scale_text} | "
            f"[{benchmark_display(contract)}]({contract_source}) |"
        )
        lines.extend(_nasdaq_tag_lines(item))
        if item.get("contract_name_match_warning"):
            lines.append(f"  - {item['contract_name_match_warning']}")
        if evidence := item.get("share_class_evidence"):
            lines.append(
                f"  - {item['code']}：{EVIDENCE_LABEL}；"
                f"[产品概要 {evidence['published_date']}]({evidence['source_url']})"
            )
        if item.get("common_period_error"):
            lines.append(f"  - 公共区间数据：{item['common_period_error']}")
    if not nasdaq_records:
        lines.append("| - | 暂无有区间收益的场外纳指100产品 | - | - | - | - | - | - | - | - |")
    nasdaq_excluded = (payload.get("nasdaq100_otc") or {}).get("excluded_records") or []
    if nasdaq_excluded:
        lines.extend(["", f"未列入（{len(nasdaq_excluded)} 只，没有公共区间收益）：", ""])
        lines.extend(
            f"- {item['code']} {item['name']}：{item['reason']}" for item in nasdaq_excluded
        )
    if payload["warnings"]:
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {warning}" for warning in payload["warnings"])
    lines.extend(
        [
            "",
            "额度为基金管理人层面的单日单基金账户上限；代销平台可能设置更低限制。",
            "年费率为天天基金基金概况页的管理费率、托管费率、销售服务费率合计；产品概要的基金运作综合费率（年化）另列作参考，二者差异来自审计费、信息披露费等其他运作费用。费用已反映在基金净值中，不从收益率重复扣减。",
            "",
        ]
    )
    if payload.get("ranking_changes"):
        changes = payload["ranking_changes"]
        lines += ["", "## 本次榜单变化", "", f"对比已验证榜单 {changes['baseline_date']}"]
        lines += [f"- {r['code']} {r['name']}：{r['change']}，{r['reason']}" for r in changes['records']]
    if payload.get("freshness_policy_version") == 1:
        from ..freshness import freshness_text
        lines += ["", "## 数据新鲜度", "", f"生成时间：{payload['generated_at']}；时效上限为 7 个已结束发布工作日。"]
        for label, evidence in payload["benchmark"]["freshness"].items():
            lines.append(f"- {label}：{freshness_text(evidence)}")
        for record in [*payload['records'], *payload['global_supplement']['records']]:
            lines.append(f"- {record['code']}：{freshness_text(record['nav_freshness'])}")
    atomic_write_text(path, "\n".join(lines))
