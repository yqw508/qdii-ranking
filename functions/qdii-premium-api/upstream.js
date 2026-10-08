"use strict";
const { normalizeQuote, shanghaiDate } = require("./premium_quotes.js");
const HOST = "https://push2delay.eastmoney.com/api/qt/ulist.np/get";
const FIELDS = "f2,f3,f6,f12,f13,f14,f124,f297,f402,f441";
const REFERER = "https://quote.eastmoney.com/";

async function mapLimit(items, action, limit = 3) {
  let cursor = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) await action(items[cursor++]);
  }));
}

async function getJson(url, fetchImpl, deadline, now = Date.now) {
  let error;
  for (let attempt = 0; attempt < 2; attempt++) {
    const remaining = deadline - now();
    if (remaining <= 0) throw new Error("ROUND_TIMEOUT");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.min(8000, remaining));
    try {
      const response = await fetchImpl(url, { signal: controller.signal,
        headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
          Referer: url.startsWith("https://api.fund.eastmoney.com/") ? "https://fund.eastmoney.com/" : REFERER }, redirect: "error" });
      if (!response.ok) throw new Error(`UPSTREAM_HTTP_${response.status}`);
      return await response.json();
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
    f297: String(record.quote_date).replaceAll("-", ""), f124: Date.parse(record.updated_at) / 1000 },
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

async function refresh(catalog, { fetchImpl = fetch, now = Date.now, budgetMs = 25000 } = {}) {
  const deadline = now() + budgetMs;
  const asOf = shanghaiDate(new Date(now()));
  const quotes = {}, errors = [], rows = [];
  const batches = [];
  for (let i = 0; i < catalog.length; i += 50) batches.push(catalog.slice(i, i + 50));
  await mapLimit(batches, async (batch) => {
    const params = new URLSearchParams({ fltt: "2", invt: "2", fields: FIELDS,
      ut: "bd1d9ddb04089700cf9c27f6f7426281",
      secids: batch.map(e => `${e.market_id}.${e.code}`).join(",") });
    try {
      const result = await getJson(`${HOST}?${params}`, fetchImpl, deadline, now);
      if (!Array.isArray(result?.data?.diff)) throw new Error("INVALID_QUOTE_RESPONSE");
      const allowed = new Set(batch.map(e => e.code));
      rows.push(...result.data.diff.filter(r => allowed.has(String(r?.f12))));
    } catch (exc) { errors.push({ codes: batch.map(e => e.code), reason: exc.message }); }
  });
  const byCode = new Map(), duplicates = new Set();
  for (const row of rows) {
    const code = String(row.f12);
    if (byCode.has(code)) duplicates.add(code);
    byCode.set(code, row);
  }
  await mapLimit(catalog, async (item) => {
    try {
      const raw = byCode.get(item.code);
      if (duplicates.has(item.code)) throw new Error("DUPLICATE_QUOTE");
      if (!raw) throw new Error("MISSING_QUOTE");
      let reference = {};
      if (raw.f441 == null || raw.f441 === "" || raw.f441 === "-") {
        const params = new URLSearchParams({ fundCode: item.code, pageIndex: "1", pageSize: "1" });
        reference = parseNav(await getJson(`https://api.fund.eastmoney.com/f10/lsjz?${params}`,
          fetchImpl, deadline, now), asOf);
      }
      quotes[item.code] = { ...normalizeQuote(raw, entryFor(item, reference), asOf),
        checkedAt: new Date(now()).toISOString() };
    } catch (exc) { errors.push({ codes: [item.code], reason: exc.message }); }
  });
  return { quotes, errors };
}
module.exports = { refresh, seedQuote, parseNav, getJson, mapLimit };
