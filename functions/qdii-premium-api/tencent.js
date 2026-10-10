"use strict";
// Verified against Tencent's own hs-fund/bundle.13362df9.js, adaptHS mapping.
const ADAPTER_VERSION = "tencent-1";
const HOST = "https://qt.gtimg.cn/?q=";
function parseTencent(text, catalog) {
  const allowed = new Map(catalog.map(e => [`${e.market_id === 1 ? "sh" : "sz"}${e.code}`, e]));
  const rows = [], errors = [], unavailable = [], seen = new Set(), duplicates = new Set();
  for (const match of text.matchAll(/v_(sh\d{6}|sz\d{6})="([^"\r\n]*)";/g)) {
    const [symbol, body] = match.slice(1), entry = allowed.get(symbol);
    if (!entry) continue;
    if (seen.has(symbol)) duplicates.add(symbol.slice(2));
    seen.add(symbol);
    try {
      const f = body.split("~");
      if (f.length < 83 || f[2] !== entry.code || !["ETF", "LOF"].includes(f[61]) || f[82] !== "CNY")
        throw new Error("TENCENT_LAYOUT_CHANGED");
      if (!/^\d{14}$/.test(f[30])) throw new Error("INVALID_QUOTE_TIME");
      const t = f[30], iso = `${t.slice(0,4)}-${t.slice(4,6)}-${t.slice(6,8)}T${t.slice(8,10)}:${t.slice(10,12)}:${t.slice(12,14)}+08:00`;
      if (!Number.isFinite(Date.parse(iso))) throw new Error("INVALID_QUOTE_TIME");
      if (new Date(Date.parse(iso) + 8 * 3600000).toISOString().slice(0,19).replace(/[-T:]/g, "") !== t)
        throw new Error("INVALID_QUOTE_TIME");
      const num = i => {
        if (!f[i]?.trim() || !Number.isFinite(Number(f[i]))) throw new Error(`INVALID_TENCENT_FIELD_${i}`);
        return Number(f[i]);
      };
      const reason = { U: "NOT_LISTED", D: "DELISTED", S: "SUSPENDED", Z: "LISTING_SUSPENDED" }[f[40]];
      if (reason && num(3) === 0 && num(57) === 0) {
        unavailable.push({ code: entry.code, reason, source: "tencent", source_status: f[40],
          source_url: `https://gu.qq.com/${symbol}`, source_updated_at: new Date(iso).toISOString() });
        continue;
      }
      if (f[61] === "ETF" && !(num(78) > 0)) throw new Error("MISSING_ETF_IOPV");
      rows.push({ f12: entry.code, f14: entry.name, f2: num(3), f3: num(32),
        f6: num(57) * 10000, f402: -num(77), f441: f[61] === "ETF" ? num(78) : "-",
        f297: t.slice(0,8), f124: Date.parse(iso) / 1000, instrumentType: f[61],
        quoteSource: "tencent", adapterVersion: ADAPTER_VERSION,
        quoteSourceUrl: `https://gu.qq.com/${symbol}`, quoteDelayMinutes: null });
    } catch (error) { errors.push({ codes: [entry.code], reason: error.message }); }
  }
  return { rows: rows.filter(r => !duplicates.has(r.f12)), unavailable: unavailable.filter(r => !duplicates.has(r.code)), errors: errors.concat(
    [...duplicates].map(code => ({ codes: [code], reason: "DUPLICATE_QUOTE" }))) };
}
module.exports = { parseTencent, ADAPTER_VERSION, HOST };
