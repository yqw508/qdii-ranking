"""HTML renderer for the ranking public contract."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from send_qdii_email import format_three_year_boundary

from ..atomic import atomic_write_text
from ..config import ETF_MARKET_LIST_PAGE_SIZE, ROUTING_REASON_GEOGRAPHY_OVERRIDE
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
    summarize_periods,
)


def render_html(path: Path, payload: dict[str, Any]) -> None:
    us_records = payload["records"]
    global_records = payload["global_supplement"]["records"]
    nasdaq_records = (payload.get("nasdaq100_otc") or {}).get("records") or []
    premium = payload["exchange_premium"]
    premium_records = premium["records"]
    dynamic_premium_catalog = (
        "discovered_count" in premium
        or any(item.get("category") == "qdii" for item in premium_records)
    )
    premium_product_label = "产品" if dynamic_premium_catalog else "ETF"
    premium_group_label = "类型" if dynamic_premium_catalog else "基准"
    combined = [*us_records, *global_records]
    filters = payload["filters"]
    scale_dates = "、".join(sorted({item["scale_report_date"] for item in combined})) or "无"
    min_scale = filters["min_scale_billion_cny"]
    scale_requirement = (
        "规模不限" if min_scale is None else f"规模 > {float(min_scale):g} 亿元"
    )
    filter_parts = (
        scale_requirement,
        f"成立 > {filters['min_age_years']} 年",
        f"三年收益 ≥ {filters['min_three_year_return_pct']:g}%",
        f"三年起点容差 ≤ {filters['three_year_boundary_tolerance_days']} 天",
        f"五年有数据 ≥ {filters['min_five_year_return_pct_if_available']:g}%",
        f"十年有数据 ≥ {filters['min_ten_year_return_pct_if_available']:g}%",
        f"直销 ≥ {filters['min_direct_limit_cny_inclusive']:,} 元",
        "业绩基准仅展示",
        f"美国榜排除 {' / '.join(filters['us_main_exclude_keywords'])}",
        "全球榜地域不限",
    )
    filter_html = "".join(
        f'<span class="filter-condition">{html.escape(part)}</span>'
        for part in filter_parts
    )

    def render_card(item: dict[str, Any]) -> str:
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

    def render_list(records: list[dict[str, Any]]) -> str:
        if not records:
            return '<p class="empty-state">暂无符合全部条件的基金</p>'
        return "".join(render_card(item) for item in records)

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

    def render_premium_row(item: dict[str, Any]) -> str:
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
              <td class="premium-name" data-label="{premium_product_label}"><button class="premium-row-toggle" type="button" aria-expanded="false" aria-controls="{detail_id}"><span><strong data-field="name">{html.escape(item['name'])}</strong><span class="fund-code">{item['code']} · {item['exchange']}</span></span><span class="premium-chevron" aria-hidden="true"></span></button></td>
              <td data-label="{premium_group_label}"><span class="benchmark-compact">{html.escape(item['benchmark_group'])}</span></td>
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

    premium_rows = "".join(render_premium_row(item) for item in premium_records)
    premium_status_labels = {
        "fresh": "日报快照完整",
        "partial": "部分ETF使用旧值",
        "stale": "当前显示缓存行情",
        "unavailable": "日报快照暂不可用",
    }
    premium_status_text = premium_status_labels.get(premium["status"], "行情状态未知")
    premium_config = {
        "refreshUrl": premium["refresh_url"],
        "refreshMode": "paged" if any(item.get("category") == "qdii" for item in premium_records) else "single",
        "refreshPageSize": ETF_MARKET_LIST_PAGE_SIZE,
        "entries": [
            {
                "code": item["code"],
                "name": item["name"],
                "benchmarkGroup": item["benchmark_group"],
                "category": item.get("category"),
                "referenceType": item.get("reference_value_type"),
                "referenceValueCny": item.get("reference_value_cny"),
                "referenceDate": item.get("reference_value_date"),
            }
            for item in premium_records
        ],
    }
    premium_config_json = json.dumps(
        premium_config, ensure_ascii=False, separators=(",", ":")
    ).replace("</", "<\\/")
    browser_script = ((Path(__file__).resolve().parents[2] / "premium_refresh.js")).read_text(
        encoding="utf-8"
    )

    warning_section = ""
    if payload["warnings"]:
        warning_items = "".join(
            f"<li>{html.escape(warning)}</li>" for warning in payload["warnings"]
        )
        warning_section = f"""
      <details class="warnings">
        <summary>数据警告 <span>{len(payload['warnings'])}</span></summary>
        <ul>{warning_items}</ul>
      </details>"""

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>QDII 榜单与场内溢价 · {html.escape(payload['run_date'])}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f3f5f7; --surface:#fff; --text:#18222c; --muted:#66727e; --border:#d7dde3; --accent:#086b58; --accent-soft:#e8f3ef; --blue:#195c9b; --blue-soft:#edf4fa; --positive:#147a4b; --negative:#b42318; --warning:#8a4b08; }}
    * {{ box-sizing:border-box; }}
    html {{ background:var(--bg); }}
    body {{ margin:0; min-width:280px; color:var(--text); background:var(--bg); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; font-size:15px; line-height:1.5; letter-spacing:0; overflow-wrap:anywhere; }}
    button {{ font:inherit; letter-spacing:0; }}
    a {{ color:var(--blue); text-decoration-thickness:1px; text-underline-offset:3px; }}
    .page {{ width:min(100%,980px); margin:0 auto; padding:max(16px,env(safe-area-inset-top)) max(14px,env(safe-area-inset-right)) max(28px,env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-left)); }}
    .page-header {{ padding:4px 2px 16px; }}
    .title-row {{ display:flex; align-items:baseline; justify-content:space-between; gap:16px; }}
    h1 {{ margin:0; font-size:22px; line-height:1.25; letter-spacing:0; }}
    .run-date,.metric-label,dt,.quota-grid span,.benchmark-detail>span {{ color:var(--muted); font-size:12px; }}
    .run-date {{ white-space:nowrap; }}
    .overview-details {{ margin-top:10px; }}
    .overview-toggle {{ display:flex; min-height:40px; align-items:center; justify-content:space-between; gap:12px; padding:0 2px; color:#35414d; cursor:pointer; font-weight:700; list-style:none; }}
    .overview-toggle::-webkit-details-marker {{ display:none; }}
    .overview-chevron {{ width:9px; height:9px; flex:0 0 auto; border-right:2px solid #7a8793; border-bottom:2px solid #7a8793; transform:rotate(45deg); transition:transform 150ms ease; }}
    .overview-details[open] .overview-chevron {{ transform:rotate(225deg); }}
    .overview-content {{ padding-top:2px; }}
    .filter-line {{ display:flex; flex-wrap:wrap; gap:2px 8px; margin:0; color:#35414d; font-size:14px; }}
    .filter-condition {{ white-space:nowrap; }}
    .filter-condition:not(:last-child)::after {{ content:" ·"; color:var(--muted); }}
    .meta-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 14px; margin:14px 0 0; }}
    .meta-grid div, .detail-grid div, .rule-grid div {{ min-width:0; }}
    dd {{ margin:2px 0 0; font-weight:650; }}
    .tabs {{ display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:4px; margin:0 0 12px; padding:4px; border:1px solid var(--border); border-radius:6px; background:#e9edf1; }}
    .tab {{ display:grid; min-height:42px; place-content:center; border:0; border-radius:4px; color:#42505d; background:transparent; cursor:pointer; font-weight:700; text-align:center; text-decoration:none; }}
    .tab[aria-selected="true"] {{ color:var(--text); background:var(--surface); box-shadow:0 1px 2px rgba(20,32,44,.12); }}
    .tab-link {{ color:#284c69; }}
    .tab-count {{ margin-left:6px; color:var(--muted); font-variant-numeric:tabular-nums; }}
    .tab:focus-visible,.overview-toggle:focus-visible,.reference-item-link:focus-visible,.fund-item summary:focus-visible,.refresh-button:focus-visible {{ outline:3px solid #86b7e8; outline-offset:2px; }}
    .reference-panel {{ display:grid; gap:12px; }}
    .reference-heading h2 {{ margin:0; font-size:17px; }}
    .reference-heading p {{ margin:3px 0 0; color:var(--muted); font-size:12px; }}
    .reference-list {{ display:grid; gap:8px; }}
    .reference-item {{ display:flex; min-width:0; align-items:center; justify-content:space-between; gap:14px; padding:14px; border:1px solid var(--border); border-left:3px solid var(--blue); border-radius:6px; background:var(--surface); }}
    .reference-copy {{ display:grid; min-width:0; gap:2px; }}
    .reference-source {{ color:var(--muted); font-size:12px; }}
    .reference-copy strong {{ font-size:15px; }}
    .reference-item-link {{ display:inline-flex; min-height:40px; flex:0 0 auto; align-items:center; justify-content:center; gap:5px; padding:7px 11px; border-radius:4px; color:#fff; background:var(--blue); font-weight:700; text-decoration:none; white-space:nowrap; }}
    .ranking-list {{ display:grid; gap:10px; }}
    .fund-item {{ border:1px solid var(--border); border-radius:6px; background:var(--surface); overflow:clip; }}
    .fund-item summary {{ display:grid; grid-template-columns:34px minmax(0,1fr) 18px; align-items:center; gap:0 10px; min-height:72px; padding:12px; cursor:pointer; list-style:none; -webkit-tap-highlight-color:transparent; }}
    .fund-item summary::-webkit-details-marker {{ display:none; }}
    .rank {{ display:grid; width:32px; height:32px; place-items:center; border-radius:4px; color:#fff; background:#263746; font-weight:750; font-variant-numeric:tabular-nums; }}
    .fund-identity {{ min-width:0; display:grid; gap:5px; }}
    .fund-name-row {{ display:flex; min-width:0; flex-wrap:wrap; align-items:baseline; gap:2px 8px; }}
    .fund-name-row strong {{ min-width:0; font-size:16px; line-height:1.35; }}
    .fund-code,.benchmark-label {{ color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; }}
    .tags {{ display:flex; flex-wrap:wrap; gap:4px; }}
    .tag {{ padding:1px 5px; border:1px solid #cbd3db; border-radius:3px; color:#4c5a67; background:#f8fafb; font-size:11px; }}
    .tag.risk {{ border-color:#e5a5a0; color:var(--negative); background:#fff5f4; }}
    .tag.route {{ border-color:#d6a04a; color:#755015; background:#fff8e8; }}
    .chevron {{ width:9px; height:9px; border-right:2px solid #7a8793; border-bottom:2px solid #7a8793; transform:rotate(45deg) translate(-2px,2px); transition:transform 150ms ease; }}
    .fund-item[open] .chevron {{ transform:rotate(225deg) translate(-1px,-1px); }}
    .summary-metrics {{ grid-column:2/-1; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-top:12px; }}
    .summary-metric {{ display:flex; min-width:0; min-height:46px; flex-direction:column; justify-content:center; padding:6px 8px; border-left:3px solid #c7cfd7; background:#f7f8fa; }}
    .summary-metric.fit {{ border-color:var(--blue); background:var(--blue-soft); }}
    .summary-metric.accent {{ border-color:var(--accent); background:var(--accent-soft); }}
    .summary-metric.positive {{ border-color:var(--positive); }}
    .metric-value {{ font-weight:750; font-variant-numeric:tabular-nums; white-space:nowrap; }}
    .metric-value.small,.metric-value.quota {{ font-size:13px; }}
    .fund-detail {{ border-top:1px solid var(--border); padding:14px 12px 16px; }}
    .detail-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px 16px; margin:0; }}
    .positive-text {{ color:var(--positive); }} .negative-text {{ color:var(--negative); }}
    .benchmark-detail {{ display:grid; gap:3px; margin-top:16px; padding:12px 0; border-top:1px solid var(--border); }}
    .benchmark-detail strong {{ font-size:13px; font-weight:650; }}
    .quota-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; padding:12px 0; border-top:1px solid var(--border); border-bottom:1px solid var(--border); }}
    .quota-grid div {{ display:flex; min-width:0; flex-direction:column; gap:3px; }}
    .source-link,.source-value {{ font-weight:700; }} .external {{ margin-left:4px; font-size:11px; }}
    .rule-grid {{ display:grid; gap:10px; margin:14px 0 0; }}
    .source-row {{ display:flex; flex-wrap:wrap; gap:10px 18px; margin-top:14px; }}
    .empty-state {{ margin:0; padding:28px 4px; color:var(--muted); text-align:center; border-top:1px solid var(--border); border-bottom:1px solid var(--border); }}
    .premium-panel {{ display:grid; gap:14px; }}
    .premium-toolbar {{ display:flex; align-items:flex-start; justify-content:space-between; gap:16px; padding:4px 2px 0; }}
    .premium-toolbar h2 {{ margin:0; font-size:17px; }}
    .premium-toolbar p {{ margin:3px 0 0; color:var(--muted); font-size:12px; }}
    .refresh-button {{ display:inline-flex; min-width:116px; min-height:40px; align-items:center; justify-content:center; gap:7px; padding:7px 12px; border:1px solid #176451; border-radius:5px; color:#fff; background:#176451; cursor:pointer; font-weight:700; white-space:nowrap; }}
    .refresh-button:disabled {{ cursor:wait; opacity:.65; }}
    .refresh-icon {{ font-size:18px; line-height:1; }}
    .refresh-button[aria-busy="true"] .refresh-icon {{ animation:spin 900ms linear infinite; }}
    .premium-status {{ min-height:22px; margin:0; padding:7px 10px; border-left:3px solid var(--blue); color:#42505d; background:var(--blue-soft); font-size:12px; }}
    .premium-table-wrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:6px; background:var(--surface); }}
    .premium-table {{ width:100%; min-width:760px; border-collapse:collapse; table-layout:fixed; font-variant-numeric:tabular-nums; }}
    .premium-table th,.premium-summary-row td {{ padding:8px 10px; border-bottom:1px solid #e5e9ed; text-align:right; vertical-align:middle; white-space:nowrap; }}
    .premium-table th {{ color:#52606d; background:#f7f8fa; font-size:12px; font-weight:700; }}
    .premium-table th:first-child,.premium-summary-row td:first-child {{ width:34%; text-align:left; }}
    .premium-table th:nth-child(2),.premium-summary-row td:nth-child(2) {{ width:14%; }}
    .premium-table th:nth-child(3),.premium-summary-row td:nth-child(3) {{ width:22%; }}
    .premium-table th:nth-child(4),.premium-summary-row td:nth-child(4) {{ width:18%; }}
    .premium-table th:last-child,.premium-summary-row td:last-child {{ width:12%; }}
    .premium-row-toggle {{ display:flex; width:100%; min-height:38px; align-items:center; justify-content:space-between; gap:12px; padding:0; border:0; color:var(--text); background:transparent; cursor:pointer; text-align:left; }}
    .premium-row-toggle>span:first-child {{ display:grid; min-width:0; gap:1px; }}
    .premium-row-toggle strong {{ overflow:hidden; text-overflow:ellipsis; }}
    .premium-chevron {{ width:8px; height:8px; flex:0 0 auto; border-right:2px solid #7a8793; border-bottom:2px solid #7a8793; transform:rotate(45deg); transition:transform .16s ease; }}
    .premium-row-toggle[aria-expanded="true"] .premium-chevron {{ transform:rotate(225deg); }}
    .premium-row-toggle:focus-visible {{ outline:3px solid #86b7e8; outline-offset:3px; }}
    .benchmark-compact {{ color:#4f5d69; font-size:13px; }}
    .premium-detail-row td {{ padding:0; border-bottom:1px solid #d9e0e6; background:#f8fafb; }}
    .premium-detail-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:0; padding:12px 14px 8px; }}
    .premium-detail-grid dd {{ font-size:13px; }}
    .premium-source-row {{ display:flex; flex-wrap:wrap; gap:10px 18px; margin:0 14px 12px; }}
    .premium-source-link {{ display:inline-block; font-size:12px; font-weight:700; }}
    .premium-cost {{ font-weight:750; }}
    .premium-value {{ margin-right:6px; font-weight:800; }}
    .premium-band,.stale-label {{ display:inline-block; padding:1px 4px; border-radius:3px; font-size:10px; line-height:1.5; }}
    .premium-band.band-discount {{ color:#176451; background:#e8f3ef; }}
    .premium-band.band-normal {{ color:#40505d; background:#edf0f2; }}
    .premium-band.band-elevated {{ color:#7a4b08; background:#fff4da; }}
    .premium-band.band-high {{ color:var(--negative); background:#fff0ee; }}
    .premium-value.band-discount {{ color:#176451; }}
    .premium-value.band-elevated {{ color:#8a4b08; }}
    .premium-value.band-high {{ color:var(--negative); }}
    .stale-label {{ margin-left:5px; color:#755015; background:#fff4da; }}
    .premium-item.is-unavailable {{ color:var(--muted); }}
    .nasdaq-panel {{ display:grid; gap:14px; }}
    .nasdaq-toolbar {{ padding:4px 2px 0; }}
    .nasdaq-toolbar h2 {{ margin:0; font-size:17px; }}
    .nasdaq-toolbar p {{ margin:4px 0 0; color:var(--muted); font-size:12px; line-height:1.6; }}
    .nasdaq-table-wrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:6px; background:var(--surface); }}
    .nasdaq-table {{ width:100%; min-width:980px; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
    .nasdaq-table th,.nasdaq-table td {{ padding:9px 10px; border-bottom:1px solid #e5e9ed; text-align:right; vertical-align:top; white-space:nowrap; }}
    .nasdaq-table th {{ color:#52606d; background:#f7f8fa; font-size:12px; font-weight:700; }}
    .nasdaq-table th:first-child,.nasdaq-table td:first-child,.nasdaq-table th:nth-child(2),.nasdaq-table td:nth-child(2) {{ text-align:left; }}
    .nasdaq-table td:nth-child(2) {{ white-space:normal; min-width:240px; }}
    .nasdaq-table small {{ display:block; margin-top:2px; color:var(--muted); font-size:11px; }}
    .nasdaq-warning {{ margin:0; padding:9px 10px; border-left:3px solid var(--warning); color:#705015; background:#fff8e8; font-size:12px; line-height:1.55; }}
    .warnings {{ margin-top:18px; border-top:1px solid var(--border); }}
    .warnings summary {{ display:flex; min-height:48px; align-items:center; justify-content:space-between; color:var(--warning); cursor:pointer; font-weight:700; }}
    .warnings ul {{ margin:0; padding:0 0 0 22px; color:#4c5661; }} .warnings li {{ margin:0 0 9px; }}
    footer {{ margin-top:18px; color:var(--muted); font-size:12px; }}
    [hidden] {{ display:none !important; }}
    @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
    @media (max-width:700px) {{
      .title-row {{ gap:8px; }} h1 {{ font-size:20px; }}
      .tab {{ min-height:52px; padding:5px 1px; font-size:12px; }} .tab-count {{ display:block; margin-left:0; font-size:11px; }}
      .reference-item {{ align-items:stretch; flex-direction:column; gap:10px; }} .reference-item-link {{ align-self:flex-start; }}
      .premium-toolbar {{ align-items:stretch; flex-direction:column; gap:9px; }} .refresh-button {{ align-self:flex-start; }}
      .nasdaq-table-wrap {{ overflow:visible; border:0; background:transparent; }} .nasdaq-table {{ min-width:0; }}
      .nasdaq-table thead {{ display:none; }} .nasdaq-table,.nasdaq-table tbody,.nasdaq-table tr,.nasdaq-table td {{ display:block; }}
      .nasdaq-table tr {{ margin-bottom:7px; padding:10px 11px; border:1px solid var(--border); border-radius:6px; background:var(--surface); }}
      .nasdaq-table td,.nasdaq-table td:first-child,.nasdaq-table td:nth-child(2) {{ display:grid; grid-template-columns:8.5em minmax(0,1fr); width:auto; min-width:0; padding:4px 0; border:0; text-align:left; white-space:normal; }}
      .nasdaq-table td::before {{ content:attr(data-label); color:var(--muted); font-size:11px; }}
      .nasdaq-table td:first-child::before {{ content:'排名'; }} .nasdaq-table td:nth-child(2)::before {{ content:'基金'; }}
      .premium-table-wrap {{ overflow:visible; border:0; background:transparent; }} .premium-table {{ min-width:0; table-layout:auto; }}
      .premium-table thead {{ display:none; }} .premium-table,.premium-item {{ display:block; }}
      .premium-item {{ margin-bottom:7px; border:1px solid var(--border); border-radius:6px; background:var(--surface); overflow:hidden; }}
      .premium-summary-row {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 14px; padding:9px 11px; }}
      .premium-summary-row td,.premium-summary-row td:first-child,.premium-summary-row td:nth-child(2),.premium-summary-row td:nth-child(3),.premium-summary-row td:nth-child(4),.premium-summary-row td:last-child {{ display:grid; width:auto; min-width:0; padding:0; border:0; text-align:left; white-space:normal; }}
      .premium-summary-row td::before {{ content:attr(data-label); color:var(--muted); font-size:10px; }}
      .premium-summary-row td:first-child {{ grid-column:1/-1; padding-bottom:6px; border-bottom:1px solid #e8ebee; }} .premium-summary-row td:first-child::before {{ display:none; }}
      .premium-detail-row {{ display:table-row; }} .premium-detail-row[hidden] {{ display:none; }} .premium-detail-row td {{ display:block; padding:0; border:0; }}
      .premium-detail-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px 16px; padding:11px 12px 8px; border-top:1px solid #e5e9ed; }} .premium-source-row {{ margin:0 12px 11px; }}
    }}
    @media (min-width:820px) {{ .page {{ padding-top:28px; }} .meta-grid {{ grid-template-columns:repeat(4,minmax(0,1fr)); }} .fund-item summary {{ grid-template-columns:38px minmax(210px,1fr) minmax(520px,560px) 18px; gap:12px; padding:14px 16px; }} .summary-metrics {{ grid-column:3; grid-row:1; grid-template-columns:repeat(5,minmax(0,1fr)); margin-top:0; }} .chevron {{ grid-column:4; }} .fund-detail {{ padding:18px 66px 20px; }} .detail-grid {{ grid-template-columns:repeat(4,minmax(0,1fr)); }} .rule-grid {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} }}
    @media (prefers-reduced-motion:reduce) {{ .overview-chevron,.chevron {{ transition:none; }} .refresh-button[aria-busy="true"] .refresh-icon {{ animation:none; }} }}
  </style>
</head>
<body>
  <div class="page">
    <header class="page-header">
      <div class="title-row"><h1>QDII 榜单与场内溢价</h1><time class="run-date" datetime="{html.escape(payload['run_date'], quote=True)}">{html.escape(payload['run_date'])}</time></div>
      <details class="overview-details">
        <summary class="overview-toggle"><span>筛选与数据概览</span><span class="overview-chevron" aria-hidden="true"></span></summary>
        <div class="overview-content">
          <p class="filter-line">{filter_html}</p>
          <dl class="meta-grid">
            <div><dt>机构持仓报告期</dt><dd>{html.escape(payload['holder_report_date'])}</dd></div>
            <div><dt>规模报告期</dt><dd>{html.escape(scale_dates)}</dd></div>
            <div><dt>净值区间</dt><dd>{html.escape(summarize_periods(combined, 'three_year'))}</dd></div>
            <div><dt>纳指基准更新</dt><dd>XNDX {html.escape(payload['benchmark']['index_latest_date'])} · 汇率 {html.escape(payload['benchmark']['fx_latest_date'])}</dd></div>
            <div><dt>基础候选</dt><dd>{filters['base_candidates_total']} 只</dd></div>
            <div><dt>合同扫描</dt><dd>{filters['contract_candidates_scanned']} 只</dd></div>
            <div><dt>美国 / 全球入榜</dt><dd>{len(us_records)} / {len(global_records)} 只</dd></div>
            <div><dt>数据警告</dt><dd>{len(payload['warnings'])} 项</dd></div>
          </dl>
        </div>
      </details>
    </header>
    <main>
      <div class="tabs" role="tablist" aria-label="榜单切换">
        <button class="tab" id="tab-us" type="button" role="tab" aria-selected="true" aria-controls="panel-us" data-panel="panel-us">美国主榜<span class="tab-count">{len(us_records)}</span></button>
        <button class="tab" id="tab-global" type="button" role="tab" aria-selected="false" aria-controls="panel-global" data-panel="panel-global">全球补充榜<span class="tab-count">{len(global_records)}</span></button>
        <button class="tab" id="tab-nasdaq100-otc" type="button" role="tab" aria-selected="false" aria-controls="panel-nasdaq100-otc" data-panel="panel-nasdaq100-otc">场外纳指100<span class="tab-count">{len(nasdaq_records)}</span></button>
        <button class="tab" id="tab-premium" type="button" role="tab" aria-selected="false" aria-controls="panel-premium" data-panel="panel-premium">场内溢价<span class="tab-count">{len(premium_records)}</span></button>
        <a class="tab tab-link" id="tab-valuation" href="valuation/" role="tab" aria-selected="false">估值代理<span class="tab-count">研究版</span></a>
        <button class="tab" id="tab-reference" type="button" role="tab" aria-selected="false" aria-controls="panel-reference" data-panel="panel-reference">平台参考<span class="tab-count">外部</span></button>
      </div>
      <section id="panel-us" class="ranking-list" role="tabpanel" aria-labelledby="tab-us">{render_list(us_records)}</section>
      <section id="panel-global" class="ranking-list" role="tabpanel" aria-labelledby="tab-global" hidden>{render_list(global_records)}</section>
      <section id="panel-nasdaq100-otc" class="nasdaq-panel" role="tabpanel" aria-labelledby="tab-nasdaq100-otc" hidden>
        <div class="nasdaq-toolbar"><h2>场外纳指100</h2><p>按名称匹配纳斯达克100、纳指100或 NASDAQ 100；完整展示场外人民币 A 类候选。排序依次为近两年收益、综合费率、两年跟踪误差、近三年收益、规模和代码，缺失数据排在后面。</p></div>
        <p class="nasdaq-warning">本榜按名称识别，合同基准仅作展示。名称命中不等于基金严格跟踪纳斯达克100；请展开合同基准和来源核对。</p>
        <div class="nasdaq-table-wrap"><table class="nasdaq-table"><thead><tr><th>排名</th><th>基金</th><th>申购状态</th><th>近两年</th><th>两年回撤</th><th>综合费率</th><th>两年跟踪误差</th><th>近三年</th><th>规模</th><th>合同基准</th></tr></thead><tbody>{render_nasdaq100_table(nasdaq_records)}</tbody></table></div>
      </section>
      <section id="panel-premium" class="premium-panel" role="tabpanel" aria-labelledby="tab-premium" hidden>
        <div class="premium-toolbar">
          <div><h2>场内 QDII</h2><p>按溢价从高到低排列；点击产品展开行情、综合费率和来源详情。</p></div>
          <button class="refresh-button" id="premium-refresh" type="button" aria-busy="false"><span class="refresh-icon" aria-hidden="true">↻</span><span>刷新行情</span></button>
        </div>
        <p class="premium-status" id="premium-refresh-status" role="status" aria-live="polite">{html.escape(premium_status_text)}；日报请求于 {html.escape(format_quote_time(premium['requested_at']))}，行情约延迟 {premium['quote_delay_minutes']} 分钟。</p>
        <div class="premium-table-wrap">
          <table class="premium-table">
            <thead><tr><th>{premium_product_label}</th><th>{premium_group_label}</th><th>溢价</th><th>综合费率</th><th>涨跌</th></tr></thead>
            {premium_rows}
          </table>
        </div>
      </section>
      <section id="panel-reference" class="reference-panel" role="tabpanel" aria-labelledby="tab-reference" hidden>
        <div class="reference-heading"><h2>第三方平台参考</h2><p>第三方公开榜单，仅作平台热度参考，不代表本站榜单或投资建议。</p></div>
        <div class="reference-list">
          <article class="reference-item">
            <div class="reference-copy"><span class="reference-source">天天基金</span><strong>天天基金月销量总榜</strong></div>
            <a class="reference-item-link" href="https://fund.eastmoney.com/fundhot8.html" target="_blank" rel="noopener noreferrer"><span>查看榜单</span><span aria-hidden="true">↗</span></a>
          </article>
        </div>
      </section>
      {warning_section}
    </main>
    <footer>额度为基金管理人层面的单日单基金账户上限；综合费率已从基金资产中扣除，不含场内券商佣金；场内溢价按约15分钟延迟价格相对 ETF 的 IOPV 或 LOF 的最新单位净值计算。</footer>
  </div>
  <script>
    const tabs = Array.from(document.querySelectorAll('[role="tab"][data-panel]'));
    const selectTab = (tab) => {{
      tabs.forEach((item) => {{
        const active = item === tab;
        item.setAttribute('aria-selected', String(active));
        document.getElementById(item.dataset.panel).hidden = !active;
      }});
    }};
    tabs.forEach((tab) => tab.addEventListener('click', () => selectTab(tab)));
    const requestedTab = new URLSearchParams(window.location.search).get('tab');
    const initialTab = tabs.find((tab) => tab.id === `tab-${{requestedTab}}`);
    if (initialTab) selectTab(initialTab);
    globalThis.__ETF_PREMIUM_CONFIG__ = {premium_config_json};
{browser_script}
  </script>
</body>
</html>
"""
    document = "\n".join(line.rstrip() for line in document.splitlines()) + "\n"
    atomic_write_text(path, document)
