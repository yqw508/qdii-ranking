import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import http from "node:http";
import { readFileSync } from "node:fs";
const require = createRequire(import.meta.url);
const { createService, validateSnapshot } = require("../../functions/qdii-premium-api/service.js");
const { refresh, getJson, parseNav } = require("../../functions/qdii-premium-api/upstream.js");
const { createStore } = require("../../functions/qdii-premium-api/store.js");
const { handler, SITE } = require("../../functions/qdii-premium-api/server.js");
const { normalizeQuote } = require("../../functions/qdii-premium-api/premium_quotes.js");
const asOf = "2026-10-08";
const time = Date.parse("2026-10-08T04:00:00Z");
const entry = { code: "513500", market_id: 1, name: "ETF", benchmark_group: "QDII" };
const raw = { f12: "513500", f14: "ETF", f2: 1.1, f3: 1, f6: 10000, f441: 1, f402: -10,
  f297: 20261008, f124: time / 1000 };
const quote = { ...normalizeQuote(raw, entry, asOf), checkedAt: new Date(time).toISOString() };
function snapshot(catalog = [entry]) {
  return { schema_version: 1, catalog, records: [], catalog_fingerprint: createHash("sha256")
    .update(JSON.stringify(catalog.map(e => [e.code, e.market_id, e.name, e.benchmark_group]))).digest("hex") };
}
function fakeDb() {
  const state = new Map();
  let queue = Promise.resolve();
  return { state, runTransaction(callback) {
    const action = queue.then(() => callback({ collection: () => ({ doc: key => ({
      get: async () => ({ data: state.has(key) ? [{ _id: key, ...structuredClone(state.get(key)) }] : [] }),
      set: async value => { state.set(key, structuredClone(value)); }
    }) }) }));
    queue = action.catch(() => {}); return action;
  } };
}

test("snapshot fingerprint catches changes and duplicates", () => {
  assert.doesNotThrow(() => validateSnapshot(snapshot()));
  const invalid = snapshot(); invalid.catalog[0] = { ...entry, code: "000001" };
  assert.throws(() => validateSnapshot(invalid), /CATALOG/);
  assert.throws(() => validateSnapshot(snapshot([entry, entry])), /CATALOG/);
});
test("batches use fixed upstream and fetch a fresh NAV for LOFs", async () => {
  const calls = [];
  const result = await refresh([{ ...entry, name: "LOF" }], { providers: ["eastmoney"], now: () => time, fetchImpl: async url => {
    calls.push(url);
    return { ok: true, json: async () => url.includes("lsjz") ?
      { Data: { LSJZList: [{ FSRQ: "2026-10-07", DWJZ: "1.0" }] } } :
      { data: { diff: [{ ...raw, f441: "-" }] } } };
  } });
  assert.equal(result.quotes[entry.code].referenceDate, "2026-10-07");
  assert.equal(calls.length, 2);
  assert.ok(calls[0].startsWith("https://push2delay.eastmoney.com/"));
  assert.throws(() => parseNav({ Data: { LSJZList: [{ FSRQ: "2026-10-09", DWJZ: 1 }] } }, asOf));
});
test("duplicate, missing and inconsistent rows never become valid quotes", async () => {
  for (const rows of [[raw, raw], [], [{ ...raw, f2: 0 }], [{ ...raw, f402: 0 }], [{ ...raw, f297: 20261009 }]]) {
    const result = await refresh([entry], { now: () => time,
      fetchImpl: async () => ({ ok: true, json: async () => ({ data: { diff: rows } }) }) });
    assert.deepEqual(result.quotes, {}); assert.ok(result.errors.length);
  }
});
test("502, disconnect and timeout are bounded and retried once", async () => {
  for (const response of [() => ({ ok: false, status: 502 }), () => { throw new TypeError("fetch failed"); }]) {
    let calls = 0;
    await assert.rejects(getJson("https://example.test", async () => { calls++; return response(); }, time + 50, () => time));
    assert.equal(calls, 2);
  }
  await assert.rejects(getJson("https://example.test", async (_url, opts) => new Promise((_, reject) => {
    opts.signal.addEventListener("abort", () => reject(Object.assign(new Error("timeout"), { name: "AbortError" })));
  }), Date.now() + 5), /TIMEOUT/);
});
test("success cache, failure cooldown and monotonic quotes preserve evidence", async () => {
  const db = fakeDb(), seed = snapshot(); let now = time, calls = 0;
  let next = { quotes: { [entry.code]: quote }, errors: [] };
  const service = createService({ snapshot: seed, store: createStore(db), now: () => now,
    log: () => {}, refreshImpl: async () => { calls++; return structuredClone(next); } });
  assert.equal((await service.get(seed.catalog_fingerprint)).status, "fresh");
  now += 59000; assert.equal((await service.get(seed.catalog_fingerprint)).cache_hit, true); assert.equal(calls, 1);
  now += 1001; next = { quotes: {}, errors: [{ reason: "UPSTREAM_HTTP_502", codes: [entry.code] }] };
  const stale = await service.get(seed.catalog_fingerprint);
  assert.equal(stale.status, "cached_stale"); assert.equal(stale.records[0].updatedAt, quote.updatedAt);
  now += 29000; await service.get(seed.catalog_fingerprint); assert.equal(calls, 2);
  now += 1001; next = { quotes: { [entry.code]: { ...quote, updatedAt: "2026-10-07T01:00:00Z" } }, errors: [] };
  const older = await service.get(seed.catalog_fingerprint);
  assert.equal(older.status, "cached_stale"); assert.equal(older.records[0].updatedAt, quote.updatedAt);
  assert.equal(older.errors[0].reason, "OLDER_QUOTE");
});
test("same-process requests merge and cross-instance locks prevent duplicate fetches", async () => {
  const seed = snapshot(), db = fakeDb(); let calls = 0, release;
  const pending = new Promise(resolve => { release = resolve; });
  const options = { snapshot: seed, store: createStore(db), now: () => time, log: () => {},
    refreshImpl: async () => { calls++; await pending; return { quotes: { [entry.code]: quote }, errors: [] }; } };
  const a = createService(options), b = createService(options);
  const first = a.get(seed.catalog_fingerprint), second = a.get(seed.catalog_fingerprint);
  await new Promise(resolve => setImmediate(resolve));
  const concurrent = await b.get(seed.catalog_fingerprint);
  assert.equal(calls, 1); assert.equal(concurrent.errors[0].reason, "REFRESH_IN_PROGRESS");
  release(); const results = await Promise.all([first, second]);
  assert.ok(results.every(r => r.status === "fresh")); assert.equal(results[1].cache_hit, true);
});
test("cold failure is unavailable and version mismatch never fetches", async () => {
  const seed = snapshot(); let calls = 0;
  const service = createService({ snapshot: seed, store: createStore(fakeDb()), now: () => time, log: () => {},
    refreshImpl: async () => { calls++; throw new Error("OFFLINE"); } });
  assert.equal((await service.get("wrong")).http_status, 409); assert.equal(calls, 0);
  assert.equal((await service.get(seed.catalog_fingerprint)).status, "unavailable");
});
test("partial refresh retains a whole failed record and lease expires after a crash", async () => {
  const second = { ...entry, code: "159100", market_id: 0 };
  const seed = snapshot([entry, second]), db = fakeDb(), store = createStore(db);
  let now = time;
  const existing = { ...quote, code: second.code, referenceValueCny: 1.0 };
  db.state.set(seed.catalog_fingerprint, { records: { [second.code]: existing }, fresh_codes: [],
    next_attempt: 0, lock_until: time + 35000, lock_owner: "crashed", checked_at: null, errors: [] });
  let calls = 0;
  const service = createService({ snapshot: seed, store, now: () => now, log: () => {},
    refreshImpl: async () => { calls++; return { quotes: { [entry.code]: quote }, errors: [{ codes: [second.code], reason: "NAV_FAILED" }] }; } });
  assert.equal((await service.get(seed.catalog_fingerprint)).status, "cached_stale");
  assert.equal(calls, 0); now += 35001;
  const result = await service.get(seed.catalog_fingerprint);
  assert.equal(result.status, "partial"); assert.equal(calls, 1);
  const retained = result.records.find(r => r.code === second.code);
  assert.equal(retained.updatedAt, existing.updatedAt); assert.equal(retained.referenceValueCny, 1.0);
  assert.equal(retained.status, "cached_stale");
});
test("upstream requests never exceed concurrency three", async () => {
  const catalog = Array.from({ length: 151 }, (_, i) => ({ ...entry, code: String(100000 + i) }));
  let active = 0, max = 0;
  await refresh(catalog, { now: () => time, fetchImpl: async () => {
    active++; max = Math.max(max, active); await new Promise(resolve => setImmediate(resolve)); active--;
    return { ok: true, json: async () => ({ data: { diff: [] } }) };
  } });
  assert.equal(max, 3);
});
test("HTTP allows only the site origin and fixed read-only route", async t => {
  const seed = snapshot();
  const service = createService({ snapshot: seed, store: createStore(fakeDb()), now: () => time, log: () => {},
    refreshImpl: async () => ({ quotes: { [entry.code]: quote }, errors: [] }) });
  const server = http.createServer(handler(service)); await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const url = `http://127.0.0.1:${server.address().port}/premium/v1/quotes?catalog=${seed.catalog_fingerprint}`;
  const ok = await fetch(url, { headers: { Origin: SITE } }); assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("access-control-allow-origin"), SITE);
  assert.equal((await fetch(url, { headers: { Origin: "https://untrusted.test" } })).status, 403);
  assert.equal((await fetch(url, { method: "POST" })).status, 405);
  assert.equal((await fetch(url + "&url=https://untrusted.test")).status, 400);
});

const fixture = JSON.parse(readFileSync(new URL("../fixtures/tencent-quotes.json", import.meta.url), "utf8"));
const { parseTencent } = require("../../functions/qdii-premium-api/tencent.js");
const sampleCatalog = fixture.records.map(r => ({ code: r.fields[2], name: r.fields[61],
  market_id: r.symbol.startsWith("sh") ? 1 : 0, benchmark_group: "QDII" }));
const sampleText = records => records.map(r => `v_${r.symbol}="${r.fields.join("~")}";`).join("\n");
const textResponse = text => ({ ok: true, arrayBuffer: async () => new TextEncoder().encode(text).buffer });
const fixtureNow = Date.parse(fixture.now);

test("Tencent official field mapping covers SH/SZ ETF, LOF and documented unlisted exception", async () => {
  const calls = [];
  const result = await refresh(sampleCatalog, { now: () => fixtureNow, fetchImpl: async url => {
    calls.push(url);
    return url.includes("lsjz") ? { ok: true, json: async () => fixture.lof_nav } : textResponse(sampleText(fixture.records));
  } });
  assert.equal(Object.keys(result.quotes).length, 3); assert.deepEqual(result.errors, []);
  assert.equal(result.quotes["513500"].quoteSource, "tencent");
  assert.equal(result.quotes["513500"].referenceType, "iopv");
  assert.equal(result.quotes["513500"].turnoverCny, Number(fixture.records[0].fields[57]) * 10000);
  assert.equal(result.quotes["159941"].referenceType, "iopv");
  assert.equal(result.quotes["161125"].referenceDate, "2026-09-29");
  assert.equal(result.unavailable[0].code, "501226");
  assert.equal(result.unavailable[0].reason, "NOT_LISTED");
  assert.equal(calls.length, 2); assert.ok(calls[0].startsWith("https://qt.gtimg.cn/"));
});

test("Tencent duplicates, shifted fields, missing IOPV and impossible dates fail closed", () => {
  const base = fixture.records[0];
  for (const [field, value] of [[61, "stock"], [82, "USD"], [78, ""], [3, "NaN"], [30, "20260230090000"]]) {
    const r = structuredClone(base); r.fields[field] = value;
    const parsed = parseTencent(sampleText([r]), sampleCatalog);
    assert.equal(parsed.rows.length, 0); assert.equal(parsed.unavailable.length, 0); assert.ok(parsed.errors.length);
  }
  const dup = parseTencent(sampleText([base, base]), sampleCatalog);
  assert.equal(dup.rows.length, 0); assert.match(dup.errors[0].reason, /DUPLICATE/);
});

test("only failed products fall back, using a whole independent quote", async () => {
  const records = structuredClone(fixture.records.slice(0,2)); records[1].fields[78] = "";
  const calls = [];
  const result = await refresh(sampleCatalog.slice(0,2), { now: () => fixtureNow, fetchImpl: async url => {
    calls.push(url);
    if (url.includes("qt.gtimg")) return textResponse(sampleText(records));
    return { ok: true, json: async () => ({ data: { diff: [{ ...raw, f12: "159941", f13: 0 }] } }) };
  } });
  assert.equal(result.quotes["513500"].quoteSource, "tencent");
  assert.equal(result.quotes["159941"].quoteSource, "eastmoney");
  assert.equal(result.quotes["159941"].marketPriceCny, raw.f2);
  assert.equal(result.quotes["159941"].referenceValueCny, raw.f441);
  assert.match(decodeURIComponent(calls[2]), /secids=0.159941/);
  assert.ok(!calls[1].includes("513500"));
  assert.ok(!calls[2].includes("513500"));
});

test("NAV mismatch, future time and wrong premium do not pass or refresh old records", async () => {
  for (const mutate of [r => { r.fields[77] = "99"; }, r => { r.fields[30] = "20991008090000"; }]) {
    const r = structuredClone(fixture.records[0]); mutate(r);
    const result = await refresh([sampleCatalog[0]], { now: () => fixtureNow, providers: ["tencent"],
      fetchImpl: async () => textResponse(sampleText([r])) });
    assert.deepEqual(result.quotes, {}); assert.ok(result.errors.length);
  }
  const result = await refresh([sampleCatalog[2]], { now: () => fixtureNow, providers: ["tencent"],
    fetchImpl: async url => url.includes("lsjz") ? { ok: true, json: async () => ({ Data: { LSJZList: [{ FSRQ: "2026-10-07", DWJZ: 1 }] } }) } : textResponse(sampleText([fixture.records[2]])) });
  assert.deepEqual(result.quotes, {});
});

test("expired shared budget starts no fallback or NAV request", async () => {
  let current = fixtureNow, calls = 0;
  const result = await refresh([sampleCatalog[0]], { now: () => current, budgetMs: 25, fetchImpl: async () => {
    calls++; current += 26; throw new Error("disconnect");
  } });
  assert.equal(calls, 1); assert.deepEqual(result.quotes, {});
  assert.ok(result.errors.some(e => e.reason === "ROUND_TIMEOUT"));
});


test("a source time regression triggers fallback without overwriting the prior record", async () => {
  const r = fixture.records[0], calls = [];
  const previous = { ...quote, updatedAt: fixture.now, quoteDate: fixture.as_of };
  const result = await refresh([sampleCatalog[0]], { now: () => fixtureNow,
    previousRecords: { [sampleCatalog[0].code]: previous }, fetchImpl: async url => {
      calls.push(url);
      if (url.includes("qt.gtimg")) return textResponse(sampleText([r]));
      return { ok: true, json: async () => ({ data: { diff: [raw] } }) };
    } });
  assert.deepEqual(result.quotes, {});
  assert.ok(calls[0].includes("/?q=")); assert.ok(calls[0].includes("&r="));
  assert.ok(calls.some(u => u.includes("eastmoney")));
  assert.ok(result.errors.some(e => e.reason === "OLDER_QUOTE"));
});


test("a single content retry recovers a lagging replica, without re-fetching successful products", async () => {
  const old = structuredClone(fixture.records[0]), good = structuredClone(old);
  old.fields[30] = "20261008093000"; good.fields[30] = "20261008120000";
  let calls = 0;
  const result = await refresh([sampleCatalog[0]], { now: () => fixtureNow,
    previousRecords: { "513500": { ...quote, updatedAt: "2026-10-08T03:00:00Z" } },
    fetchImpl: async url => { calls++; assert.ok(url.startsWith("https://qt.gtimg.cn/?q="));
      return textResponse(sampleText([calls === 1 ? old : good])); } });
  assert.equal(calls, 2); assert.equal(result.quotes["513500"].updatedAt, "2026-10-08T04:00:00.000Z");
});
