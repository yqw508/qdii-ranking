"""Configuration and stable ranking constants.

Business rules live here so the CLI, pipeline and validators do not need to
duplicate literal thresholds or public schema versions.
"""

from __future__ import annotations

import re
from pathlib import Path

RANKING_SCHEMA_VERSION = 15
DEFAULT_EXCLUDE_KEYWORDS = ["亚洲", "中国", "港"]
EXCLUDED_FUND_TYPES = frozenset({"QDII-纯债", "QDII-混合债", "QDII-商品"})

FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
HOLDER_API_URL = "https://fund.eastmoney.com/data/FundDataPortfolio_Interface.aspx"
ANNOUNCEMENT_API_URL = "https://api.fund.eastmoney.com/f10/JJGG"
FUND_PAGE_URL = "https://fund.eastmoney.com/{code}.html"
PERFORMANCE_DATA_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js?v={cache_buster}"
ANNOUNCEMENT_PDF_URL = "https://pdf.dfcfw.com/pdf/H2_{announcement_id}_1.pdf"
NASDAQ100_HISTORY_PAGE_URL = "https://indexes.nasdaq.com/Index/History/XNDX"
NASDAQ100_HISTORY_DATA_URL = "https://indexes.nasdaq.com/Index/HistoryChartData"
SAFE_USD_CNY_HISTORY_URL = "https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do"
ETF_QUOTE_API_URL = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
ETF_MARKET_LIST_API_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
ETF_LOF_NAV_API_URL = "https://api.fund.eastmoney.com/f10/lsjz"
ETF_QUOTE_PAGE_URL = "https://quote.eastmoney.com/center/gridlist.html#fund_etf"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)

PERFORMANCE_WORKERS = 10
DOCUMENT_WORKERS = 6
ETF_MARKET_LIST_PAGE_SIZE = 100
ETF_MARKET_LIST_FS = "m:1+t:9,m:0+t:10"
BENCHMARK_WINDOW_YEARS = 3
BENCHMARK_HISTORY_BUFFER_DAYS = 14
BENCHMARK_MAX_STALENESS_DAYS = 7
NASDAQ100_MIN_OBSERVATIONS = 140
NASDAQ100_MIN_SPAN_DAYS = 1000
NASDAQ100_TWO_YEAR_WINDOW_YEARS = 2
NASDAQ100_TWO_YEAR_MIN_OBSERVATIONS = 90
NASDAQ100_TWO_YEAR_MIN_SPAN_DAYS = 600
NASDAQ100_COMMON_MIN_AGE_YEARS = 1
NASDAQ100_COMMON_MAX_BOUNDARY_DELAY_DAYS = 7
NASDAQ100_COMMON_MIN_OBSERVATIONS = 45
NASDAQ100_COMMON_MIN_SPAN_DAYS = 330
NASDAQ100_COMMON_MIN_COVERAGE_RATIO = 0.85
THREE_YEAR_BOUNDARY_TOLERANCE_DAYS = 7
DEFAULT_MIN_DIRECT_LIMIT_CNY = 200
DEFAULT_MIN_THREE_YEAR_RETURN_PCT = 30.0
DEFAULT_MIN_FIVE_YEAR_RETURN_PCT = 50.0
DEFAULT_MIN_TEN_YEAR_RETURN_PCT = 100.0

PERFORMANCE_CACHE_SCHEMA_VERSION = 5
ANNOUNCEMENT_INDEX_CACHE_SCHEMA_VERSION = 1
CONTRACT_RESULT_CACHE_SCHEMA_VERSION = 1
CONTRACT_RESULT_METHOD_VERSION = 1
QUOTA_NOTICE_CACHE_SCHEMA_VERSION = 1
QUOTA_NOTICE_METHOD_VERSION = 1
BENCHMARK_CACHE_SCHEMA_VERSION = 1
FUND_EXPOSURE_CACHE_SCHEMA_VERSION = 1
US_EQUITY_METHOD_VERSION = 2
ETF_PREMIUM_CACHE_SCHEMA_VERSION = 1
ETF_PREMIUM_CATALOG_SCHEMA_VERSION = 1
ETF_HOLDING_COST_CACHE_SCHEMA_VERSION = 1
ETF_HOLDING_COST_METHOD_VERSION = 1
ETF_PREMIUM_DELAY_MINUTES = 15
ETF_PREMIUM_GROUP_ORDER = ("标普500", "纳指100", "美国50", "道琼斯", "行业主题")

ROUTING_REASON_CONFIRMED_US = "confirmed_us_exposure"
ROUTING_REASON_BELOW_US_THRESHOLD = "us_exposure_below_threshold"
ROUTING_REASON_GEOGRAPHY_OVERRIDE = "us_main_name_geography_override"
ROUTING_REASON_LABELS = {
    ROUTING_REASON_CONFIRMED_US: "美股确认达标",
    ROUTING_REASON_BELOW_US_THRESHOLD: "美股确认不足",
    ROUTING_REASON_GEOGRAPHY_OVERRIDE: "地域名称分流",
}

NASDAQ100_OTC_NAME_RE = re.compile(
    r"(?:纳斯达克\s*100|纳指\s*100|NASDAQ\s*[-－]?\s*100)", re.I
)
NOTICE_TITLE_RE = re.compile(
    r"大额申购|申购.{0,20}(?:限额|业务上限)|(?:限额|业务上限).{0,20}申购|恢复.{0,12}申购"
)
REPORT_TITLE_EXCLUDE_RE = re.compile(r"摘要|提示性公告")

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_US_EQUITY_CATALOG = REPOSITORY_ROOT / "references" / "us-equity-instruments.json"
DEFAULT_CONTRACT_BENCHMARK_CATALOG = (
    REPOSITORY_ROOT / "references" / "contract-benchmarks.json"
)
DEFAULT_US_EQUITY_ETF_CATALOG = REPOSITORY_ROOT / "references" / "us-equity-etfs.json"
