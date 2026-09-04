(function (global) {
  "use strict";

  function finiteNumber(value, label, code) {
    const number = Number(value);
    if (!Number.isFinite(number)) {
      throw new Error(`${code} ${label} is missing or non-finite`);
    }
    return number;
  }

  function shanghaiDate(value) {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(value);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  }

  function shanghaiDateTime(value) {
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(value);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
  }

  function normalizeQuote(raw, entry, asOf) {
    const code = entry.code;
    if (!raw || String(raw.f12 || "") !== code) {
      throw new Error(`quote code differs from requested ETF ${code}`);
    }
    const price = finiteNumber(raw.f2, "price", code);
    const sourceDiscount = finiteNumber(raw.f402, "discount rate", code);
    const changePct = finiteNumber(raw.f3, "change percentage", code);
    const turnoverCny = finiteNumber(raw.f6, "turnover", code);
    const hasIopv = raw.f441 != null && raw.f441 !== "" && raw.f441 !== "-";
    const referenceType = hasIopv ? "iopv" : entry.referenceType;
    const referenceValueCny = hasIopv
      ? finiteNumber(raw.f441, "IOPV", code)
      : finiteNumber(entry.referenceValueCny, "NAV", code);
    const referenceDate = referenceType === "nav" ? String(entry.referenceDate || "") : null;
    if (!hasIopv && referenceType !== "nav") {
      throw new Error(`${code} has neither IOPV nor a verified NAV reference`);
    }
    if (referenceType === "nav" && (!/^\d{4}-\d{2}-\d{2}$/.test(referenceDate) || referenceDate > asOf)) {
      throw new Error(`${code} NAV reference date is invalid or in the future`);
    }
    if (price <= 0 || referenceValueCny <= 0 || turnoverCny < 0) {
      throw new Error(`${code} price, reference value, or turnover is outside its valid range`);
    }
    const quoteDateRaw = String(raw.f297 || "");
    if (!/^\d{8}$/.test(quoteDateRaw)) {
      throw new Error(`${code} quote date is invalid`);
    }
    const quoteDate = `${quoteDateRaw.slice(0, 4)}-${quoteDateRaw.slice(4, 6)}-${quoteDateRaw.slice(6, 8)}`;
    const updatedTimestamp = Number(raw.f124);
    const updatedDate = new Date(updatedTimestamp * 1000);
    if (!Number.isFinite(updatedTimestamp) || Number.isNaN(updatedDate.getTime())) {
      throw new Error(`${code} update timestamp is invalid`);
    }
    if (quoteDate > asOf || shanghaiDate(updatedDate) > asOf) {
      throw new Error(`${code} quote contains future data`);
    }
    const premiumPct = -sourceDiscount;
    const calculatedPremium = (price / referenceValueCny - 1) * 100;
    const tolerance = Math.max(0.05, (0.0005 / referenceValueCny) * 100 + 0.01);
    if (Math.abs(premiumPct - calculatedPremium) > tolerance) {
      throw new Error(`${code} premium differs from price/${referenceType.toUpperCase()} calculation`);
    }
    return {
      code,
      name: String(raw.f14 || entry.name || "").trim(),
      benchmarkGroup: entry.benchmarkGroup,
      marketPriceCny: price,
      referenceType,
      referenceValueCny,
      referenceDate,
      premiumPct,
      changePct,
      turnoverCny,
      quoteDate,
      updatedAt: updatedDate.toISOString(),
      updatedText: shanghaiDateTime(updatedDate),
    };
  }

  function normalizeResponse(payload, entries, asOf) {
    const rows = payload && payload.data && payload.data.diff;
    if (!Array.isArray(rows)) {
      throw new Error("行情响应缺少记录列表");
    }
    const entryByCode = new Map(entries.map((entry) => [entry.code, entry]));
    const seen = new Set();
    const valid = new Map();
    const errors = [];
    rows.forEach((raw) => {
      const code = String((raw && raw.f12) || "");
      const entry = entryByCode.get(code);
      if (!entry) return;
      if (seen.has(code)) {
        valid.delete(code);
        errors.push(`${code} 返回重复`);
        return;
      }
      seen.add(code);
      if (
        entry.category === "qdii" &&
        ["f2", "f402", "f3", "f6"].some((field) =>
          raw[field] == null || raw[field] === "" || raw[field] === "-"
        )
      ) {
        return;
      }
      try {
        valid.set(code, normalizeQuote(raw, entry, asOf));
      } catch (error) {
        errors.push(error.message);
      }
    });
    entries.forEach((entry) => {
      if (!seen.has(entry.code)) errors.push(`${entry.code} 缺失`);
    });
    return { valid, errors };
  }

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
      try {
        const payload = await fetchPagedQuotes(config, global.fetch.bind(global));
        const result = normalizeResponse(payload, config.entries, shanghaiDate(new Date()));
        result.valid.forEach((quote, code) => {
          const item = panel.querySelector(`[data-etf-code="${code}"]`);
          if (item) updateRow(item, quote);
        });
        sortPremiumRows(panel);
        if (result.valid.size === 0) throw new Error("没有场内 QDII 通过行情校验");
        const suffix = result.errors.length ? `，${result.errors.length}只保留原值` : "";
        status.textContent = `更新${result.valid.size}/${config.entries.length}只${suffix}；请求于 ${shanghaiDateTime(new Date())}，行情约延迟15分钟。`;
      } catch (error) {
        status.textContent = `刷新失败，继续显示原行情：${error.message || error}`;
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
  };
  global.QdiiPremiumRefresh = api;
  if (typeof document !== "undefined") {
    boot(global.__ETF_PREMIUM_CONFIG__);
  }
})(globalThis);
