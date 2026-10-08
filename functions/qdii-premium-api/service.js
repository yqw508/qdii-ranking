"use strict";
const { randomUUID, createHash } = require("node:crypto");
const { refresh, seedQuote } = require("./upstream.js");
const { shanghaiDate } = require("./premium_quotes.js");

function validateSnapshot(snapshot) {
  if (snapshot.schema_version !== 1 || !Array.isArray(snapshot.catalog) || !snapshot.catalog.length ||
      !Array.isArray(snapshot.records)) throw new Error("INVALID_SNAPSHOT");
  const canonical = snapshot.catalog.map(e => [e.code, e.market_id, e.name, e.benchmark_group]);
  const hash = createHash("sha256").update(JSON.stringify(canonical)).digest("hex");
  if (hash !== snapshot.catalog_fingerprint || new Set(snapshot.catalog.map(e => e.code)).size !== snapshot.catalog.length ||
      snapshot.catalog.some(e => !/^\d{6}$/.test(e.code) || ![0, 1].includes(e.market_id))) throw new Error("INVALID_CATALOG");
  const codes = new Set(snapshot.catalog.map(e => e.code));
  if (snapshot.records.some(e => !codes.has(e.code)) || new Set(snapshot.records.map(e => e.code)).size !== snapshot.records.length)
    throw new Error("INVALID_SEED_RECORDS");
  return snapshot;
}

function createService({ snapshot, store, refreshImpl = refresh, now = Date.now, log = console.log }) {
  validateSnapshot(snapshot);
  const key = snapshot.catalog_fingerprint;
  let inflight;
  function initial() {
    const records = {}, asOf = shanghaiDate(new Date(now()));
    for (const row of snapshot.records) {
      try { records[row.code] = { ...seedQuote(row, asOf), checkedAt: null }; }
      catch (exc) { log(JSON.stringify({ stage: "seed", code: row.code, error: exc.message })); }
    }
    return { records, next_attempt: 0, fresh_codes: [], checked_at: null, errors: [] };
  }
  function response(state, requestId, cacheHit, busy = false) {
    const fresh = new Set(busy ? [] : state.fresh_codes);
    const records = Object.values(state.records).map(r => ({ ...r, status: fresh.has(r.code) ? "fresh" : "cached_stale" }));
    const freshCount = records.filter(r => r.status === "fresh").length;
    const status = !records.length ? "unavailable" : !freshCount ? "cached_stale" :
      freshCount === snapshot.catalog.length ? "fresh" : "partial";
    return { schema_version: 1, catalog_fingerprint: key, request_id: requestId,
      status, cache_hit: cacheHit, checked_at: state.checked_at, requested_at: new Date(now()).toISOString(),
      quote_delay_minutes: null, adapter_version: "multi-source-1", expected_count: snapshot.catalog.length, fresh_count: freshCount,
      retry_after_seconds: Math.max(1, Math.ceil(((busy ? state.lock_until : state.next_attempt) - now()) / 1000)),
      unavailable_products: busy ? [] : (state.unavailable_products || []),
      errors: busy ? [{ reason: "REFRESH_IN_PROGRESS", codes: [] }] : state.errors, records };
  }
  async function run(requestId) {
    const started = now();
    const claim = await store.claim(key, requestId, started, initial());
    if (!claim.acquired) return { state: claim.state, cacheHit: true, busy: claim.busy };
    let result;
    try { result = await refreshImpl(snapshot.catalog, { previousRecords: claim.state.records }); }
    catch (exc) { result = { quotes: {}, errors: [{ reason: exc.message, codes: [] }] }; }
    const records = { ...claim.state.records }, freshCodes = [];
    for (const [code, quote] of Object.entries(result.quotes)) {
      const previous = records[code];
      if (previous && (Date.parse(quote.updatedAt) < Date.parse(previous.updatedAt) || quote.quoteDate < previous.quoteDate ||
          (quote.referenceType === "nav" && previous.referenceType === "nav" && quote.referenceDate < previous.referenceDate))) {
        result.errors.push({ reason: "OLDER_QUOTE", codes: [code] });
        continue;
      }
      records[code] = quote; freshCodes.push(code);
    }
    const state = { records, fresh_codes: freshCodes, checked_at: new Date(now()).toISOString(),
      unavailable_products: result.unavailable || [],
      next_attempt: now() + (freshCodes.length + (result.unavailable || []).length === snapshot.catalog.length ? 60000 : 30000),
      errors: result.errors, lock_until: 0, lock_owner: null };
    const saved = await store.finish(key, requestId, state);
    log(JSON.stringify({ request_id: requestId, duration_ms: now() - started,
      fresh_count: freshCodes.length, expected_count: snapshot.catalog.length, errors: result.errors }));
    return { state: saved, cacheHit: false, busy: false };
  }
  return { snapshot, async get(fingerprint, requestId = randomUUID()) {
    if (fingerprint !== key) return { schema_version: 1, status: "unavailable", request_id: requestId,
      error: "CATALOG_MISMATCH", catalog_fingerprint: key, http_status: 409 };
    const shared = Boolean(inflight);
    if (!inflight) inflight = run(requestId).finally(() => { inflight = null; });
    const result = await inflight;
    const payload = response(result.state, requestId, result.cacheHit || shared, result.busy);
    log(JSON.stringify({ request_id: requestId, status: payload.status, cache_hit: payload.cache_hit,
      checked_at: payload.checked_at, fresh_count: payload.fresh_count }));
    return payload;
  } };
}
module.exports = { createService, validateSnapshot };
