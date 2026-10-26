"use strict";
const { randomUUID, createHash } = require("node:crypto");
const { refresh, seedQuote } = require("./upstream.js");
const { shanghaiDate } = require("./premium_quotes.js");

const STALE_BLOCK_CALENDAR_DAYS = 7;
const STALE_WARNING_TRADING_DAYS = 3;

function ageDays(value, now) {
  const timestamp = Date.parse(value || "");
  if (!Number.isFinite(timestamp)) return null;
  return Math.max(0, Math.floor((now - timestamp) / 86400000));
}

function tradingAgeDays(value, now) {
  const timestamp = Date.parse(value || "");
  if (!Number.isFinite(timestamp)) return null;
  const start = new Date(timestamp);
  const end = new Date(now - 86400000);
  let count = 0;
  for (let cursor = new Date(start.getTime() + 86400000); cursor <= end; cursor = new Date(cursor.getTime() + 86400000)) {
    const day = cursor.getUTCDay();
    if (day !== 0 && day !== 6) count++;
  }
  return count;
}

function summarizeErrors(errors = []) {
  const summary = {};
  for (const error of errors) {
    const key = `${error.source || "combined"}:${error.reason || "UNKNOWN"}`;
    summary[key] = (summary[key] || 0) + Math.max(1, Array.isArray(error.codes) ? error.codes.length : 1);
  }
  return summary;
}

function validRecord(record, code, now) {
  if (!record || record.code !== code || !Number.isFinite(record.marketPriceCny) || record.marketPriceCny <= 0 ||
      !Number.isFinite(record.referenceValueCny) || record.referenceValueCny <= 0 || !Number.isFinite(record.premiumPct) ||
      Math.abs(record.premiumPct - (record.marketPriceCny / record.referenceValueCny - 1) * 100) >
        Math.max(0.05, (0.0005 / record.referenceValueCny) * 100 + 0.01)) return false;
  const quoteDate = Date.parse(`${record.quoteDate || ""}T00:00:00Z`), updatedAt = Date.parse(record.updatedAt || "");
  if (!Number.isFinite(quoteDate) || !Number.isFinite(updatedAt) || quoteDate > now || updatedAt > now) return false;
  if (!(["tencent", "eastmoney"].includes(record.quoteSource) && record.adapterVersion === `${record.quoteSource}-1`)) return false;
  try {
    const url = new URL(record.quoteSourceUrl);
    if (url.protocol !== "https:" || url.hostname !== (record.quoteSource === "tencent" ? "gu.qq.com" : "quote.eastmoney.com")) return false;
  } catch (_) { return false; }
  return true;
}

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
    return { records, next_attempt: 0, fresh_codes: [], unavailable_products: [], invalid_codes: [], checked_at: null, errors: [],
      catalog_fingerprint: key, source_failure_streak: 0, last_success_at: null,
      last_checked_at: null, coverage: {}, stale_age: {}, diagnostics: {} };
  }
  function usableState(state) {
    return state && state.catalog_fingerprint === key && state.records && typeof state.records === "object" &&
      Array.isArray(state.fresh_codes) && state.fresh_codes.every(code => snapshot.catalog.some(item => item.code === code)) &&
      Array.isArray(state.unavailable_products || []) &&
      Array.isArray(state.invalid_codes || []) &&
      Object.keys(state.records).every(code => snapshot.catalog.some(item => item.code === code) && validRecord(state.records[code], code, now()));
  }
  function response(state, requestId, cacheHit, busy = false) {
    if (!usableState(state)) state = { ...initial(), lock_until: state?.lock_until || 0,
      next_attempt: state?.next_attempt || 0, errors: [{ reason: "INVALID_PERSISTED_STATE", codes: [] }] };
    const fresh = new Set(busy ? [] : state.fresh_codes);
    const records = Object.values(state.records).map(r => {
      const status = fresh.has(r.code) ? "fresh" : "cached_stale";
      const calendarDays = status === "fresh" ? 0 : ageDays(r.quoteDate, now());
      return { ...r, status, stale_age: { calendar_days: calendarDays ?? 0,
        trading_days: status === "fresh" ? 0 : tradingAgeDays(r.quoteDate, now()) ?? 0 },
        last_success_at: r.last_success_at || r.checkedAt || r.updatedAt || null,
        last_checked_at: r.last_checked_at || r.checkedAt || null,
        source_failure_streak: r.source_failure_streak || state.source_failure_streak || 0 };
    });
    const freshCount = records.filter(r => r.status === "fresh").length;
    const staleRecords = records.filter(r => r.status === "cached_stale");
    const nowValue = now();
    const staleAges = staleRecords.map(r => ageDays(r.quoteDate, nowValue)).filter(v => v != null);
    const staleTradingAges = staleRecords.map(r => tradingAgeDays(r.quoteDate, nowValue)).filter(v => v != null);
    const maxStaleAge = Math.max(0, ...staleAges);
    const maxStaleTradingAge = Math.max(0, ...staleTradingAges);
    const unavailable = busy ? [] : (state.unavailable_products || []);
    const invalidCodes = busy ? [] : (state.invalid_codes || []);
    const staleExpired = staleRecords.some(r => {
      const age = ageDays(r.quoteDate, nowValue);
      return age == null || age > STALE_BLOCK_CALENDAR_DAYS;
    });
    const exceptionCodes = new Set(unavailable.map(r => r.code));
    const exceptionOverlap = records.some(r => exceptionCodes.has(r.code));
    const covered = new Set([
      ...records.filter(r => { const age = ageDays(r.quoteDate, nowValue); return age != null && age <= STALE_BLOCK_CALENDAR_DAYS; }).map(r => r.code),
      ...unavailable.filter(r => !records.some(record => record.code === r.code)).map(r => r.code),
    ]);
    const unaccountedCount = Math.max(0, snapshot.catalog.length - covered.size);
    const publicationDecision = unaccountedCount || staleExpired || exceptionOverlap || invalidCodes.length || !freshCount ? "block" :
      staleRecords.length || unavailable.length ? "allow_with_stale" : "allow_fresh";
    const status = !records.length ? "unavailable" : !freshCount ? "cached_stale" :
      freshCount === snapshot.catalog.length ? "fresh" : "partial";
    const payload = { schema_version: 1, catalog_fingerprint: key, request_id: requestId,
      status, cache_hit: cacheHit, checked_at: state.checked_at, requested_at: new Date(now()).toISOString(),
      quote_delay_minutes: null, adapter_version: "multi-source-1", expected_count: snapshot.catalog.length, fresh_count: freshCount,
      retry_after_seconds: Math.max(1, Math.ceil(((busy ? state.lock_until : state.next_attempt) - now()) / 1000)),
      unavailable_products: unavailable,
      errors: busy ? [{ reason: "REFRESH_IN_PROGRESS", codes: [] }] : state.errors, records };
    const diagnostics = { ...(state.diagnostics || {}), error_summary: summarizeErrors(state.errors) };
    return { ...payload, catalog_count: snapshot.catalog.length, stale_count: staleRecords.length,
      unavailable_count: unavailable.length, invalid_count: invalidCodes.length, invalid_codes: invalidCodes, unaccounted_count: unaccountedCount,
      coverage: state.coverage || {}, stale_age: state.stale_age || {},
      max_stale_age: { calendar_days: maxStaleAge, trading_days: maxStaleTradingAge },
      stale_warning: maxStaleTradingAge >= STALE_WARNING_TRADING_DAYS,
      source_failure_streak: state.source_failure_streak || 0,
      last_success_at: state.last_success_at || null, last_checked_at: state.last_checked_at || null,
      diagnostics, batch_retry_count: diagnostics.batch_retry_count || 0,
      tencent_invalid_iopv_count: diagnostics.tencent_invalid_iopv_count || 0,
      eastmoney_fallback_count: diagnostics.eastmoney_fallback_count || 0,
      systemic_source_degradation: Boolean(diagnostics.systemic_source_degradation),
      publication_decision: publicationDecision,
      publication_block_reason: invalidCodes.length ? "invalid_record" : exceptionOverlap ? "unavailable_overlap" : unaccountedCount ? "unaccounted_products" : staleExpired ? "stale_cache_expired" : !freshCount ? "no_fresh_evidence" : null };
  }
  async function run(requestId) {
    const started = now();
    const claim = await store.claim(key, requestId, started, initial());
    if (!claim.acquired) return { state: claim.state, cacheHit: true, busy: claim.busy };
    const persisted = usableState(claim.state) ? claim.state : initial();
    let result;
    try { result = await refreshImpl(snapshot.catalog, { previousRecords: persisted.records }); }
    catch (exc) { result = { quotes: {}, errors: [{ reason: exc.message, codes: [] }] }; }
    result = { quotes: result.quotes || {}, errors: result.errors || [], unavailable: result.unavailable || [], diagnostics: result.diagnostics || {} };
    const records = { ...persisted.records }, freshCodes = [], invalidCodes = [];
    const catalogCodes = new Set(snapshot.catalog.map(item => item.code));
    for (const [code, quote] of Object.entries(result.quotes)) {
      if (!catalogCodes.has(code) || !quote || quote.code !== code) {
        result.errors.push({ reason: "INVALID_QUOTE_CODE", codes: [code] });
        invalidCodes.push(code);
        continue;
      }
      const previous = records[code];
      if (previous && (Date.parse(quote.updatedAt) < Date.parse(previous.updatedAt) || quote.quoteDate < previous.quoteDate ||
          (quote.referenceType === "nav" && previous.referenceType === "nav" && quote.referenceDate < previous.referenceDate))) {
        result.errors.push({ reason: "OLDER_QUOTE", codes: [code] });
        continue;
      }
      records[code] = { ...quote, last_success_at: quote.last_success_at || quote.checkedAt || new Date(now()).toISOString(),
        last_checked_at: new Date(now()).toISOString(), source_failure_streak: 0 };
      freshCodes.push(code);
    }
    const reasons = { U: "NOT_LISTED", D: "DELISTED", S: "SUSPENDED", Z: "LISTING_SUSPENDED" };
    const validUnavailable = [];
    for (const item of result.unavailable) {
      const observed = Date.parse(item.observed_at || ""), sourceUpdated = Date.parse(item.source_updated_at || "");
      if (catalogCodes.has(item.code) && item.source === "tencent" && reasons[item.source_status] === item.reason &&
          Number.isFinite(observed) && Number.isFinite(sourceUpdated) && observed <= now() && sourceUpdated <= observed &&
          /^https:\/\/gu\.qq\.com\//.test(item.source_url || "")) validUnavailable.push(item);
      else { result.errors.push({ reason: "INVALID_UNAVAILABLE_STATUS", codes: [item.code] }); invalidCodes.push(item.code); }
    }
    result.unavailable = validUnavailable;
    const unavailableCodes = new Set(validUnavailable.map(item => item.code));
    for (const code of unavailableCodes) delete records[code];
    const checkedAt = new Date(now()).toISOString();
    const hasErrors = Boolean(result.errors?.length);
    const coveredCodes = new Set([...Object.keys(records), ...unavailableCodes]);
    const staleAge = Object.fromEntries(Object.keys(records).filter(code => !freshCodes.includes(code))
      .map(code => [code, { calendar_days: ageDays(records[code].quoteDate, now()) ?? null,
        trading_days: tradingAgeDays(records[code].quoteDate, now()) ?? null }]));
    const state = { records, fresh_codes: freshCodes, checked_at: checkedAt,
      unavailable_products: result.unavailable || [],
      next_attempt: now() + (freshCodes.length + (result.unavailable || []).length === snapshot.catalog.length ? 60000 : 30000),
      errors: result.errors, invalid_codes: [...new Set(invalidCodes)], lock_until: 0, lock_owner: null, catalog_fingerprint: key,
      source_failure_streak: hasErrors ? (persisted.source_failure_streak || 0) + 1 : 0,
      last_success_at: freshCodes.length ? checkedAt : (persisted.last_success_at || null),
      last_checked_at: checkedAt, coverage: {
        fresh: freshCodes, cached_stale: Object.keys(records).filter(code => !freshCodes.includes(code)),
        unavailable: [...unavailableCodes], unaccounted: snapshot.catalog.map(item => item.code).filter(code => !coveredCodes.has(code)),
      }, stale_age: staleAge, diagnostics: result.diagnostics || {} };
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
