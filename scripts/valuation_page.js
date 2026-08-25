(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ValuationPage = api;
    const mount = () => api.mount(root.document, root.__INDEX_VALUATION__, root);
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", mount, { once: true });
    } else {
      mount();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const WIDTH = 920;
  const HEIGHT = 390;
  const MARGIN = { top: 24, right: 62, bottom: 43, left: 54 };
  const MODE_LABELS = {
    direct: "雪球直取",
    proxy: "研究代理",
    external_model: "外部模型",
  };
  const STATUS_LABELS = {
    fresh: "已完成本次重验",
    cached_stale: "使用上次有效缓存",
    unavailable: "暂不可用",
  };

  function finiteNumber(value, label) {
    const number = Number(value);
    if (!Number.isFinite(number)) throw new Error(`${label} must be finite`);
    return number;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function resolveRoute(payload, search) {
    const params = new URLSearchParams(String(search || ""));
    if (!params.has("asset")) {
      return { view: "overview", assetId: null, invalidAsset: false };
    }
    const requested = params.get("asset");
    const ids = new Set((payload.assets || []).map((asset) => asset.id));
    return ids.has(requested)
      ? { view: "detail", assetId: requested, invalidAsset: false }
      : { view: "overview", assetId: null, invalidAsset: true };
  }

  function buildViewUrl(href, assetId) {
    const url = new URL(String(href || ""), "https://valuation.local/");
    if (assetId) url.searchParams.set("asset", assetId);
    else url.searchParams.delete("asset");
    return url.href;
  }

  function filterAssets(assets, mode) {
    return (assets || []).filter((asset) => mode === "all" || asset.source_mode === mode);
  }

  function detailKind(asset) {
    if (!asset || asset.status === "unavailable") return "unavailable";
    return asset.source_mode;
  }

  function detailDocumentTitle(asset) {
    const subject = asset.name.endsWith("估值") ? `${asset.name}详情` : `${asset.name}估值详情`;
    return `${subject} · 指数与黄金估值研究`;
  }

  function buildChartModel(history, referenceLevels) {
    if (!Array.isArray(history) || history.length < 2) {
      throw new Error("Valuation history must contain at least two points");
    }
    const values = history.map((item) => finiteNumber(item.proxy_pe_ttm, "proxy PE"));
    const references = ["p30", "p50", "p70"].map((key) => ({
      key,
      label: key.toUpperCase(),
      value: finiteNumber(referenceLevels[key], key),
    }));
    const domain = values.concat(references.map((item) => item.value));
    const rawMin = Math.min(...domain);
    const rawMax = Math.max(...domain);
    const padding = Math.max((rawMax - rawMin) * 0.1, rawMax * 0.025, 0.5);
    const yMin = Math.max(0, rawMin - padding);
    const yMax = rawMax + padding;
    const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
    const innerHeight = HEIGHT - MARGIN.top - MARGIN.bottom;
    const x = (index) => MARGIN.left + (innerWidth * index) / (history.length - 1);
    const y = (value) => MARGIN.top + ((yMax - value) * innerHeight) / (yMax - yMin);
    const points = history.map((item, index) => ({
      month: String(item.month), value: values[index], x: x(index), y: y(values[index]),
    }));
    const tickIndexes = Array.from(new Set([0, 30, 60, 90, history.length - 1]))
      .filter((index) => index < history.length);
    const yTicks = Array.from({ length: 5 }, (_, index) => {
      const value = yMin + ((yMax - yMin) * index) / 4;
      return { value, y: y(value) };
    }).reverse();
    return {
      width: WIDTH, height: HEIGHT, margin: MARGIN, points,
      references: references.map((item) => ({ ...item, y: y(item.value) })),
      tickIndexes, yTicks, baselineY: HEIGHT - MARGIN.bottom,
    };
  }

  function nearestIndex(clientX, left, renderedWidth, pointCount) {
    if (pointCount <= 1 || renderedWidth <= 0) return 0;
    const plotLeft = (MARGIN.left / WIDTH) * renderedWidth;
    const plotRight = ((WIDTH - MARGIN.right) / WIDTH) * renderedWidth;
    const relative = Math.min(plotRight, Math.max(plotLeft, clientX - left));
    return Math.max(0, Math.min(
      pointCount - 1,
      Math.round(((relative - plotLeft) / (plotRight - plotLeft)) * (pointCount - 1)),
    ));
  }

  function svgElement(document, tag, attributes, text) {
    const element = document.createElementNS(SVG_NS, tag);
    Object.entries(attributes || {}).forEach(([key, value]) => element.setAttribute(key, String(value)));
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function renderSvg(document, model, assetName) {
    const label = `${assetName} 120 月代理序列`;
    const svg = svgElement(document, "svg", {
      viewBox: `0 0 ${model.width} ${model.height}`, role: "img", tabindex: "0",
      "aria-label": label, preserveAspectRatio: "xMidYMid meet",
    });
    svg.appendChild(svgElement(document, "title", {}, label));
    model.yTicks.forEach((tick) => {
      svg.appendChild(svgElement(document, "line", {
        x1: model.margin.left, x2: model.width - model.margin.right,
        y1: tick.y, y2: tick.y, stroke: "#e1e6ea", "stroke-width": 1,
      }));
      svg.appendChild(svgElement(document, "text", {
        x: model.margin.left - 9, y: tick.y + 4, fill: "#66727e",
        "font-size": 11, "text-anchor": "end",
      }, tick.value.toFixed(1)));
    });
    model.references.forEach((reference) => {
      svg.appendChild(svgElement(document, "line", {
        x1: model.margin.left, x2: model.width - model.margin.right,
        y1: reference.y, y2: reference.y, stroke: "#83909c",
        "stroke-width": 1, "stroke-dasharray": "5 5",
      }));
      svg.appendChild(svgElement(document, "text", {
        x: model.width - model.margin.right + 8, y: reference.y + 4,
        fill: "#56636f", "font-size": 10,
      }, reference.label));
    });
    model.tickIndexes.forEach((index) => {
      const point = model.points[index];
      svg.appendChild(svgElement(document, "line", {
        x1: point.x, x2: point.x, y1: model.baselineY,
        y2: model.baselineY + 5, stroke: "#9aa7b3", "stroke-width": 1,
      }));
      svg.appendChild(svgElement(document, "text", {
        x: point.x, y: model.baselineY + 20, fill: "#66727e", "font-size": 10,
        "text-anchor": index === 0 ? "start" : (index === model.points.length - 1 ? "end" : "middle"),
      }, point.month));
    });
    const linePath = model.points
      .map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(2)},${point.y.toFixed(2)}`)
      .join(" ");
    const first = model.points[0];
    const last = model.points[model.points.length - 1];
    svg.appendChild(svgElement(document, "path", {
      d: `${linePath} L${last.x.toFixed(2)},${model.baselineY} L${first.x.toFixed(2)},${model.baselineY} Z`,
      fill: "#edf4fa", stroke: "none",
    }));
    svg.appendChild(svgElement(document, "path", {
      d: linePath, fill: "none", stroke: "#1f6fae", "stroke-width": 2.5,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    const guide = svgElement(document, "line", {
      x1: last.x, x2: last.x, y1: model.margin.top, y2: model.baselineY,
      stroke: "#52606d", "stroke-width": 1, "stroke-dasharray": "3 4",
      visibility: "hidden", "data-chart-guide": "",
    });
    const marker = svgElement(document, "circle", {
      cx: last.x, cy: last.y, r: 4.5, fill: "#fff", stroke: "#1f6fae",
      "stroke-width": 2.5, "data-chart-marker": "",
    });
    svg.appendChild(guide);
    svg.appendChild(marker);
    svg.appendChild(svgElement(document, "rect", {
      x: model.margin.left, y: model.margin.top,
      width: model.width - model.margin.left - model.margin.right,
      height: model.height - model.margin.top - model.margin.bottom,
      fill: "transparent", "data-chart-overlay": "",
    }));
    return svg;
  }

  function mountChart(document, asset) {
    const root = document.getElementById("valuation-chart");
    if (!root || asset.source_mode !== "proxy" || asset.status === "unavailable") return null;
    const model = buildChartModel(asset.history, asset.current.reference_levels);
    const tooltip = document.createElement("div");
    tooltip.id = "valuation-tooltip";
    tooltip.className = "chart-tooltip";
    tooltip.setAttribute("role", "status");
    tooltip.setAttribute("aria-live", "polite");
    tooltip.hidden = true;
    const svg = renderSvg(document, model, asset.name);
    root.replaceChildren(svg, tooltip);
    const guide = svg.querySelector("[data-chart-guide]");
    const marker = svg.querySelector("[data-chart-marker]");
    let selectedIndex = model.points.length - 1;
    const showPoint = (index, clientPosition) => {
      selectedIndex = Math.max(0, Math.min(model.points.length - 1, index));
      const point = model.points[selectedIndex];
      guide.setAttribute("x1", point.x);
      guide.setAttribute("x2", point.x);
      guide.setAttribute("visibility", "visible");
      marker.setAttribute("cx", point.x);
      marker.setAttribute("cy", point.y);
      tooltip.textContent = `${point.month}  ·  ${point.value.toFixed(2)}`;
      tooltip.hidden = false;
      const rect = root.getBoundingClientRect();
      const renderedX = clientPosition === undefined
        ? (point.x / model.width) * rect.width : clientPosition - rect.left;
      tooltip.style.left = `${Math.max(8, Math.min(rect.width - 132, renderedX + 10))}px`;
      tooltip.style.top = "10px";
    };
    const hidePoint = () => {
      guide.setAttribute("visibility", "hidden");
      tooltip.hidden = true;
    };
    svg.addEventListener("pointermove", (event) => {
      const rect = svg.getBoundingClientRect();
      showPoint(nearestIndex(event.clientX, rect.left, rect.width, model.points.length), event.clientX);
    });
    svg.addEventListener("pointerleave", hidePoint);
    svg.addEventListener("focus", () => showPoint(selectedIndex));
    svg.addEventListener("blur", hidePoint);
    svg.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      showPoint(selectedIndex + (event.key === "ArrowLeft" ? -1 : 1));
    });
    return { model, svg, showPoint, hidePoint };
  }

  function metric(label, value, note) {
    return `<div class="metric"><span class="metric-label">${escapeHtml(label)}</span><strong class="metric-value">${escapeHtml(value)}</strong><span class="metric-note">${escapeHtml(note || "")}</span></div>`;
  }

  function sourceList(asset, payload) {
    const map = new Map(payload.sources.map((source) => [source.id, source]));
    return `<ul class="source-list">${asset.source_ids.map((id) => {
      const source = map.get(id);
      if (!source) return "";
      const stamp = source.last_success_at || "无有效缓存";
      return `<li><span><strong>${escapeHtml(source.name)}</strong><small>${escapeHtml(stamp)}</small></span><span class="status status-${escapeHtml(source.status)}">${escapeHtml(STATUS_LABELS[source.status])}</span><a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">来源</a></li>`;
    }).join("")}</ul>`;
  }

  function limitations(method) {
    return `<ul class="limitations">${(method.limitations || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
  }

  function detailHeader(asset) {
    const experimental = asset.method.experimental ? " · 实验版" : "";
    return `<div class="detail-header"><div><span class="detail-kicker">${escapeHtml(MODE_LABELS[asset.source_mode])}${escapeHtml(experimental)}</span><h2 class="detail-title" id="detail-heading">${escapeHtml(asset.name)}</h2></div><div class="detail-meta"><strong class="status status-${escapeHtml(asset.status)}">${escapeHtml(STATUS_LABELS[asset.status])}</strong><br>${escapeHtml(asset.code)} · ${escapeHtml(asset.as_of || "无数据日期")}</div></div>`;
  }

  function renderDetailNavigation(asset, payload, currentHref) {
    const groups = [
      ["direct", "雪球直取"],
      ["proxy", "研究代理"],
      ["external_model", "黄金"],
    ];
    const options = groups.map(([mode, label]) => {
      const groupAssets = payload.assets.filter((item) => item.source_mode === mode);
      if (!groupAssets.length) return "";
      return `<optgroup label="${escapeHtml(label)}">${groupAssets.map((item) => `<option value="${escapeHtml(item.id)}"${item.id === asset.id ? " selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</optgroup>`;
    }).join("");
    return `<div class="detail-toolbar"><a class="back-link" id="valuation-overview-link" href="${escapeHtml(buildViewUrl(currentHref, null))}"><span aria-hidden="true">←</span> 返回估值概览</a><label class="asset-switcher-label" for="asset-switcher"><span>切换标的</span><select id="asset-switcher">${options}</select></label></div>`;
  }

  function renderUnavailable(asset, payload) {
    return `${detailHeader(asset)}<div class="detail-grid"><div class="notice"><strong>该标的暂不可用</strong><br>${escapeHtml((asset.warnings || []).join("；") || "来源抓取失败且没有通过校验的缓存。")}</div><div class="panel"><h3 id="sources-heading">来源状态</h3>${sourceList(asset, payload)}</div></div>`;
  }

  function renderDirect(asset, payload) {
    const current = asset.current;
    return `${detailHeader(asset)}
      <div class="metrics">${metric("PE-TTM", current.pe_ttm.toFixed(2), "雪球当前快照")}${metric("10 年 PE 分位", `${current.pe_percentile_10y.toFixed(1)}%`, `来源评级：${current.source_rating.label}`)}${metric("PB", current.pb_mrq.toFixed(2), `${current.pb_percentile_10y.toFixed(1)}% · 10 年 PB 分位`)}${metric("ROE / 股息率", `${current.roe_pct.toFixed(1)}% / ${current.dividend_yield_pct.toFixed(1)}%`, `序列始于 ${current.history_since}`)}</div>
      <div class="detail-grid"><div class="notice"><strong>当前快照</strong><br>雪球仅提供当前估值与其计算的 10 年百分位，本页没有可发布的历史估值曲线。</div><div class="two-column"><div class="panel"><h3 id="sources-heading">来源状态</h3>${sourceList(asset, payload)}</div><div class="panel"><h3 id="method-heading">口径与边界</h3><p><code>${escapeHtml(asset.method.id)}</code></p>${limitations(asset.method)}</div></div></div>`;
  }

  function renderProxy(asset, payload) {
    const current = asset.current;
    const levels = current.reference_levels;
    const anchor = asset.method.anchor;
    const recent = asset.history.slice(-12).reverse().map((item) => `<tr><td>${escapeHtml(item.month)}</td><td>${item.proxy_pe_ttm.toFixed(2)}</td></tr>`).join("");
    return `${detailHeader(asset)}
      <div class="metrics">${metric("代理 PE-TTM", current.proxy_pe_ttm.toFixed(2), asset.as_of)}${metric("10 年代理百分位", `${current.proxy_percentile_10y.toFixed(1)}%`, "中位秩算法，不附估值标签")}${metric("完整样本", `${current.sample_count} 个月`, "连续且已结束的自然月")}${metric("P30 / P50 / P70", `${levels.p30.toFixed(2)} / ${levels.p50.toFixed(2)} / ${levels.p70.toFixed(2)}`, "参考分位值")}</div>
      <div class="detail-grid"><section aria-labelledby="history-heading"><div class="section-heading"><h3 id="history-heading">120 月代理序列</h3><span>${escapeHtml(asset.history[0].month)} 至 ${escapeHtml(asset.history.at(-1).month)}</span></div><div class="chart-shell" id="valuation-chart"></div><div class="chart-legend"><span><i class="legend-line"></i>代理 PE-TTM</span><span><i class="legend-line reference"></i>P30 / P50 / P70</span></div></section>
      <div class="two-column"><div class="panel"><h3>最近 12 个月</h3><div class="compact-table"><table><thead><tr><th>月份</th><th>代理 PE-TTM</th></tr></thead><tbody>${recent}</tbody></table></div></div><div class="panel"><h3 id="sources-heading">来源状态</h3>${sourceList(asset, payload)}</div></div>
      <div class="two-column"><div class="panel"><h3 id="method-heading">方法与锚点</h3><p><code>${escapeHtml(asset.method.id)}</code></p><p>${escapeHtml(asset.method.formula)}</p><p>锚点 ${escapeHtml(anchor.month)} · ${escapeHtml(anchor.publisher)} PE ${Number(anchor.pe_ttm).toFixed(2)} · <a href="${escapeHtml(anchor.source_url)}" target="_blank" rel="noopener noreferrer">锚点来源</a></p></div><div class="panel"><h3>限制</h3>${limitations(asset.method)}</div></div></div>`;
  }

  function renderGold(asset, payload) {
    const current = asset.current;
    const labels = { "1y": "近 1 年", "3y": "近 3 年", "5y": "近 5 年", "10y": "近 10 年", all: "全部历史" };
    const percentileRows = Object.entries(labels).map(([key, label]) => {
      const rating = current.source_rating_1y && key === "1y" ? current.source_rating_1y.label : (key === "all" ? current.source_rating_all.label : "--");
      return `<tr><td>${label}</td><td>${current.percentiles[key].toFixed(1)}%</td><td>${escapeHtml(rating)}</td></tr>`;
    }).join("");
    const factorRows = Object.values(current.factors).map((factor) => `<tr><td>${escapeHtml(factor.label)}</td><td>${Number(factor.value).toFixed(factor.unit === "ratio" ? 1 : 2)}${factor.unit === "%" ? "%" : ""}</td><td>${escapeHtml(factor.date)}</td><td>${factor.lag_days} 天</td></tr>`).join("");
    return `${detailHeader(asset)}
      <div class="metrics">${metric("美元金价", `$${current.spot_usd_oz.toLocaleString("en-US", { maximumFractionDigits: 2 })}`, "美元 / 盎司")}${metric("10 年模型分位", `${current.percentiles["10y"].toFixed(1)}%`, "来源模型计算")}${metric("全部历史分位", `${current.percentiles.all.toFixed(1)}%`, `来源评级：${current.source_rating_all.label}`)}${metric("当前残差", current.residual.toFixed(4), "来源回归模型")}</div>
      <div class="detail-grid"><div class="notice"><strong>外部模型快照</strong><br>来源未发布可供本站复用的历史序列。本页不复制来源图表、回测、仓位建议或买卖策略。</div>
      <div class="two-column"><div class="panel"><h3>多窗口模型分位</h3><div class="compact-table"><table><thead><tr><th>窗口</th><th>分位</th><th>来源评级</th></tr></thead><tbody>${percentileRows}</tbody></table></div></div><div class="panel"><h3>因子与新鲜度</h3><div class="compact-table"><table><thead><tr><th>因子</th><th>数值</th><th>日期</th><th>滞后</th></tr></thead><tbody>${factorRows}</tbody></table></div></div></div>
      <div class="two-column"><div class="panel"><h3 id="sources-heading">来源状态</h3>${sourceList(asset, payload)}</div><div class="panel"><h3 id="method-heading">归属与边界</h3><p><code>${escapeHtml(asset.method.id)}</code></p><p><a href="${escapeHtml(asset.method.attribution_url)}" target="_blank" rel="noopener noreferrer">模型原页</a> · <a href="${escapeHtml(asset.method.repository_url)}" target="_blank" rel="noopener noreferrer">项目仓库</a></p>${limitations(asset.method)}</div></div></div>`;
  }

  function renderDetail(asset, payload) {
    const kind = detailKind(asset);
    if (kind === "unavailable") return renderUnavailable(asset, payload);
    if (kind === "direct") return renderDirect(asset, payload);
    if (kind === "proxy") return renderProxy(asset, payload);
    return renderGold(asset, payload);
  }

  function mount(document, payload, windowObject) {
    if (!document || !payload || !Array.isArray(payload.assets)) return null;
    const overview = document.getElementById("valuation-overview");
    const detail = document.getElementById("valuation-detail");
    const rows = Array.from(document.querySelectorAll("[data-asset-id]"));
    const filters = Array.from(document.querySelectorAll("[data-filter]"));
    const assetLinks = Array.from(document.querySelectorAll("[data-asset-link]"));
    if (!overview || !detail || !rows.length) return null;
    const assets = new Map(payload.assets.map((asset) => [asset.id, asset]));
    const currentHref = windowObject && windowObject.location
      ? windowObject.location.href
      : "https://valuation.local/valuation/";
    const search = windowObject && windowObject.location ? windowObject.location.search : "";
    const route = resolveRoute(payload, search);
    let activeFilter = "all";

    assetLinks.forEach((link) => {
      link.setAttribute("href", buildViewUrl(currentHref, link.dataset.assetLink));
    });

    const applyFilter = (mode) => {
      activeFilter = mode;
      rows.forEach((row) => { row.hidden = mode !== "all" && row.dataset.sourceMode !== mode; });
      filters.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.filter === mode)));
    };
    rows.forEach((row) => {
      row.addEventListener("click", (event) => {
        if (event.target.closest("a,button,select")) return;
        const target = buildViewUrl(currentHref, row.dataset.assetId);
        if (windowObject && windowObject.location && typeof windowObject.location.assign === "function") {
          windowObject.location.assign(target);
        }
      });
    });
    filters.forEach((button) => button.addEventListener("click", () => applyFilter(button.dataset.filter)));

    if (route.invalidAsset && windowObject && windowObject.history) {
      windowObject.history.replaceState(null, "", buildViewUrl(currentHref, null));
    }
    if (route.view === "detail") {
      const asset = assets.get(route.assetId);
      overview.hidden = true;
      detail.hidden = false;
      detail.innerHTML = `${renderDetailNavigation(asset, payload, currentHref)}${renderDetail(asset, payload)}`;
      document.title = detailDocumentTitle(asset);
      mountChart(document, asset);
      const switcher = document.getElementById("asset-switcher");
      switcher.addEventListener("change", () => {
        const target = buildViewUrl(currentHref, switcher.value);
        if (windowObject && windowObject.location && typeof windowObject.location.assign === "function") {
          windowObject.location.assign(target);
        }
      });
    } else {
      overview.hidden = false;
      detail.hidden = true;
      detail.replaceChildren();
      document.title = "指数与黄金估值研究";
      applyFilter(activeFilter);
    }
    return { route, applyFilter, get activeFilter() { return activeFilter; } };
  }

  return {
    buildChartModel, nearestIndex, resolveRoute, buildViewUrl, filterAssets, detailDocumentTitle,
    detailKind, renderDetailNavigation, renderDetail, mountChart, mount,
  };
});
