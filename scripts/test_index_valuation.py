import json
import io
import math
import threading
import time
import unittest
import urllib.parse
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import update_index_valuation as valuation
import validate_index_valuation as validator


AS_OF = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 7, 7, tzinfo=valuation.SHANGHAI_TZ)


def fixture_months():
    return [valuation.add_months("2026-07", offset) for offset in range(-119, 1)]


def fixture_series():
    prices = {ticker: [] for ticker in valuation.NASDAQ_TICKERS}
    pe_rows = []
    for index, month in enumerate(fixture_months()):
        parsed = valuation.month_start(month).replace(day=15)
        spy = 100.0 + index * 0.8
        ratios = {
            "RSP": 0.28 + 0.00025 * index + 0.008 * math.sin(index / 7),
            "EQWL": 0.46 + 0.00018 * index + 0.006 * math.sin(index / 8),
            "EWU": 0.31 + 0.00012 * index + 0.005 * math.sin(index / 11),
            "SPY": 1.0,
        }
        for ticker, ratio in ratios.items():
            prices[ticker].append(
                {"date": parsed.strftime("%m/%d/%Y"), "close": f"${spy * ratio:.6f}"}
            )
        pe_rows.append((month, 21.0 + 2.3 * math.sin(index / 9) + 0.012 * index))
    return prices, pe_rows


def ndxtmc_fixture_points():
    points = []
    for index, month in enumerate(fixture_months()):
        value = 900.0 + index * 7.0 + 35.0 * math.sin(index / 8)
        points.append({"date": f"{month}-15", "close": value})
    points.extend([
        {"date": "2022-03-18", "close": 1000.0},
        {"date": "2022-03-21", "close": 1005.0},
    ])
    return sorted(points, key=lambda item: item["date"])


def ndxtmc_workbook(points=None):
    points = points or [item for item in ndxtmc_fixture_points() if item["date"] <= "2022-03-18"]
    epoch = date(1899, 12, 30)
    rows = [
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
    ]
    for index, item in enumerate(points, 2):
        serial = (date.fromisoformat(item["date"]) - epoch).days
        rows.append(
            f'<row r="{index}"><c r="A{index}"><v>{serial}</v></c>'
            f'<c r="B{index}"><v>{item["close"]}</v></c></row>'
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(rows)}</sheetData></worksheet>'
    )
    shared = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<si><t>Date</t></si><si><t>NDXTMC</t></si></sst>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/sharedStrings.xml", shared)
    return output.getvalue()


def ndxtmc_history_body(points=None):
    points = points or [item for item in ndxtmc_fixture_points() if item["date"] >= "2022-03-21"]
    return json.dumps([
        {
            "x": int(datetime.combine(date.fromisoformat(item["date"]), datetime.min.time(), timezone.utc).timestamp() * 1000),
            "y": item["close"],
            "FPSymbol": "NDXTMC",
        }
        for item in points
    ]).encode()


def nasdaq_body(rows):
    return json.dumps(
        {"data": {"tradesTable": {"rows": list(reversed(rows))}}},
        separators=(",", ":"),
    ).encode()


def dqydj_body(rows):
    table = "".join(
        f"<tr><td>{month[5:7]}-{month[:4]}</td><td>5000</td><td>$200</td><td>{pe:.8f}</td></tr>"
        for month, pe in reversed(rows)
    )
    return f"<html><body><table><tbody>{table}</tbody></table></body></html>".encode()


def snowball_body(missing=None):
    timestamp = int(datetime(2026, 8, 24, tzinfo=timezone.utc).timestamp() * 1000)
    begin = int(datetime(2016, 5, 12, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [
        {
            "index_code": code,
            "name": name,
            "pe": pe,
            "pe_percentile": percentile,
            "pb": pb,
            "pb_percentile": pb_percentile,
            "roe": roe,
            "yeild": dividend,
            "ts": timestamp,
            "begin_at": begin,
            "eva_type": "high",
        }
        for code, name, pe, percentile, pb, pb_percentile, roe, dividend in (
            ("NDX", "纳指100", 30.58, 0.492, 9.17, 0.788, 0.30, 0.0044),
            ("SP500", "标普500", 25.57, 0.6088, 5.54, 0.9552, 0.216, 0.0102),
            ("GDAXI", "德国DAX", 17.56, 0.492, 1.77, 0.6192, 0.1008, 0.001),
            ("HSI", "恒生指数", 12.0, 0.2, 1.2, 0.2, 0.1, 0.02),
        )
        if code != missing
    ]
    return json.dumps({"data": {"items": rows}, "result_code": 0}).encode()


def gold_body():
    return """
    <html><body>
      <p>因子：TIPS + 中美利差 + 金油比 · 数据始于 2016-04-18 · 更新于 2026-08-25</p>
      <h3>最新金价</h3><div>4682.34 美元</div>
      <p>TIPS: 2.40% | 中美利差: -3.02%</p><p>金油比: 55.0</p>
      <table><tr><th>尺度</th><th>分位</th><th>状态</th></tr>
      <tr><td>近1年</td><td>67.9%</td><td>合理</td></tr>
      <tr><td>近3年</td><td>89.3%</td><td>高估</td></tr>
      <tr><td>近5年</td><td>93.3%</td><td>极度高估</td></tr>
      <tr><td>近10年</td><td>96.6%</td><td>极度高估</td></tr>
      <tr><td>全部历史</td><td>96.8%</td><td>极度高估</td></tr></table>
      <p>当前残差为0.1835，处于来源模型区间。</p>
      <table><tr><th>因子</th><th>最新数据日期</th><th>状态</th></tr>
      <tr><td>TIPS 实际利率</td><td>2026-08-21</td><td>滞后 4 天</td></tr>
      <tr><td>中国10年期国债</td><td>2026-08-24</td><td>实时</td></tr>
      <tr><td>美国10年期国债</td><td>2026-08-24</td><td>实时</td></tr>
      <tr><td>金油比</td><td>2026-08-25</td><td>实时</td></tr></table>
      <h3>历史回测</h3><p>这段策略说明不得进入规范化结果。</p>
    </body></html>
    """.encode()


def load_catalog():
    return valuation.load_catalog(valuation.DEFAULT_CATALOG)


class FakeClient:
    def __init__(self, fail=None, delay=0.0, tail_prices=False, conditional_304=False):
        prices, pe = fixture_series()
        self.full = {
            **{valuation.source_cache_id(ticker): nasdaq_body(rows) for ticker, rows in prices.items()},
            "dqydj": dqydj_body(pe),
            "snowball": snowball_body(),
            "gold": gold_body(),
            "ndxtmc-workbook": ndxtmc_workbook(),
        }
        self.tail = {
            valuation.source_cache_id(ticker): nasdaq_body(rows[-4:])
            for ticker, rows in prices.items()
        }
        self.fail = set(fail or [])
        self.delay = delay
        self.tail_prices = tail_prices
        self.conditional_304 = conditional_304
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    @staticmethod
    def source_for(url):
        if "AdditionalData_NDXTMC.xlsx" in url:
            return "ndxtmc-workbook"
        if "HistoryChartData" in url:
            return "ndxtmc-history"
        for ticker in valuation.NASDAQ_TICKERS:
            if f"/{ticker}/" in url:
                return valuation.source_cache_id(ticker)
        if "dqydj.com" in url:
            return "dqydj"
        if "index_eva" in url:
            return "snowball"
        if "gold_cn" in url:
            return "gold"
        raise AssertionError(f"unknown fixture URL {url}")

    def fetch(self, url, headers):
        source = self.source_for(url)
        with self.lock:
            self.calls.append((source, url, dict(headers)))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if source in self.fail or (
                source.startswith("ndxtmc-") and valuation.NDXTMC_SOURCE_ID in self.fail
            ):
                raise valuation.ValuationError(f"fixture {source} outage")
            if self.conditional_304 and source in {"snowball", "gold", "ndxtmc-workbook"} and (
                "If-None-Match" in headers or "If-Modified-Since" in headers
            ):
                return valuation.FetchResponse(b"", self.delay, 1, 304, url, {"etag": '"fixture"'})
            body = self.tail[source] if self.tail_prices and source in self.tail else self.full[source]
            return valuation.FetchResponse(
                body, self.delay, 1, 200, url,
                {"etag": '"fixture"', "last-modified": "Tue, 25 Aug 2026 00:00:00 GMT"},
            )
        finally:
            with self.lock:
                self.active -= 1

    def post_form(self, url, fields, headers):
        del headers
        source = self.source_for(url)
        with self.lock:
            self.calls.append((source, url, dict(fields)))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if valuation.NDXTMC_SOURCE_ID in self.fail:
                raise valuation.ValuationError("fixture nasdaq-ndxtmc outage")
            start = fields["startDate"][:10]
            end = fields["endDate"][:10]
            points = [
                item for item in ndxtmc_fixture_points()
                if start <= item["date"] <= end and item["date"] >= "2022-03-21"
            ]
            return valuation.FetchResponse(
                ndxtmc_history_body(points), self.delay, 1, 200, url, {}
            )
        finally:
            with self.lock:
                self.active -= 1


def make_cache_bundle():
    catalog, catalog_hash = load_catalog()
    prices, pe = fixture_series()
    data = {
        **{
            valuation.source_cache_id(ticker): valuation.parse_nasdaq_history(
                nasdaq_body(rows), ticker
            )
            for ticker, rows in prices.items()
        },
        "dqydj": valuation.parse_dqydj_pe(dqydj_body(pe)),
        "snowball": valuation.parse_snowball_snapshot(
            snowball_body(), {"NDX", "SP500", "GDAXI"}
        ),
        "gold": valuation.parse_gold_snapshot(gold_body(), AS_OF),
        valuation.NDXTMC_SOURCE_ID: valuation.merge_ndxtmc_points(
            valuation.parse_ndxtmc_workbook(ndxtmc_workbook()),
            valuation.parse_ndxtmc_history(ndxtmc_history_body()),
        ),
    }
    return {
        source_id: {
            "schema_version": valuation.CACHE_SCHEMA_VERSION,
            "fingerprint": valuation.source_fingerprint(catalog_hash, source_id),
            "source_id": source_id,
            "last_success_at": "2026-08-24T07:07:00+08:00",
            "data_updated_at": "2026-08-24T07:07:00+08:00",
            "etag": '"fixture"',
            "last_modified": "Mon, 24 Aug 2026 00:00:00 GMT",
            "data": source_data,
        }
        for source_id, source_data in data.items()
    }


def make_manifest(month="2026-08"):
    catalog, catalog_hash = load_catalog()
    return {
        "schema_version": valuation.CACHE_SCHEMA_VERSION,
        "fingerprint": valuation.cache_fingerprint(catalog, catalog_hash),
        "catalog_sha256": catalog_hash,
        "updated_at": "2026-08-24T07:07:00+08:00",
        "last_full_refresh_month": month,
    }


def run_build(caches=None, client=None, manifest=None, cache_states=None):
    catalog, catalog_hash = load_catalog()
    caches = caches or {}
    return valuation.build_payload(
        as_of=AS_OF,
        now=NOW,
        catalog=catalog,
        catalog_hash=catalog_hash,
        caches=caches,
        cache_states=cache_states or {
            source_id: ("valid" if source_id in caches else "missing")
            for source_id in valuation.all_source_ids()
        },
        manifest=manifest,
        manifest_state="valid" if manifest else "missing",
        client=client or FakeClient(),
    )


class SourceParsingTests(unittest.TestCase):
    def test_parses_all_source_shapes_and_allowlist(self):
        prices, pe = fixture_series()
        self.assertEqual(120, len(valuation.parse_nasdaq_history(nasdaq_body(prices["RSP"]), "RSP")))
        self.assertEqual(120, len(valuation.parse_dqydj_pe(dqydj_body(pe))))
        direct = valuation.parse_snowball_snapshot(snowball_body(), {"NDX", "SP500", "GDAXI"})
        self.assertEqual({"NDX", "SP500", "GDAXI"}, {item["code"] for item in direct})
        self.assertEqual("偏高", direct[0]["source_rating"]["label"])
        gold = valuation.parse_gold_snapshot(gold_body(), AS_OF)
        self.assertEqual(96.6, gold["percentiles"]["10y"])
        self.assertEqual(4, gold["factors"]["tips_real_yield"]["lag_days"])
        self.assertNotIn("回测", json.dumps(gold, ensure_ascii=False))

    def test_parses_and_merges_ndxtmc_official_sources(self):
        static = valuation.parse_ndxtmc_workbook(ndxtmc_workbook())
        live = valuation.parse_ndxtmc_history(ndxtmc_history_body())
        valuation.validate_ndxtmc_boundary(static, live)
        merged = valuation.merge_ndxtmc_points(static, live, [live[0]])
        self.assertEqual("2022-03-18", static[-1]["date"])
        self.assertEqual("2022-03-21", live[0]["date"])
        self.assertEqual(len(static) + len(live), len(merged))

    def test_ndxtmc_merge_rejects_conflicting_duplicates(self):
        with self.assertRaisesRegex(valuation.ValuationError, "conflicting values"):
            valuation.merge_ndxtmc_points(
                [{"date": "2022-03-21", "close": 1005.0}],
                [{"date": "2022-03-21", "close": 1006.0}],
            )

    def test_ndxtmc_boundary_rejects_non_trading_gap(self):
        with self.assertRaisesRegex(valuation.ValuationError, "boundary"):
            valuation.validate_ndxtmc_boundary(
                [{"date": "2022-03-18", "close": 1000.0}],
                [{"date": "2022-03-25", "close": 1005.0}],
            )

    def test_snowball_missing_expected_item_is_rejected(self):
        with self.assertRaisesRegex(valuation.ValuationError, "缺少白名单"):
            valuation.parse_snowball_snapshot(
                snowball_body(missing="NDX"), {"NDX", "SP500", "GDAXI"}
            )

    def test_gold_requires_complete_factor_freshness(self):
        with self.assertRaisesRegex(valuation.ValuationError, "美国10年期国债"):
            valuation.parse_gold_snapshot(
                gold_body().replace("美国10年期国债".encode(), b"missing factor"), AS_OF
            )


class ModelAndRefreshTests(unittest.TestCase):
    def test_cold_start_runs_eight_sources_concurrently(self):
        client = FakeClient(delay=0.04)
        payload, _caches, _manifest, metrics = run_build(client=client)
        logical_sources = {
            valuation.NDXTMC_SOURCE_ID if item[0].startswith("ndxtmc-") else item[0]
            for item in client.calls
        }
        self.assertEqual(set(valuation.all_source_ids()), logical_sources)
        self.assertEqual(8, len(payload["sources"]))
        self.assertGreaterEqual(client.max_active, 7)
        self.assertEqual("cold", payload["cache"]["startup"])
        self.assertEqual("full", payload["cache"]["refresh_mode"])
        self.assertLess(metrics["request_wall_seconds"], 0.30)

    def test_builds_all_assets_and_reproduces_each_anchor(self):
        payload, _caches, _manifest, _metrics = run_build()
        self.assertEqual("fresh", payload["status"])
        self.assertEqual(list(valuation.EXPECTED_ASSET_IDS), [item["id"] for item in payload["assets"]])
        proxies = [item for item in payload["assets"] if item["source_mode"] == "proxy"]
        self.assertEqual(4, len(proxies))
        for asset in proxies:
            self.assertEqual(120, len(asset["history"]))
            if asset["id"] == valuation.RELATIVE_PROXY_ASSET_ID:
                values = [item["relative_score"] for item in asset["history"]]
                self.assertAlmostEqual(
                    valuation.percentile_midrank(values, values[-1]),
                    asset["current"]["relative_percentile_10y"],
                    places=2,
                )
                self.assertNotIn("proxy_pe_ttm", asset["current"])
                self.assertNotIn("anchor", asset["method"])
                continue
            anchor = asset["method"]["anchor"]
            self.assertAlmostEqual(anchor["pe_ttm"], anchor["reproduced_pe_ttm"], places=5)
            values = [item["proxy_pe_ttm"] for item in asset["history"]]
            self.assertAlmostEqual(
                valuation.percentile_midrank(values, values[-1]),
                asset["current"]["proxy_percentile_10y"],
                places=2,
            )
            self.assertNotIn("source_rating", asset["current"])
        self.assertTrue(next(item for item in proxies if item["id"] == "ftse-100-proxy")["method"]["experimental"])

    def test_relative_proxy_percentile_is_scale_invariant(self):
        payload, _caches, _manifest, _metrics = run_build()
        asset = next(item for item in payload["assets"] if item["id"] == valuation.RELATIVE_PROXY_ASSET_ID)
        values = [item["relative_score"] for item in asset["history"]]
        scaled = [value * 17.25 for value in values]
        self.assertEqual(
            valuation.percentile_midrank(values, values[-1]),
            valuation.percentile_midrank(scaled, scaled[-1]),
        )

    def test_hot_start_uses_five_price_tails_and_three_conditional_requests(self):
        caches = make_cache_bundle()
        client = FakeClient(tail_prices=True, conditional_304=True)
        payload, _new, _manifest, metrics = run_build(
            caches=caches, manifest=make_manifest(), client=client
        )
        self.assertEqual("hot", payload["cache"]["startup"])
        self.assertEqual("tail", payload["cache"]["refresh_mode"])
        self.assertEqual(304, metrics["sources"]["snowball"]["http_status"])
        self.assertEqual(304, metrics["sources"]["gold"]["http_status"])
        self.assertEqual("tail", metrics["sources"][valuation.NDXTMC_SOURCE_ID]["request_mode"])
        self.assertEqual("full", metrics["sources"]["dqydj"]["request_mode"])
        for source, url, headers in client.calls:
            if source.startswith("nasdaq-") and source != valuation.NDXTMC_SOURCE_ID:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                self.assertGreaterEqual(query["fromdate"][0], "2026-05-01")
                self.assertEqual("tail", metrics["sources"][source]["request_mode"])
            if source in {"snowball", "gold", "ndxtmc-workbook"}:
                self.assertIn("If-None-Match", headers)

    def test_unchanged_ndxtmc_workbook_200_uses_only_online_tail(self):
        caches = make_cache_bundle()
        client = FakeClient(tail_prices=True)
        _payload, new_caches, _manifest, _metrics = run_build(
            caches=caches, manifest=make_manifest(), client=client
        )
        live_calls = [call for call in client.calls if call[0] == "ndxtmc-history"]
        self.assertEqual(1, len(live_calls))
        self.assertEqual(
            caches[valuation.NDXTMC_SOURCE_ID]["data"][0],
            new_caches[valuation.NDXTMC_SOURCE_ID]["data"][0],
        )

    def test_new_month_runs_all_five_full_price_scans(self):
        caches = make_cache_bundle()
        payload, _new, manifest, _metrics = run_build(
            caches=caches, manifest=make_manifest("2026-07"), client=FakeClient()
        )
        self.assertEqual("full", payload["cache"]["refresh_mode"])
        self.assertEqual("2026-08", manifest["last_full_refresh_month"])

    def test_ndxtmc_full_refresh_chunks_and_merges_live_history(self):
        client = FakeClient()
        payload, _new, _manifest, _metrics = run_build(client=client)
        chunks = [call for call in client.calls if call[0] == "ndxtmc-history"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("fresh", next(
            item for item in payload["assets"] if item["id"] == valuation.RELATIVE_PROXY_ASSET_ID
        )["status"])
        self.assertTrue(all("startDate" in call[2] and "endDate" in call[2] for call in chunks))

    def test_ndxtmc_failure_without_cache_only_disables_relative_proxy(self):
        payload, _new, _manifest, _metrics = run_build(
            client=FakeClient(fail={valuation.NDXTMC_SOURCE_ID})
        )
        status = {item["id"]: item["status"] for item in payload["assets"]}
        self.assertEqual("unavailable", status[valuation.RELATIVE_PROXY_ASSET_ID])
        self.assertTrue(all(
            status[asset_id] == "fresh"
            for asset_id in valuation.EXPECTED_ASSET_IDS
            if asset_id != valuation.RELATIVE_PROXY_ASSET_ID
        ))

    def test_ndxtmc_failure_with_cache_marks_relative_proxy_stale(self):
        caches = make_cache_bundle()
        payload, _new, _manifest, _metrics = run_build(
            caches=caches,
            manifest=make_manifest(),
            client=FakeClient(fail={valuation.NDXTMC_SOURCE_ID}),
        )
        asset = next(
            item for item in payload["assets"] if item["id"] == valuation.RELATIVE_PROXY_ASSET_ID
        )
        self.assertEqual("cached_stale", asset["status"])

    def test_full_scan_keeps_cached_boundary_for_lagging_pe_source(self):
        """A one-month DQYDJ lag must not discard the prior 120-month price window."""
        caches = make_cache_bundle()
        catalog, catalog_hash = load_catalog()
        payload, _new, _manifest, _metrics = valuation.build_payload(
            as_of=date(2026, 9, 3),
            now=datetime(2026, 9, 3, 7, 7, tzinfo=valuation.SHANGHAI_TZ),
            catalog=catalog,
            catalog_hash=catalog_hash,
            caches=caches,
            cache_states={source_id: "valid" for source_id in valuation.all_source_ids()},
            manifest=make_manifest("2026-08"),
            manifest_state="valid",
            client=FakeClient(),
        )
        self.assertEqual("full", payload["cache"]["refresh_mode"])
        self.assertTrue(
            all(
                asset["status"] == "fresh"
                for asset in payload["assets"]
                if asset["source_mode"] == "proxy"
            )
        )
        self.assertEqual("2016-08", next(
            asset for asset in payload["assets"] if asset["id"] == "sp-500-equal-weight"
        )["history"][0]["month"])

    def test_shared_spy_failure_without_cache_only_disables_proxies(self):
        payload, _new, _manifest, _metrics = run_build(
            client=FakeClient(fail={"nasdaq-spy"})
        )
        self.assertEqual("partial", payload["status"])
        status = {item["id"]: item["status"] for item in payload["assets"]}
        self.assertTrue(all(status[item] == "unavailable" for item in valuation.PROXY_ASSET_IDS))
        self.assertTrue(all(status[item] == "fresh" for item in valuation.DIRECT_ASSET_IDS))
        self.assertEqual("fresh", status[valuation.GOLD_ASSET_ID])

    def test_source_failure_with_cache_marks_only_dependents_stale(self):
        caches = make_cache_bundle()
        payload, new_caches, _manifest, metrics = run_build(
            caches=caches,
            manifest=make_manifest(),
            client=FakeClient(fail={"dqydj"}, tail_prices=True),
        )
        self.assertEqual("partial", payload["status"])
        proxies = [item for item in payload["assets"] if item["source_mode"] == "proxy"]
        self.assertTrue(all(item["status"] == "cached_stale" for item in proxies))
        self.assertTrue(all(item["status"] == "fresh" for item in payload["assets"] if item["source_mode"] != "proxy"))
        self.assertTrue(metrics["sources"]["dqydj"]["cache_fallback"])
        self.assertEqual(caches["dqydj"]["last_success_at"], new_caches["dqydj"]["last_success_at"])

    def test_failed_monthly_full_scan_does_not_advance_marker(self):
        caches = make_cache_bundle()
        _payload, _new, manifest, _metrics = run_build(
            caches=caches,
            manifest=make_manifest("2026-07"),
            client=FakeClient(fail={"nasdaq-rsp"}),
        )
        self.assertEqual("2026-07", manifest["last_full_refresh_month"])


class CacheAndArtifactTests(unittest.TestCase):
    def test_source_cache_fingerprint_change_is_a_miss(self):
        catalog, catalog_hash = load_catalog()
        cache = make_cache_bundle()["snowball"]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            valuation.atomic_write_json(root / "snowball.json", cache)
            loaded, states = valuation.load_source_caches(
                root, catalog, "0" * 64, AS_OF
            )
        self.assertNotIn("snowball", loaded)
        self.assertEqual("fingerprint_mismatch", states["snowball"])
        self.assertNotEqual(valuation.source_fingerprint(catalog_hash, "snowball"), "0" * 64)

    def test_corrupt_source_cache_is_a_miss(self):
        catalog, catalog_hash = load_catalog()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gold.json").write_text("{broken", encoding="utf-8")
            loaded, states = valuation.load_source_caches(root, catalog, catalog_hash, AS_OF)
        self.assertNotIn("gold", loaded)
        self.assertEqual("corrupt", states["gold"])

    def test_validator_accepts_byte_identical_multi_asset_artifacts(self):
        payload, _cache, _manifest, _metrics = run_build()
        document = valuation.render_html(
            payload, valuation.DEFAULT_PAGE_SCRIPT.read_text(encoding="utf-8")
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            publish = root / "public"
            valuation.atomic_write_json(output / "latest.json", payload)
            valuation.atomic_write_text(output / "latest.html", document)
            valuation.atomic_write_text(publish / "valuation" / "index.html", document)
            validated = validator.validate_local_artifacts(output, publish, "2026-08-25")
        self.assertEqual(8, len(validated["assets"]))

    def test_validator_rejects_inconsistent_proxy_percentile(self):
        payload, _cache, _manifest, _metrics = run_build()
        proxy = next(item for item in payload["assets"] if item["source_mode"] == "proxy")
        proxy["current"]["proxy_percentile_10y"] += 1
        with self.assertRaisesRegex(validator.ValidationError, "midrank"):
            validator.validate_payload(payload, "2026-08-25")

    def test_validator_rejects_source_rating_on_proxy(self):
        payload, _cache, _manifest, _metrics = run_build()
        proxy = next(item for item in payload["assets"] if item["source_mode"] == "proxy")
        proxy["current"]["source_rating"] = {"label": "高估"}
        with self.assertRaisesRegex(validator.ValidationError, "must not expose"):
            validator.validate_payload(payload, "2026-08-25")

    def test_validator_rejects_all_assets_unavailable(self):
        payload, _cache, _manifest, _metrics = run_build(
            client=FakeClient(fail=set(valuation.all_source_ids()))
        )
        self.assertEqual("unavailable", payload["status"])
        with self.assertRaisesRegex(validator.ValidationError, "All valuation assets"):
            validator.validate_payload(payload, "2026-08-25")


if __name__ == "__main__":
    unittest.main()
