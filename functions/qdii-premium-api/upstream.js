"use strict";
const { normalizeQuote, shanghaiDate } = require("./premium_quotes.js");
const tencent = require("./tencent.js");
const HOST = "https://push2delay.eastmoney.com/api/qt/ulist.np/get";
const FIELDS = "f2,f3,f6,f12,f13,f14,f124,f297,f402,f441";
const REFERER = "https://quote.eastmoney.com/";

async function mapLimit(items, action, limit = 3) {
  let cursor = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) await action(items[cursor++]);
  }));
}

async function getJson(url, fetchImpl, deadline, now = Date.now, format = "json", attempts = 2) {
  let error;
  for (let attempt = 0; attempt < attempts; attempt++) {
    const remaining = deadline - now();
    if (remaining <= 0) throw new Error("ROUND_TIMEOUT");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.min(8000, remaining));
    try {
      const response = await fetchImpl(url, { signal: controller.signal,
        headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36", "Cache-Control": "no-cache",
          Referer: url.startsWith(tencent.HOST) ? "https://gu.qq.com/" : url.startsWith("https://api.fund.eastmoney.com/") ? "https://fund.eastmoney.com/" : REFERER }, redirect: "error" });
      if (!response.ok) throw new Error(`UPSTREAM_HTTP_${response.status}`);
      return format === "text" ? new TextDecoder("gb18030").decode(await response.arrayBuffer()) : await response.json();
    } catch (exc) { error = exc; }
    finally { clearTimeout(timer); }
  }
  throw new Error(error?.name === "AbortError" ? "UPSTREAM_TIMEOUT" :
    `UPSTREAM_FAILURE: ${error?.message} ${error?.cause?.code || ""} ${error?.cause?.message || ""}`.trim());
}

function entryFor(item, reference = {}) {
  return { code: item.code, name: item.name, category: "qdii",
    benchmarkGroup: item.benchmark_group, ...reference };
}

function seedQuote(record, asOf) {
  return normalizeQuote({ f12: record.code, f14: record.name, f2: record.market_price_cny,
    f3: record.change_pct, f6: record.turnover_cny, f402: record.source_discount_pct,
    f441: record.reference_value_type === "iopv" ? record.reference_value_cny : "-",
    f297: String(record.quote_date).replaceAll("-", ""), f124: Date.parse(record.updated_at) / 1000,
    quoteSource: record.quote_source || "eastmoney", adapterVersion: record.adapter_version || "eastmoney-1",
    quoteSourceUrl: record.quote_source_url, quoteDelayMinutes: record.quote_source === "tencent" ? null : 15 },
  entryFor(record, { referenceType: record.reference_value_type,
    referenceValueCny: record.reference_value_cny, referenceDate: record.reference_value_date }), asOf);
}

function parseNav(payload, asOf) {
  const row = payload?.Data?.LSJZList?.[0];
  const referenceDate = row?.FSRQ;
  const referenceValueCny = Number(row?.DWJZ);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(referenceDate || "") || referenceDate > asOf ||
      !Number.isFinite(referenceValueCny) || referenceValueCny <= 0) throw new Error("INVALID_NAV_REFERENCE");
  return { referenceType: "nav", referenceDate, referenceValueCny };
}

async function refresh(catalog, { fetchImpl = fetch, now = Date.now, budgetMs = 25000,
    providers = ["tencent", "tencent", "eastmoney"], previousRecords = {} } = {}) {
  const deadline = now() + budgetMs;
  const asOf = shanghaiDate(new Date(now()));
  const quotes = {}, errors = [], unavailable = [], navRequests = new Map();
  for (const provider of providers) {
  const remaining = catalog.filter(e => !quotes[e.code] && !unavailable.some(u => u.code === e.code));
  if (!remaining.length || now() >= deadline) break;
  const rows = [];
  const batches = [];
  for (let i = 0; i < remaining.length; i += 50) batches.push(remaining.slice(i, i + 50));
  await mapLimit(batches, async (batch) => {
    const params = new URLSearchParams({ fltt: "2", invt: "2", fields: FIELDS,
      ut: "bd1d9ddb04089700cf9c27f6f7426281",
      secids: batch.map(e => `${e.market_id}.${e.code}`).join(",") });
    try {
      if (provider === "tencent") {
        const url = tencent.HOST + batch.map(e => `${e.market_id === 1 ? "sh" : "sz"}${e.code}`).join(",") + `&r=${now()}`;
        // The second Tencent pass is its single retry, for failed products only.
        // Include content failures (notably older server snapshots), not just transport errors.
        const parsed = tencent.parseTencent(await getJson(url, fetchImpl, deadline, now, "text", 1), batch);
        rows.push(...parsed.rows); errors.push(...parsed.errors.map(e => ({ ...e, source: provider })));
        unavailable.push(...parsed.unavailable.filter(u => Date.parse(u.observed_at) <= now() &&
          shanghaiDate(new Date(u.observed_at)) === asOf));
        return;
      }
      const result = await getJson(`${HOST}?${params}`, fetchImpl, deadline, now);
      if (!Array.isArray(result?.data?.diff)) throw new Error("INVALID_QUOTE_RESPONSE");
      const allowed = new Set(batch.map(e => e.code));
      rows.push(...result.data.diff.filter(r => allowed.has(String(r?.f12))).map(r => ({ ...r,
        quoteSource: "eastmoney", adapterVersion: "eastmoney-1", quoteDelayMinutes: 15,
        quoteSourceUrl: `https://quote.eastmoney.com/${r.f13 === 1 ? "sh" : "sz"}${r.f12}.html` })));
    } catch (exc) { errors.push({ codes: batch.map(e => e.code), source: provider, reason: exc.message }); }
  });
  const byCode = new Map(), duplicates = new Set();
  for (const row of rows) {
    const code = String(row.f12);
    if (byCode.has(code)) duplicates.add(code);
    byCode.set(code, row);
  }
  await mapLimit(remaining, async (item) => {
    if (unavailable.some(u => u.code === item.code)) return;
    try {
      const raw = byCode.get(item.code);
      if (duplicates.has(item.code)) throw new Error("DUPLICATE_QUOTE");
      if (!raw) throw new Error("MISSING_QUOTE");
      let reference = {};
      if (raw.f441 == null || raw.f441 === "" || raw.f441 === "-") {
        if (raw.instrumentType === "ETF" || (/ETF/i.test(item.name) && !/LOF/i.test(item.name)))
          throw new Error("MISSING_ETF_IOPV");
        const params = new URLSearchParams({ fundCode: item.code, pageIndex: "1", pageSize: "1" });
        if (!navRequests.has(item.code)) navRequests.set(item.code,
          getJson(`https://api.fund.eastmoney.com/f10/lsjz?${params}`, fetchImpl, deadline, now).then(p => parseNav(p, asOf)));
        reference = await navRequests.get(item.code);
      }
      const quote = normalizeQuote(raw, entryFor(item, reference), asOf);
      if (Date.parse(quote.updatedAt) > now()) throw new Error("FUTURE_QUOTE_TIME");
      const previous = previousRecords[item.code];
      if (previous && (Date.parse(quote.updatedAt) < Date.parse(previous.updatedAt) || quote.quoteDate < previous.quoteDate ||
          (quote.referenceType === "nav" && previous.referenceType === "nav" && quote.referenceDate < previous.referenceDate)))
        throw new Error("OLDER_QUOTE");
      quotes[item.code] = { ...quote, checkedAt: new Date(now()).toISOString() };
    } catch (exc) { errors.push({ codes: [item.code], source: provider, reason: exc.message }); }
  });
  }
  for (const item of catalog.filter(e => !quotes[e.code] && !unavailable.some(u => u.code === e.code)))
    errors.push({ codes: [item.code], reason: now() >= deadline ? "ROUND_TIMEOUT" : "NO_VALID_QUOTE" });
  return { quotes, errors, unavailable };
}
module.exports = { refresh, seedQuote, parseNav, getJson, mapLimit };
