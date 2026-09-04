import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("./premium_refresh.js", import.meta.url), "utf8");

function loadApi(extra = {}) {
  const context = {
    AbortController,
    Date,
    Error,
    Intl,
    Map,
    Math,
    Number,
    Object,
    Promise,
    Set,
    String,
    clearTimeout,
    setTimeout,
    ...extra,
  };
  context.globalThis = context;
  vm.runInNewContext(source, context, { filename: "premium_refresh.js" });
  return { api: context.QdiiPremiumRefresh, context };
}

const entry = { code: "513500", name: "标普500ETF博时", benchmarkGroup: "标普500" };
const rawQuote = {
  f2: 2.672,
  f3: -0.11,
  f6: 228746612,
  f12: "513500",
  f14: "标普500ETF博时",
  f124: 1787299916,
  f297: 20260821,
  f402: -9.09,
  f441: 2.4493,
};

const lofEntry = {
  code: "161125",
  name: "标普500LOF",
  benchmarkGroup: "QDII-指数",
  category: "qdii",
  referenceType: "nav",
  referenceValueCny: 3.1496,
  referenceDate: "2026-08-20",
};
const rawLofQuote = {
  ...rawQuote,
  f2: 3.252,
  f12: "161125",
  f14: "标普500LOF",
  f402: -3.25,
  f441: "-",
};

test("normalizes the discount field into a checked premium", () => {
  const { api } = loadApi();
  const quote = api.normalizeQuote(rawQuote, entry, "2026-08-21");
  assert.equal(quote.premiumPct, 9.09);
  assert.equal(quote.marketPriceCny, 2.672);
  assert.equal(api.premiumBand(quote.premiumPct).key, "high");
});

test("rejects future, non-finite, and inconsistent quote values", () => {
  const { api } = loadApi();
  assert.throws(
    () => api.normalizeQuote(rawQuote, entry, "2026-08-20"),
    /future data/,
  );
  assert.throws(
    () => api.normalizeQuote({ ...rawQuote, f2: "--" }, entry, "2026-08-21"),
    /non-finite/,
  );
  assert.throws(
    () => api.normalizeQuote({ ...rawQuote, f402: -1 }, entry, "2026-08-21"),
    /differs from price\/IOPV/,
  );
});

test("keeps only unique requested records in partial responses", () => {
  const { api } = loadApi();
  const second = { code: "159655", name: "标普500ETF华夏", benchmarkGroup: "标普500" };
  const result = api.normalizeResponse(
    { data: { diff: [rawQuote, rawQuote, { ...rawQuote, f12: "999999" }] } },
    [entry, second],
    "2026-08-21",
  );
  assert.equal(result.valid.size, 0);
  assert.equal(result.errors.length, 2);
});

test("uses the latest published NAV when a listed LOF has no IOPV", () => {
  const { api } = loadApi();
  const quote = api.normalizeQuote(rawLofQuote, lofEntry, "2026-08-21");
  assert.equal(quote.referenceType, "nav");
  assert.equal(quote.referenceValueCny, 3.1496);
  assert.equal(quote.referenceDate, "2026-08-20");
  assert.equal(quote.premiumPct, 3.25);
  assert.throws(
    () => api.normalizeQuote(rawLofQuote, { ...lofEntry, referenceDate: "2026-08-22" }, "2026-08-21"),
    /NAV reference date/,
  );
});

test("fetches every page for the dynamic QDII market refresh", async () => {
  const { api } = loadApi();
  const pages = [];
  const payload = await api.fetchPagedQuotes(
    {
      refreshMode: "paged",
      refreshPageSize: 2,
      refreshUrl: "https://example.test/quotes?pn=1",
    },
    async (url) => {
      pages.push(url);
      const page = url.includes("pn=2") ? 2 : 1;
      return {
        ok: true,
        json: async () => ({
          data: {
            total: 3,
            diff: page === 1 ? [{ f12: "159100" }, { f12: "160125" }] : [{ f12: "159202" }],
          },
        }),
      };
    },
  );
  assert.deepEqual(pages, [
    "https://example.test/quotes?pn=1",
    "https://example.test/quotes?pn=2",
  ]);
  assert.equal(payload.data.diff.length, 3);
});

test("rejects an incomplete dynamic market refresh", async () => {
  const { api } = loadApi();
  await assert.rejects(
    api.fetchPagedQuotes(
      {
        refreshMode: "paged",
        refreshPageSize: 2,
        refreshUrl: "https://example.test/quotes?pn=1",
      },
      async () => ({
        ok: true,
        json: async () => ({ data: { total: 3, diff: [{ f12: "159100" }] } }),
      }),
    ),
    /分页记录不完整/,
  );
});

test("sorts all listed QDII products by premium descending with missing values last", () => {
  const { api } = loadApi();
  const item = (code, premium) => ({
    dataset: { etfCode: code, premium: premium == null ? "NaN" : String(premium) },
  });
  const items = [item("000003", null), item("000002", 2), item("000001", 8)];
  items.sort(api.comparePremiumItems);
  assert.deepEqual(items.map((value) => value.dataset.etfCode), ["000001", "000002", "000003"]);
});

test("refreshes quote fields without changing the rendered holding cost", () => {
  const { api } = loadApi();
  const fields = new Map([
    ["name", { textContent: "旧名称" }],
    ["price", { textContent: "1.000" }],
    ["reference-label", { textContent: "IOPV" }],
    ["reference-value", { textContent: "1.0000" }],
    ["premium", { textContent: "+0.00%", className: "premium-value band-normal" }],
    ["change", { textContent: "+0.00%" }],
    ["turnover", { textContent: "1万元" }],
    ["updated", { textContent: "2026-08-20 15:00" }],
    ["band", { textContent: "0–2%", className: "premium-band band-normal" }],
    ["stale", { textContent: "旧值" }],
    ["holding-cost", { textContent: "0.80%/年" }],
  ]);
  const item = {
    dataset: {},
    classList: { remove() {} },
    querySelector(selector) {
      const match = selector.match(/data-field="([^"]+)"/);
      return match ? fields.get(match[1]) || null : null;
    },
  };
  api.updateRow(item, api.normalizeQuote(rawQuote, entry, "2026-08-21"));
  assert.equal(fields.get("holding-cost").textContent, "0.80%/年");
  assert.equal(fields.get("price").textContent, "2.672");
  assert.equal(fields.get("reference-label").textContent, "IOPV");
  assert.equal(fields.get("reference-value").textContent, "2.4493");
});

test("toggles a listed-QDII detail row without opening other rows", () => {
  const { api } = loadApi();
  let handler;
  const detail = { hidden: true };
  const attributes = new Map([
    ["aria-controls", "premium-detail-513500"],
    ["aria-expanded", "false"],
  ]);
  const button = {
    addEventListener(_event, callback) { handler = callback; },
    getAttribute(name) { return attributes.get(name); },
    setAttribute(name, value) { attributes.set(name, value); },
  };
  const panel = {
    querySelectorAll() { return [button]; },
    querySelector() { return detail; },
  };
  api.setupDetailToggles(panel);
  handler();
  assert.equal(detail.hidden, false);
  assert.equal(attributes.get("aria-expanded"), "true");
  handler();
  assert.equal(detail.hidden, true);
  assert.equal(attributes.get("aria-expanded"), "false");
});

test("retries a failed request once", async () => {
  const { api } = loadApi();
  let calls = 0;
  const payload = { data: { diff: [rawQuote] } };
  const result = await api.fetchWithRetry(
    "https://example.test/quotes",
    async () => {
      calls += 1;
      if (calls === 1) throw new Error("temporary");
      return { ok: true, json: async () => payload };
    },
    { timeoutMs: 100, retryDelayMs: 0 },
  );
  assert.equal(calls, 2);
  assert.deepEqual(result, payload);
});

test("ignores a repeated refresh while the first request is running", async () => {
  let handler;
  const button = {
    disabled: false,
    addEventListener(_event, callback) { handler = callback; },
    setAttribute() {},
  };
  const status = { textContent: "" };
  const panel = {
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  const document = {
    getElementById(id) {
      return { "premium-refresh": button, "premium-refresh-status": status, "panel-premium": panel }[id];
    },
  };
  let calls = 0;
  let release;
  const responsePromise = new Promise((resolve) => { release = resolve; });
  const { api, context } = loadApi({ document });
  context.fetch = async () => {
    calls += 1;
    await responsePromise;
    return { ok: true, json: async () => ({ data: { diff: [rawQuote] } }) };
  };
  api.boot({ refreshUrl: "https://example.test", entries: [entry] });
  const first = handler();
  const second = handler();
  assert.equal(calls, 1);
  release();
  await Promise.all([first, second]);
  assert.equal(calls, 1);
  assert.match(status.textContent, /更新1\/1只/);
});
