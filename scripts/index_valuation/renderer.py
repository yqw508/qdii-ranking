"""Standalone valuation HTML renderer."""

import html
import json
from typing import Any

from .common import (
    DQYDJ_SOURCE_ID,
    GOLD_SOURCE_ID,
    NDXTMC_SOURCE_ID,
    SNOWBALL_SOURCE_ID,
)
from .pipeline import _summary_values

def render_html(payload: dict[str, Any], page_script: str) -> str:
    mode_labels = {"direct": "雪球直取", "proxy": "研究代理", "external_model": "黄金模型"}
    status_labels = {"fresh": "已更新", "cached_stale": "缓存", "unavailable": "暂不可用"}
    rows = []
    for asset in payload["assets"]:
        core, percentile, rating = _summary_values(asset)
        experimental = "<span class=\"tag experimental\">实验</span>" if asset.get("method", {}).get("experimental") else ""
        rows.append(
            f"""<tr class="asset-row" data-asset-id="{html.escape(asset['id'], quote=True)}" data-source-mode="{html.escape(asset['source_mode'], quote=True)}">
              <td><a class="asset-link" data-asset-link="{html.escape(asset['id'], quote=True)}" href="?asset={html.escape(asset['id'], quote=True)}"><strong>{html.escape(asset['name'])}</strong>{experimental}<small>{html.escape(asset['code'])}</small></a></td>
              <td>{html.escape(asset['region'])}</td>
              <td><span class="mode mode-{html.escape(asset['source_mode'])}">{html.escape(mode_labels[asset['source_mode']])}</span></td>
              <td>{html.escape(core)}</td><td>{html.escape(percentile)}</td>
              <td>{html.escape(rating)}{('<small>来源评级</small>' if rating != '--' else '')}</td>
              <td>{html.escape(asset['as_of'] or '--')}</td>
              <td><span class="status status-{html.escape(asset['status'])}">{html.escape(status_labels[asset['status']])}</span><a class="detail-arrow" data-asset-link="{html.escape(asset['id'], quote=True)}" href="?asset={html.escape(asset['id'], quote=True)}" aria-label="查看{html.escape(asset['name'], quote=True)}详情"><span aria-hidden="true">→</span></a></td>
            </tr>"""
        )
    embedded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    warning_text = "；".join(payload["warnings"][:4])
    if len(payload["warnings"]) > 4:
        warning_text += f"；另有 {len(payload['warnings']) - 4} 条详见对应资产"
    banner_class = "" if payload["status"] == "fresh" else " warning"
    banner_title = {
        "fresh": "全部数据已完成本次重验",
        "partial": "部分数据使用缓存或暂不可用",
        "stale": "全部数据来自有效缓存",
        "unavailable": "估值数据暂不可用",
    }[payload["status"]]
    banner_detail = warning_text or "8 个标的均通过来源与结构校验。"
    source_links = "".join(
        f'<a href="{html.escape(source["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(source["name"])}</a>'
        for source in payload["sources"]
        if source["id"] in {
            SNOWBALL_SOURCE_ID, GOLD_SOURCE_ID, DQYDJ_SOURCE_ID,
            "nasdaq-spy", NDXTMC_SOURCE_ID,
        }
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>指数与黄金估值研究</title>
  <style>
    :root {{ color-scheme:light; --bg:#f3f5f6; --surface:#fff; --text:#19232c; --muted:#66727d; --border:#d7dde2; --blue:#1d6098; --blue-soft:#eaf2f8; --green:#176653; --green-soft:#e7f2ee; --amber:#8a520d; --amber-soft:#fff3d8; --red:#a33d35; --red-soft:#faeae7; --violet:#765078; --line:#236da7; }}
    * {{ box-sizing:border-box; }} html {{ background:var(--bg); }} [hidden] {{ display:none !important; }}
    body {{ margin:0; min-width:280px; color:var(--text); background:var(--bg); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; font-size:14px; line-height:1.5; letter-spacing:0; overflow-wrap:anywhere; }}
    a {{ color:var(--blue); text-underline-offset:3px; }} button {{ font:inherit; }}
    .page {{ width:min(100%,1180px); margin:0 auto; padding:max(16px,env(safe-area-inset-top)) max(14px,env(safe-area-inset-right)) max(30px,env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-left)); }}
    .route-tabs {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:4px; margin:0 0 18px; padding:4px; border:1px solid var(--border); border-radius:6px; background:#e8ecef; }}
    .route-tab {{ display:grid; min-height:42px; place-content:center; padding:3px; border-radius:4px; color:#42505c; font-size:13px; font-weight:700; text-align:center; text-decoration:none; }}
    .route-tab.current {{ color:var(--text); background:var(--surface); box-shadow:0 1px 2px rgba(20,32,44,.12); }} .route-tab small {{ display:block; color:var(--muted); font-size:10px; }}
    .page-header {{ display:flex; align-items:flex-end; justify-content:space-between; gap:22px; padding:2px 2px 16px; border-bottom:1px solid var(--border); }}
    h1 {{ margin:0; font-size:25px; line-height:1.25; }} .subtitle {{ max-width:720px; margin:7px 0 0; color:#46535e; }} .as-of {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
    .status-banner {{ display:grid; grid-template-columns:auto minmax(0,1fr); gap:8px 14px; margin:14px 0 0; padding:9px 11px; border-left:3px solid var(--green); color:#365248; background:var(--green-soft); font-size:12px; }}
    .status-banner.warning {{ border-left-color:var(--amber); color:#684612; background:var(--amber-soft); }}
    main {{ display:grid; gap:28px; padding-top:22px; }} section {{ min-width:0; }}
    .section-heading {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:10px; }} h2 {{ margin:0; font-size:18px; }} .section-heading span {{ color:var(--muted); font-size:12px; }}
    .filters {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }} .filter-button {{ min-height:34px; padding:5px 11px; border:1px solid var(--border); border-radius:5px; color:#44515c; background:#fff; cursor:pointer; }} .filter-button[aria-pressed="true"] {{ color:#fff; border-color:#344652; background:#344652; }}
    .table-wrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:6px; background:var(--surface); }} table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
    th,td {{ padding:10px 12px; border-bottom:1px solid #e4e8eb; text-align:right; white-space:nowrap; }} th {{ color:#52606c; background:#f7f8f9; font-size:11px; }} th:first-child,td:first-child {{ min-width:170px; text-align:left; }} tr:last-child td {{ border-bottom:0; }}
    .asset-row {{ cursor:pointer; }} .asset-row:hover {{ background:var(--blue-soft); box-shadow:inset 3px 0 var(--blue); }} .asset-row:focus-within {{ background:#f4f8fb; box-shadow:inset 3px 0 var(--blue); }} .asset-row[hidden] {{ display:none; }}
    .asset-link {{ display:block; color:var(--text); text-decoration:none; }} .asset-link:focus-visible,.detail-arrow:focus-visible,.back-link:focus-visible,select:focus-visible {{ outline:2px solid var(--blue); outline-offset:2px; }} .detail-arrow {{ display:inline-grid; width:24px; height:24px; margin-left:7px; place-items:center; border-radius:50%; color:var(--blue); text-decoration:none; font-size:17px; vertical-align:middle; }} .detail-arrow:hover {{ background:#dcebf6; }}
    td small {{ display:block; color:var(--muted); font-size:10px; }} .tag {{ display:inline-block; margin-left:6px; padding:0 4px; border:1px solid #d7b36a; border-radius:3px; color:#76510b; background:#fff7df; font-size:9px; vertical-align:2px; }}
    .mode,.status {{ display:inline-block; font-size:11px; }} .mode-direct {{ color:var(--blue); }} .mode-proxy {{ color:var(--violet); }} .mode-external_model {{ color:#85600c; }} .status-fresh {{ color:var(--green); }} .status-cached_stale {{ color:var(--amber); }} .status-unavailable {{ color:var(--red); }}
    .detail {{ min-height:360px; padding-top:2px; }} .detail-toolbar {{ display:flex; align-items:flex-end; justify-content:space-between; gap:20px; margin-bottom:18px; padding-bottom:14px; border-bottom:1px solid var(--border); }} .back-link {{ display:inline-flex; align-items:center; gap:6px; min-height:36px; color:var(--blue); font-weight:700; text-decoration:none; }} .back-link span {{ font-size:18px; }} .asset-switcher-label {{ display:grid; gap:4px; color:var(--muted); font-size:10px; font-weight:700; }} .asset-switcher-label select {{ min-width:230px; height:36px; padding:4px 32px 4px 9px; border:1px solid #bfc8cf; border-radius:5px; color:var(--text); background:var(--surface); font-size:13px; }} .detail-header {{ display:flex; align-items:flex-end; justify-content:space-between; gap:20px; padding-bottom:13px; border-bottom:1px solid var(--border); }} .detail-title {{ margin:0; font-size:22px; }} .detail-kicker {{ color:var(--muted); font-size:11px; font-weight:700; }} .detail-meta {{ color:var(--muted); font-size:12px; text-align:right; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); margin-top:14px; border-top:1px solid var(--border); border-bottom:1px solid var(--border); background:var(--surface); }} .metric {{ min-width:0; min-height:92px; padding:13px 14px; border-left:3px solid #9aa6af; }} .metric:first-child {{ border-left-color:var(--blue); }} .metric:nth-child(2) {{ border-left-color:var(--green); }} .metric-label {{ display:block; color:var(--muted); font-size:11px; }} .metric-value {{ display:block; margin-top:4px; font-size:24px; line-height:1.2; font-weight:800; font-variant-numeric:tabular-nums; }} .metric-note {{ display:block; margin-top:4px; color:var(--muted); font-size:10px; }}
    .detail-grid {{ display:grid; gap:24px; margin-top:24px; }} .chart-shell {{ position:relative; min-height:300px; border:1px solid var(--border); border-radius:6px; background:var(--surface); overflow:hidden; }} .chart-shell svg {{ display:block; width:100%; aspect-ratio:920/390; min-height:300px; }} .chart-tooltip {{ position:absolute; z-index:2; min-width:112px; padding:7px 9px; border:1px solid #b8c4cf; border-radius:4px; background:rgba(255,255,255,.97); box-shadow:0 2px 8px rgba(20,32,44,.13); pointer-events:none; font-size:12px; }} .chart-tooltip[hidden] {{ display:none; }}
    .chart-legend {{ display:flex; flex-wrap:wrap; gap:7px 18px; margin:8px 2px 0; color:var(--muted); font-size:11px; }} .legend-line {{ display:inline-block; width:18px; margin-right:6px; border-top:2px solid var(--line); vertical-align:middle; }} .legend-line.reference {{ border-top:1px dashed #83909c; }}
    .two-column {{ display:grid; grid-template-columns:minmax(260px,.8fr) minmax(0,1.2fr); gap:24px; }} .panel {{ min-width:0; }} .panel h3 {{ margin:0 0 8px; font-size:14px; }} .compact-table {{ border:1px solid var(--border); border-radius:6px; overflow:auto; background:#fff; }} .compact-table th,.compact-table td {{ padding:8px 10px; }}
    .source-list,.limitations {{ margin:0; padding:0; list-style:none; border-top:1px solid var(--border); }} .source-list li {{ display:grid; grid-template-columns:minmax(0,1fr) auto auto; align-items:center; gap:10px; min-height:50px; border-bottom:1px solid var(--border); }} .source-list small {{ color:var(--muted); }} .limitations li {{ padding:7px 2px 7px 18px; border-bottom:1px solid var(--border); position:relative; }} .limitations li::before {{ content:""; position:absolute; left:2px; top:15px; width:5px; height:5px; border-radius:50%; background:#84919b; }}
    .notice {{ padding:12px 14px; border-left:3px solid var(--amber); color:#624514; background:var(--amber-soft); }} code {{ padding:1px 4px; border-radius:3px; color:#214f70; background:#edf4fa; font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:11px; }}
    .source-directory {{ display:flex; flex-wrap:wrap; gap:8px 18px; }} footer {{ margin-top:28px; padding-top:13px; border-top:1px solid var(--border); color:var(--muted); font-size:11px; }}
    @media(max-width:760px) {{ .page-header,.detail-header,.detail-toolbar {{ align-items:stretch; flex-direction:column; gap:8px; }} .detail-meta {{ text-align:left; }} .asset-switcher-label select {{ width:100%; min-width:0; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .two-column {{ grid-template-columns:1fr; }} }}
    @media(max-width:560px) {{ h1 {{ font-size:22px; }} .route-tab {{ font-size:12px; }} .section-heading {{ align-items:flex-start; flex-direction:column; gap:4px; }} .metric {{ min-height:84px; padding:11px 9px; }} .metric-value {{ font-size:20px; }} .status-banner {{ grid-template-columns:1fr; }} .chart-shell,.chart-shell svg {{ min-height:250px; }} }}
  </style>
</head>
<body>
  <div class="page">
    <nav class="route-tabs" aria-label="页面切换">
      <a class="route-tab" href="../">美国主榜</a><a class="route-tab" href="../?tab=global">全球补充榜</a><a class="route-tab" href="../?tab=premium">场内溢价</a><span class="route-tab current" aria-current="page">估值研究<small>8 个标的</small></span>
    </nav>
    <header class="page-header"><div><h1>指数与黄金估值研究</h1><p class="subtitle">直取来源数据与研究代理分开呈现。代理值不是官方指数 PE，所有数据仅用于研究。</p></div><time class="as-of" datetime="{html.escape(payload['generated_at'], quote=True)}">生成于 {html.escape(payload['generated_at'])}</time></header>
    <div class="status-banner{banner_class}" role="status"><strong>{html.escape(banner_title)}</strong><span>{html.escape(banner_detail)}</span></div>
    <main>
      <section id="valuation-overview" aria-labelledby="overview-heading">
        <div class="section-heading"><h2 id="overview-heading">估值概览</h2><span>来源评级仅转述，不代表本站判断</span></div>
        <div class="filters" role="group" aria-label="数据类型筛选"><button class="filter-button" type="button" data-filter="all" aria-pressed="true">全部</button><button class="filter-button" type="button" data-filter="direct" aria-pressed="false">雪球直取</button><button class="filter-button" type="button" data-filter="proxy" aria-pressed="false">研究代理</button><button class="filter-button" type="button" data-filter="external_model" aria-pressed="false">黄金</button></div>
        <div class="table-wrap"><table id="asset-table"><thead><tr><th>标的</th><th>市场</th><th>来源类型</th><th>核心值</th><th>10 年分位</th><th>来源评级</th><th>数据日期</th><th>状态</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
      </section>
      <section class="detail" id="valuation-detail" aria-live="polite" hidden></section>
    </main>
    <footer><div class="source-directory">{source_links}</div><p>公开产物只包含规范化快照和派生代理序列，不镜像原始响应、图表或策略内容。</p></footer>
  </div>
  <script>window.__INDEX_VALUATION__={embedded};</script>
  <script>{page_script}</script>
</body>
</html>
"""
