(function (global) {
  "use strict";

  const { normalizeQuote, normalizeResponse, shanghaiDate, shanghaiDateTime } = global.QdiiPremiumQuotes;

  function pageUrl(url, page) {
    if (/[?&]pn=\d+/.test(url)) return url.replace(/([?&]pn=)\d+/, `$1${page}`);
    return `${url}${url.includes("?") ? "&" : "?"}pn=${page}`;
  }

  async function fetchPagedQuotes(config, fetchImpl) {
    const first = await fetchWithRetry(config.refreshUrl, fetchImpl);
    if (config.refreshMode !== "paged") return first;
    const firstData = first && first.data;
    const firstRows = firstData && firstData.diff;
    if (!firstData || !Array.isArray(firstRows)) {
      throw new Error("行情分页响应缺少记录列表");
    }
    const total = Number(firstData.total);
    const pageSize = Number(config.refreshPageSize) || 100;
    if (!Number.isInteger(total) || total < firstRows.length || pageSize <= 0) {
      throw new Error("行情分页响应缺少有效总量");
    }
    const rows = firstRows.slice();
    const pageCount = Math.ceil(total / pageSize);
    for (let page = 2; page <= pageCount; page += 1) {
      const payload = await fetchWithRetry(pageUrl(config.refreshUrl, page), fetchImpl);
      const pageData = payload && payload.data;
      const pageRows = pageData && pageData.diff;
      if (!Array.isArray(pageRows)) throw new Error(`行情第${page}页缺少记录列表`);
      if (Number(pageData.total) !== total) throw new Error("行情分页总数发生变化");
      rows.push(...pageRows);
    }
    if (rows.length !== total) throw new Error("行情分页记录不完整");
    return { ...first, data: { ...firstData, diff: rows } };
  }

  async function fetchWithRetry(url, fetchImpl, options) {
    const timeoutMs = options && options.timeoutMs != null ? options.timeoutMs : 10000;
    const retryDelayMs = options && options.retryDelayMs != null ? options.retryDelayMs : 1000;
    let lastError;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetchImpl(url, {
          cache: "no-store",
          credentials: "omit",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`行情接口返回 HTTP ${response.status}`);
        return await response.json();
      } catch (error) {
        lastError = error;
        if (attempt === 0 && retryDelayMs > 0) {
          await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
        }
      } finally {
        clearTimeout(timeout);
      }
    }
    throw lastError || new Error("行情刷新失败");
  }

  function premiumBand(value) {
    if (value < 0) return { key: "discount", label: "折价" };
    if (value <= 2) return { key: "normal", label: "0–2%" };
    if (value <= 5) return { key: "elevated", label: "2–5%" };
    return { key: "high", label: ">5%高溢价" };
  }

  function formatSigned(value) {
    return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
  }

  function formatTurnover(value) {
    if (value >= 100000000) return `${(value / 100000000).toFixed(2)}亿元`;
    if (value >= 10000) return `${(value / 10000).toFixed(0)}万元`;
    return `${Math.round(value)}元`;
  }

  function setText(row, field, value) {
    const element = row.querySelector(`[data-field="${field}"]`);
    if (element) element.textContent = value;
  }

  function updateRow(item, quote) {
    item.dataset.updatedAt = quote.updatedAt;
    item.dataset.premium = String(quote.premiumPct);
    item.dataset.quoteStatus = "fresh";
    item.classList.remove("is-stale", "is-unavailable");
    setText(item, "name", quote.name);
    setText(item, "price", quote.marketPriceCny.toFixed(3));
    setText(
      item,
      "reference-label",
      quote.referenceType === "nav" && quote.referenceDate
        ? `最新单位净值（${quote.referenceDate}）`
        : "IOPV",
    );
    setText(item, "reference-value", quote.referenceValueCny.toFixed(4));
    setText(item, "premium", formatSigned(quote.premiumPct));
    setText(item, "change", formatSigned(quote.changePct));
    setText(item, "turnover", formatTurnover(quote.turnoverCny));
    setText(item, "updated", quote.updatedText);
    const premium = item.querySelector('[data-field="premium"]');
    if (premium) premium.className = `premium-value band-${premiumBand(quote.premiumPct).key}`;
    const band = item.querySelector('[data-field="band"]');
    if (band) {
      const state = premiumBand(quote.premiumPct);
      band.textContent = state.label;
      band.className = `premium-band band-${state.key}`;
    }
    const stale = item.querySelector('[data-field="stale"]');
    if (stale) stale.textContent = "";
  }

  function comparePremiumItems(left, right) {
    const leftValue = Number(left.dataset.premium);
    const rightValue = Number(right.dataset.premium);
    const leftMissing = !Number.isFinite(leftValue);
    const rightMissing = !Number.isFinite(rightValue);
    if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
    if (!leftMissing && leftValue !== rightValue) return rightValue - leftValue;
    return left.dataset.etfCode.localeCompare(right.dataset.etfCode);
  }

  function sortPremiumRows(panel) {
    const table = panel.querySelector(".premium-table");
    if (!table) return;
    const items = Array.from(table.querySelectorAll(".premium-item"));
    items.sort(comparePremiumItems);
    items.forEach((item) => table.appendChild(item));
  }

  function setupDetailToggles(panel) {
    panel.querySelectorAll(".premium-row-toggle").forEach((button) => {
      button.addEventListener("click", () => {
        const detail = panel.querySelector(`#${button.getAttribute("aria-controls")}`);
        if (!detail) return;
        const expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", String(!expanded));
        detail.hidden = expanded;
      });
    });
  }

  function serviceResponse(payload, config, asOf) {
    if (payload.error === "CATALOG_MISMATCH") throw new Error("产品目录已更新，请重新加载页面");
    if (payload.schema_version !== 1 || payload.catalog_fingerprint !== config.catalogFingerprint ||
        !["fresh", "partial", "cached_stale", "unavailable"].includes(payload.status) ||
        !Array.isArray(payload.records)) throw new Error("行情服务返回的数据未通过校验");
    const byCode = new Map(config.entries.map(e => [e.code, e]));
    const counts = new Map();
    payload.records.forEach(r => counts.set(r.code, (counts.get(r.code) || 0) + 1));
    const valid = new Map();
    for (const record of payload.records) {
      const entry = byCode.get(record.code);
      if (!entry || counts.get(record.code) !== 1 || record.status !== "fresh" ||
          !["fresh", "partial"].includes(payload.status)) continue;
      try {
        const quote = normalizeQuote({ f12: record.code, f14: record.name, f2: record.marketPriceCny,
          f402: -record.premiumPct, f3: record.changePct, f6: record.turnoverCny,
          f441: record.referenceType === "iopv" ? record.referenceValueCny : "-",
          f297: String(record.quoteDate).replaceAll("-", ""), f124: Date.parse(record.updatedAt) / 1000 },
        { ...entry, referenceType: record.referenceType, referenceValueCny: record.referenceValueCny,
          referenceDate: record.referenceDate }, asOf);
        if (entry.updatedAt && Date.parse(quote.updatedAt) < Date.parse(entry.updatedAt)) continue;
        if (entry.quoteDate && quote.quoteDate < entry.quoteDate) continue;
        if (entry.referenceType === "nav" && quote.referenceType === "nav" && quote.referenceDate < entry.referenceDate) continue;
        valid.set(quote.code, quote);
      } catch (_) { /* Keep the entire original row when evidence is invalid. */ }
    }
    return valid;
  }

  async function requestService(config, fetchImpl) {
    if (!config.serviceUrl) throw new Error("行情服务尚未启用，继续显示页面快照");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetchImpl(`${config.serviceUrl}?catalog=${encodeURIComponent(config.catalogFingerprint)}`,
        { credentials: "omit", cache: "no-store", signal: controller.signal });
      const payload = await response.json();
      if (payload.error === "CATALOG_MISMATCH") throw Object.assign(new Error("产品目录已更新，请重新加载页面"), { requestId: payload.request_id });
      if (!response.ok) throw Object.assign(new Error("行情服务暂不可用，原行情已保留"), { requestId: payload.request_id });
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("行情请求超时，原行情已保留");
      if (error.name === "SyntaxError") throw new Error("行情服务返回格式异常，原行情已保留");
      if (error instanceof TypeError || error.message === "Failed to fetch") throw new Error("无法连接行情服务，请稍后重试，原行情已保留");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function boot(config) {
    const button = document.getElementById("premium-refresh");
    const status = document.getElementById("premium-refresh-status");
    const panel = document.getElementById("panel-premium");
    if (!button || !status || !panel || !config) return;
    setupDetailToggles(panel);
    let running = false;
    button.addEventListener("click", async () => {
      if (running) return;
      running = true;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      status.textContent = "正在刷新约15分钟延迟行情…";
      let requestId;
      try {
        const payload = await requestService(config, global.fetch.bind(global));
        requestId = payload.request_id;
        const valid = serviceResponse(payload, config, shanghaiDate(new Date()));
        let updated = 0;
        valid.forEach((quote, code) => {
          const item = panel.querySelector(`[data-etf-code="${code}"]`);
          if (item && (!item.dataset.updatedAt || Date.parse(quote.updatedAt) >= Date.parse(item.dataset.updatedAt))) {
            updateRow(item, quote);
            Object.assign(config.entries.find(e => e.code === code), quote);
            updated += 1;
          }
        });
        sortPremiumRows(panel);
        const suffix = `，${config.entries.length - updated}只保留旧值`;
        const evidence = `；请求编号 ${payload.request_id || "--"}；行情约延迟15分钟，实际时间见各行`;
        const reasons = (payload.errors || []).map(e => e.reason).join(" ");
        const failure = /TIMEOUT/.test(reasons) ? "行情源请求超时" :
          /UPSTREAM_FAILURE|UPSTREAM_HTTP/.test(reasons) ? "行情源暂时不可用" :
          /REFRESH_IN_PROGRESS/.test(reasons) ? "服务正在核验行情，请稍后重试" : "暂无通过校验的新行情";
        status.textContent = updated ? `更新${updated}/${config.entries.length}只${suffix}${payload.cache_hit ? "（服务缓存）" : ""}${evidence}` :
          `${failure}，继续显示原行情及原时间${evidence}`;
      } catch (error) {
        const id = error.requestId || requestId;
        status.textContent = `${error.message || "行情刷新失败，原行情已保留"}${id ? `；请求编号 ${id}` : ""}`;
      } finally {
        running = false;
        button.disabled = false;
        button.setAttribute("aria-busy", "false");
      }
    });
  }

  const api = {
    normalizeQuote,
    normalizeResponse,
    fetchWithRetry,
    fetchPagedQuotes,
    pageUrl,
    premiumBand,
    formatTurnover,
    updateRow,
    comparePremiumItems,
    sortPremiumRows,
    setupDetailToggles,
    boot,
    serviceResponse,
    requestService,
  };
  global.QdiiPremiumRefresh = api;
  if (typeof document !== "undefined") {
    boot(global.__ETF_PREMIUM_CONFIG__);
  }
})(globalThis);
