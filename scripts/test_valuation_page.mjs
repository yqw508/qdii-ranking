import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const page = require("./valuation_page.js");

function history(count = 120) {
  return Array.from({ length: count }, (_, index) => ({
    month: `${2016 + Math.floor((index + 7) / 12)}-${String(((index + 7) % 12) + 1).padStart(2, "0")}`,
    proxy_pe_ttm: 18 + Math.sin(index / 8) * 2 + index / 100,
  }));
}

function assets() {
  return [
    { id: "nasdaq-100", name: "纳指 100", source_mode: "direct", status: "fresh" },
    { id: "sp-500-equal-weight", name: "标普 500 等权", source_mode: "proxy", status: "fresh" },
    { id: "gold-dual-anchor", name: "黄金估值", source_mode: "external_model", status: "fresh" },
  ];
}

test("buildChartModel creates stable geometry for 120 monthly points", () => {
  const model = page.buildChartModel(history(), { p30: 18.4, p50: 19.1, p70: 20.2 });
  assert.equal(model.points.length, 120);
  assert.equal(model.references.length, 3);
  assert.equal(model.width, 920);
  assert.equal(model.height, 390);
  assert.ok(model.points.every((point) => Number.isFinite(point.x) && Number.isFinite(point.y)));
  assert.ok(model.points[0].x < model.points.at(-1).x);
});

test("nearestIndex clamps pointer positions to the data window", () => {
  assert.equal(page.nearestIndex(-100, 0, 920, 120), 0);
  assert.equal(page.nearestIndex(1000, 0, 920, 120), 119);
  assert.ok(page.nearestIndex(460, 0, 920, 120) >= 58);
  assert.ok(page.nearestIndex(460, 0, 920, 120) <= 61);
});

test("invalid chart data is rejected", () => {
  assert.throws(() => page.buildChartModel([], { p30: 1, p50: 2, p70: 3 }), /at least two/);
  assert.throws(
    () => page.buildChartModel(history(2), { p30: 1, p50: Number.NaN, p70: 3 }),
    /p50 must be finite/,
  );
});

test("route distinguishes overview, valid detail, and invalid asset", () => {
  const payload = { default_asset_id: "sp-500-equal-weight", assets: assets() };
  assert.deepEqual(page.resolveRoute(payload, ""), {
    view: "overview", assetId: null, invalidAsset: false,
  });
  assert.deepEqual(page.resolveRoute(payload, "?v=2026-08-25&asset=gold-dual-anchor"), {
    view: "detail", assetId: "gold-dual-anchor", invalidAsset: false,
  });
  assert.deepEqual(page.resolveRoute(payload, "?asset=unknown"), {
    view: "overview", assetId: null, invalidAsset: true,
  });
});

test("view URLs preserve unrelated query parameters and fragments", () => {
  const current = "https://example.test/valuation/?v=2026-08-25&run=7&asset=old#model";
  assert.equal(
    page.buildViewUrl(current, "nasdaq-100"),
    "https://example.test/valuation/?v=2026-08-25&run=7&asset=nasdaq-100#model",
  );
  assert.equal(
    page.buildViewUrl(current, null),
    "https://example.test/valuation/?v=2026-08-25&run=7#model",
  );
  assert.equal(
    page.buildViewUrl("file:///D:/site/index.html?v=1", "gold-dual-anchor"),
    "file:///D:/site/index.html?v=1&asset=gold-dual-anchor",
  );
});

test("detail navigation groups assets and selects the current item", () => {
  const payload = { assets: assets() };
  const markup = page.renderDetailNavigation(
    assets()[1], payload, "https://example.test/valuation/?v=1&asset=sp-500-equal-weight",
  );
  assert.match(markup, /返回估值概览/);
  assert.match(markup, /<optgroup label="雪球直取">/);
  assert.match(markup, /<optgroup label="研究代理">/);
  assert.match(markup, /<optgroup label="黄金">/);
  assert.match(markup, /value="sp-500-equal-weight" selected/);
  assert.match(markup, /id="valuation-overview-link" href="https:\/\/example\.test\/valuation\/\?v=1"/);
});

test("overview filters preserve source-mode grouping", () => {
  assert.deepEqual(page.filterAssets(assets(), "proxy").map((asset) => asset.id), ["sp-500-equal-weight"]);
  assert.deepEqual(page.filterAssets(assets(), "all").map((asset) => asset.id), assets().map((asset) => asset.id));
  assert.equal(page.filterAssets(assets(), "direct")[0].id, "nasdaq-100");
});

test("chart is only eligible for an available proxy detail", () => {
  assert.equal(page.detailKind({ source_mode: "proxy", status: "fresh" }), "proxy");
  assert.equal(page.detailKind({ source_mode: "direct", status: "fresh" }), "direct");
  assert.equal(page.detailKind({ source_mode: "proxy", status: "unavailable" }), "unavailable");
});

test("detail document titles do not duplicate the valuation suffix", () => {
  assert.equal(
    page.detailDocumentTitle({ name: "标普 500 等权" }),
    "标普 500 等权估值详情 · 指数与黄金估值研究",
  );
  assert.equal(
    page.detailDocumentTitle({ name: "黄金估值" }),
    "黄金估值详情 · 指数与黄金估值研究",
  );
});
