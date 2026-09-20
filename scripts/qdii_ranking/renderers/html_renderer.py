"""HTML renderer for the ranking public contract."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from ..atomic import atomic_write_text
from ..config import ETF_MARKET_LIST_PAGE_SIZE
from .common import summarize_periods
from .html_components import (
    format_quote_time,
    render_list,
    render_nasdaq100_table,
    render_premium_row,
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

    premium_rows = "".join(
        render_premium_row(item, premium_product_label, premium_group_label)
        for item in premium_records
    )
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

    styles = (Path(__file__).with_name("ranking.css")).read_text(
        encoding="utf-8"
    ).rstrip()
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>QDII 榜单与场内溢价 · {html.escape(payload['run_date'])}</title>
  <style>
{styles}
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
      <section id="panel-us" class="ranking-list" role="tabpanel" aria-labelledby="tab-us">{render_list(us_records, payload)}</section>
      <section id="panel-global" class="ranking-list" role="tabpanel" aria-labelledby="tab-global" hidden>{render_list(global_records, payload)}</section>
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
