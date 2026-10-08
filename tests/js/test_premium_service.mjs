import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import http from "node:http";
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
  const result = await refresh([entry], { now: () => time, fetchImpl: async url => {
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
