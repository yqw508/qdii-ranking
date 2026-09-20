"""HTML components used by the ranking page renderer."""

from __future__ import annotations

import html
from typing import Any

from send_qdii_email import format_three_year_boundary

from ..config import ROUTING_REASON_GEOGRAPHY_OVERRIDE
from .common import (
    benchmark_display,
    format_beta,
    format_correlation,
    format_holding_cost,
    format_limit,
    format_long_return,
    format_optional_percentage,
    format_percentage,
    format_rule,
    html_source_link,
    routing_reason_label,
)

def render_card(item: dict[str, Any], payload: dict[str, Any]) -> str:
    is_us = item["ranking_list"] == "us_main"
    fit = item["nasdaq100_fit"]
    contract = item["contract_benchmark"]
    holding_cost = item["holding_cost"]
    exposure = item["us_equity_exposure"]
    direct_text = format_limit(item["direct_limit"])
    agency_text = format_limit(item["agency_limit"])
    direct_link = html_source_link(direct_text, item["direct_limit"].get("source_url"))
    agency_link = html_source_link(agency_text, item["agency_limit"].get("source_url"))
    tags = "".join(
        f'<span class="tag {"risk" if tag in {"杠杆", "反向", "波动率策略"} else ""}">{html.escape(tag)}</span>'
        for tag in item["product_structure_tags"]
    )
    if item["routing_reason"] == ROUTING_REASON_GEOGRAPHY_OVERRIDE:
        tags += '<span class="tag route">地域名称分流</span>'
    if is_us:
        primary_metrics = f"""
        <span class="summary-metric fit"><span class="metric-label">纳指相关 / β</span><span class="metric-value">{format_correlation(fit['correlation'])} · {format_beta(fit['beta'])}</span></span>
        <span class="summary-metric accent"><span class="metric-label">美股下限</span><span class="metric-value">{format_percentage(exposure['confirmed_pct'])}</span></span>"""
        list_details = f"""
        <div><dt>纳指相关性</dt><dd>{format_correlation(fit['correlation'])}</dd></div>
        <div><dt>Beta</dt><dd>{format_beta(fit['beta'])}</dd></div>
        <div><dt>跟踪误差</dt><dd>{format_percentage(fit['tracking_error_pct'])}</dd></div>
        <div><dt>相关样本</dt><dd>{fit['observations']} 周</dd></div>
        <div><dt>美股确认下限</dt><dd>{format_percentage(exposure['confirmed_pct'])}</dd></div>
        <div><dt>美股可能上限</dt><dd>{format_percentage(exposure['possible_pct'])}</dd></div>"""
        extra_sources = "".join(
            (
                html_source_link(
                    f"定期报告 {exposure['report_date']}", exposure.get("source_url")
                ),
                html_source_link("Nasdaq XNDX", payload["benchmark"]["index_source_url"]),
                html_source_link("美元兑人民币中间价", payload["benchmark"]["fx_source_url"]),
            )
        )
    else:
        ratio = "∞" if item["return_drawdown_ratio"] is None else f"{item['return_drawdown_ratio']:.2f}"
        primary_metrics = f"""
        <span class="summary-metric fit"><span class="metric-label">收益回撤比</span><span class="metric-value">{ratio}</span></span>
        <span class="summary-metric accent"><span class="metric-label">美股确认区间</span><span class="metric-value small">{format_percentage(exposure['confirmed_pct'])}-{format_percentage(exposure['possible_pct'])}</span></span>"""
        list_details = f"""
        <div><dt>三年年化收益</dt><dd class="positive-text">{format_percentage(item['three_year_annualized_return_pct'], show_sign=True)}</dd></div>
        <div><dt>收益回撤比</dt><dd>{ratio}</dd></div>
        <div><dt>美股确认下限</dt><dd>{format_percentage(exposure['confirmed_pct'])}</dd></div>
        <div><dt>美股可能上限</dt><dd>{format_percentage(exposure['possible_pct'])}</dd></div>"""
        extra_sources = html_source_link(
            f"定期报告 {exposure['report_date']}", exposure.get("source_url")
        )
    holding_cost_link = html_source_link(
        format_holding_cost(holding_cost), holding_cost.get("source_url")
    )
    prospectus_date = contract.get("prospectus_published_date") or "--"
    boundary_detail = (
        f'<div><dt>三年实际区间</dt><dd>{html.escape(format_three_year_boundary(item))}</dd></div>'
        if item.get("three_year_boundary_shortfall_days") else ""
    )
    return f"""
  <details class="fund-item" data-code="{html.escape(item['code'], quote=True)}" data-list="{html.escape(item['ranking_list'], quote=True)}" data-routing-reason="{html.escape(item['routing_reason'], quote=True)}">
    <summary>
      <span class="rank" aria-label="排名 {item['rank']}">{item['rank']}</span>
      <span class="fund-identity">
        <span class="fund-name-row"><strong>{html.escape(item['name'])}</strong><span class="fund-code">{html.escape(item['code'])}</span></span>
        <span class="benchmark-label">{html.escape(benchmark_display(contract))}</span>
        <span class="tags">{tags}</span>
      </span>
      <span class="summary-metrics">
        {primary_metrics}
        <span class="summary-metric positive"><span class="metric-label">近三年</span><span class="metric-value">{format_percentage(item['three_year_return_pct'], show_sign=True)}</span></span>
        <span class="summary-metric"><span class="metric-label">持有费率</span><span class="metric-value quota">{format_holding_cost(holding_cost)}</span></span>
        <span class="summary-metric"><span class="metric-label">直销</span><span class="metric-value quota">{direct_text}</span></span>
      </span>
      <span class="chevron" aria-hidden="true"></span>
    </summary>
    <div class="fund-detail">
      <dl class="detail-grid">
        <div><dt>成立日</dt><dd>{html.escape(item['inception_date'])}</dd></div>
        <div><dt>机构持有</dt><dd>{format_percentage(item['institution_holding_ratio_pct'])}</dd></div>
        <div><dt>规模</dt><dd>{item['scale_billion_cny']:.2f} 亿元</dd></div>
        <div><dt>分流原因</dt><dd>{html.escape(routing_reason_label(item['routing_reason']))}</dd></div>
        <div><dt>近一年收益</dt><dd class="positive-text">{format_percentage(item['one_year_return_pct'], show_sign=True)}</dd></div>
        <div><dt>近一年回撤</dt><dd class="negative-text">{format_percentage(item['one_year_max_drawdown_pct'])}</dd></div>
        <div><dt>近三年收益</dt><dd class="positive-text">{format_percentage(item['three_year_return_pct'], show_sign=True)}</dd></div>
        <div><dt>近三年回撤</dt><dd class="negative-text">{format_percentage(item['three_year_max_drawdown_pct'])}</dd></div>
        <div><dt>近五年收益</dt><dd>{html.escape(format_long_return(item, 'five_year'))}</dd></div>
        <div><dt>近十年收益</dt><dd>{html.escape(format_long_return(item, 'ten_year'))}</dd></div>
        <div><dt>持有费率（年化）</dt><dd>{holding_cost_link}</dd></div>
        {list_details}
      </dl>
      <section class="benchmark-detail" aria-label="合同基准">
        <span>合同基准</span>
        <strong>{html.escape(contract['benchmark_text'])}</strong>
        <small>解析状态：{html.escape(contract['status'])}；仅作资料展示，不参与准入或分榜。</small>
      </section>
      <div class="quota-grid" aria-label="申购额度">
        <div><span>直销额度</span>{direct_link}</div>
        <div><span>代销额度</span>{agency_link}</div>
      </div>
      <dl class="rule-grid">
        <div><dt>额度计算</dt><dd>{html.escape(format_rule(item['share_class_rule'], item['channel_rule']))}</dd></div>
        <div><dt>完整净值历史</dt><dd>{html.escape(item['nav_history_start_date'])} 至 {html.escape(item['nav_history_end_date'])}</dd></div>
        {boundary_detail}
        <div><dt>招募说明书日期</dt><dd>{html.escape(prospectus_date)}</dd></div>
      </dl>
      <div class="source-row">
        {html_source_link('招募说明书', contract['source_url'])}
        {html_source_link('人民币产品概要', contract.get('product_summary_source_url'))}
        {html_source_link('基金主页', item['fund_page_url'])}
        {extra_sources}
      </div>
    </div>
    </details>"""

def render_nasdaq100_table(records: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in records:
        fit = item.get("nasdaq100_fit_2y") or {}
        contract = item["contract_benchmark"]
        contract_label = benchmark_display(contract)
        if item.get("contract_name_match_warning"):
            contract_label += "（名称与合同基准需核对）"
        scale = (
            "--"
            if item.get("scale_billion_cny") is None
            else f"{float(item['scale_billion_cny']):.2f} 亿元"
        )
        rows.append(
            "<tr>"
            f"<td data-label=\"排名\"><strong>{item['rank']}</strong></td>"
            f"<td data-label=\"基金\"><a class=\"source-link\" href=\"{html.escape(item['fund_page_url'], quote=True)}\" target=\"_blank\" rel=\"noopener noreferrer\">{html.escape(item['name'])}</a><small>{html.escape(item['code'])} · {html.escape(item.get('fund_type') or '')}</small></td>"
            f"<td data-label=\"申购状态\">{html.escape(item.get('purchase_status_text') or item.get('purchase_status') or '待核实')}</td>"
            f"<td data-label=\"近两年\" class=\"{'positive-text' if item.get('two_year_return_pct') is not None and float(item['two_year_return_pct']) >= 0 else ''}\">{html.escape(format_optional_percentage(item.get('two_year_return_pct'), show_sign=True))}</td>"
            f"<td data-label=\"两年回撤\" class=\"negative-text\">{html.escape(format_optional_percentage(item.get('two_year_max_drawdown_pct')))}</td>"
            f"<td data-label=\"综合费率\">{html.escape(format_holding_cost(item['holding_cost']))}</td>"
            f"<td data-label=\"两年跟踪误差\">{html.escape(format_optional_percentage(fit.get('tracking_error_pct')))}</td>"
            f"<td data-label=\"近三年\">{html.escape(format_optional_percentage(item.get('three_year_return_pct'), show_sign=True))}</td>"
            f"<td data-label=\"规模\">{html.escape(scale)}</td>"
            f"<td data-label=\"合同基准\"><span class=\"benchmark-compact\">{html.escape(contract_label)}</span></td>"
            "</tr>"
        )
    return "".join(rows) or '<tr><td colspan="10" class="empty-state">暂无名称匹配的场外纳指100产品</td></tr>'

def render_list(
    records: list[dict[str, Any]], payload: dict[str, Any]
) -> str:
    if not records:
        return '<p class="empty-state">暂无符合全部条件的基金</p>'
    return "".join(render_card(item, payload) for item in records)

def premium_band(value: float | None) -> tuple[str, str]:
    if value is None:
        return "unavailable", "--"
    if value < 0:
        return "discount", "折价"
    if value <= 2:
        return "normal", "0–2%"
    if value <= 5:
        return "elevated", "2–5%"
    return "high", ">5%高溢价"

def format_premium_value(value: Any, digits: int = 2) -> str:
    if value is None:
        return "--"
    numeric = float(value)
    return f"{numeric:+.{digits}f}%"

def format_turnover(value: Any) -> str:
    if value is None:
        return "--"
    numeric = float(value)
    if numeric >= 100_000_000:
        return f"{numeric / 100_000_000:.2f}亿元"
    if numeric >= 10_000:
        return f"{numeric / 10_000:.0f}万元"
    return f"{numeric:.0f}元"

def format_quote_time(value: str | None) -> str:
    return "--" if not value else value[:16].replace("T", " ")

def render_premium_row(
    item: dict[str, Any], product_label: str, group_label: str
) -> str:
    value = item["premium_pct"]
    reference_type = item["reference_value_type"]
    reference_value = item["reference_value_cny"]
    reference_label = (
        f"最新单位净值（{item['reference_value_date']}）"
        if reference_type == "nav"
        else "IOPV"
    )
    band_key, band_label = premium_band(value)
    quote_status = item["quote_status"]
    holding_cost = item["holding_cost"]
    holding_cost_text = format_holding_cost(holding_cost)
    holding_cost_status = holding_cost["status"]
    holding_cost_stale = (
        '<span class="stale-label">旧值</span>'
        if holding_cost_status == "stale"
        else ""
    )
    holding_cost_link = html_source_link(
        holding_cost_text, holding_cost.get("source_url")
    )
    holding_cost_date = holding_cost.get("source_published_date") or "--"
    row_classes = ["premium-row"]
    if quote_status == "stale":
        row_classes.append("is-stale")
    elif quote_status == "unavailable":
        row_classes.append("is-unavailable")
    stale_text = "旧值" if quote_status == "stale" else ("暂无行情" if quote_status == "unavailable" else "")
    source_url = item.get("quote_source_url") or item["source_url"]
    holding_cost_source = ""
    if holding_cost.get("source_url"):
        holding_cost_source = (
            f'<a class="premium-source-link" href="{html.escape(holding_cost["source_url"], quote=True)}" '
            'target="_blank" rel="noopener noreferrer">查看费率来源'
            '<span class="external" aria-hidden="true">↗</span></a>'
        )
    reference_source = ""
    if reference_type == "nav":
        reference_source = (
            f'<a class="premium-source-link" href="{html.escape(item["reference_value_source_url"], quote=True)}" '
            'target="_blank" rel="noopener noreferrer">查看净值来源'
            '<span class="external" aria-hidden="true">↗</span></a>'
        )
    premium_data = "NaN" if value is None else f"{float(value):.8g}"
    detail_id = f"premium-detail-{item['code']}"
    return f"""
      <tbody class="premium-item {' '.join(row_classes)}" data-etf-code="{item['code']}" data-premium="{premium_data}" data-quote-status="{quote_status}">
        <tr class="premium-summary-row">
          <td class="premium-name" data-label="{product_label}"><button class="premium-row-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}"><span><strong data-field="name">{html.escape(item['name'])}</strong><span class="fund-code">{item['code']} · {item['exchange']}</span></span><span class="premium-chevron" aria-hidden="true"></span></button></td>
          <td data-label="{group_label}"><span class="benchmark-compact">{html.escape(item['benchmark_group'])}</span></td>
          <td data-label="溢价"><span class="premium-value band-{band_key}" data-field="premium">{format_premium_value(value)}</span><span class="premium-band band-{band_key}" data-field="band">{band_label}</span><span class="stale-label" data-field="stale">{stale_text}</span></td>
          <td data-label="综合费率"><span class="premium-cost" data-field="holding-cost">{holding_cost_text}</span>{holding_cost_stale}</td>
          <td data-label="涨跌"><span data-field="change">{format_premium_value(item['change_pct'])}</span></td>
        </tr>
        <tr class="premium-detail-row" id="{detail_id}" hidden>
          <td colspan="5">
            <dl class="premium-detail-grid">
              <div><dt>价格</dt><dd data-field="price">{'--' if item['market_price_cny'] is None else f"{float(item['market_price_cny']):.3f}"}</dd></div>
              <div><dt data-field="reference-label">{html.escape(reference_label)}</dt><dd data-field="reference-value">{float(reference_value):.4f}</dd></div>
              <div><dt>成交额</dt><dd data-field="turnover">{format_turnover(item['turnover_cny'])}</dd></div>
              <div><dt>行情时间</dt><dd><time data-field="updated">{format_quote_time(item['updated_at'])}</time></dd></div>
              <div><dt>交易所</dt><dd>{item['exchange']}</dd></div>
              <div><dt>分类</dt><dd>{'QDII' if item['category'] == 'qdii' else ('行业主题' if item['category'] == 'sector_theme' else '宽基')}</dd></div>
              <div><dt>综合费率（年化）</dt><dd>{holding_cost_link}{holding_cost_stale}</dd></div>
              <div><dt>费率资料日期</dt><dd>{html.escape(holding_cost_date)}</dd></div>
            </dl>
            <div class="premium-source-row">
              <a class="premium-source-link" href="{html.escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">查看行情来源<span class="external" aria-hidden="true">↗</span></a>
              {reference_source}
              {holding_cost_source}
            </div>
          </td>
        </tr>
      </tbody>"""
