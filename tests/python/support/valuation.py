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
