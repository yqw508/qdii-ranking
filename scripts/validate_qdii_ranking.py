#!/usr/bin/env python3
"""Compatibility facade for QDII ranking artifact validation."""

from qdii_validation.cli import main, parse_args
from qdii_validation.common import (
    BOUNDARY_WARNING_RE,
    ETF_MARKET_LIST_API_URL,
    ETF_QUOTE_API_URL,
    EXPECTED_FILTERS,
    EXPECTED_GLOBAL_RANKING_METHOD,
    EXPECTED_NASDAQ100_OTC_RANKING_METHOD,
    EXPECTED_PREMIUM_CODES,
    EXPECTED_PREMIUM_GROUP_ORDER,
    EXPECTED_RANKING_METHOD,
    EXPECTED_US_MAIN_EXCLUDE_KEYWORDS,
    MARKDOWN_ROW_RE,
    NASDAQ100_MIN_OBSERVATIONS,
    NASDAQ100_MIN_SPAN_DAYS,
    NASDAQ100_OTC_NAME_RE,
    ROUTING_REASON_BELOW_US_THRESHOLD,
    ROUTING_REASON_CONFIRMED_US,
    ROUTING_REASON_GEOGRAPHY_OVERRIDE,
    SHANGHAI_TZ,
    RankingHtmlParser,
    ValidationError,
    as_number,
    classify_warnings,
    current_shanghai_date,
    format_beta,
    format_correlation,
    format_limit,
    format_percentage,
    is_reportable_warning,
    parse_date,
    require,
    validate_three_year_window,
    years_ago,
)
from qdii_validation.formats import validate_csv, validate_markdown
from qdii_validation.premium import (
    format_premium_turnover,
    format_premium_value,
    parse_html,
    premium_band,
    validate_exchange_premium,
    validate_html_document,
)
from qdii_validation.publish import (
    validate_deployment,
    validate_email_rendering,
    validate_local_artifacts,
)
from qdii_validation.schema import (
    load_payload,
    validate_benchmark,
    validate_contract_benchmark,
    validate_exclusion_summary,
    validate_filters,
    validate_holding_cost,
    validate_limit,
    validate_nasdaq100_otc_section,
    validate_nasdaq_fit,
    validate_records,
)

__all__ = [
    "BOUNDARY_WARNING_RE", "ETF_MARKET_LIST_API_URL", "ETF_QUOTE_API_URL",
    "EXPECTED_FILTERS", "EXPECTED_GLOBAL_RANKING_METHOD",
    "EXPECTED_NASDAQ100_OTC_RANKING_METHOD", "EXPECTED_PREMIUM_CODES",
    "EXPECTED_PREMIUM_GROUP_ORDER", "EXPECTED_RANKING_METHOD",
    "EXPECTED_US_MAIN_EXCLUDE_KEYWORDS", "MARKDOWN_ROW_RE",
    "NASDAQ100_MIN_OBSERVATIONS", "NASDAQ100_MIN_SPAN_DAYS", "NASDAQ100_OTC_NAME_RE",
    "ROUTING_REASON_BELOW_US_THRESHOLD", "ROUTING_REASON_CONFIRMED_US",
    "ROUTING_REASON_GEOGRAPHY_OVERRIDE", "RankingHtmlParser", "SHANGHAI_TZ",
    "ValidationError", "as_number", "classify_warnings", "current_shanghai_date",
    "format_beta", "format_correlation", "format_limit", "format_percentage",
    "format_premium_turnover", "format_premium_value", "is_reportable_warning",
    "load_payload", "main", "parse_args", "parse_date", "parse_html", "premium_band",
    "require", "validate_benchmark", "validate_contract_benchmark", "validate_csv",
    "validate_deployment", "validate_email_rendering", "validate_exchange_premium",
    "validate_exclusion_summary", "validate_filters", "validate_holding_cost",
    "validate_html_document", "validate_limit", "validate_local_artifacts",
    "validate_markdown", "validate_nasdaq100_otc_section", "validate_nasdaq_fit",
    "validate_records", "validate_three_year_window", "years_ago",
]


if __name__ == "__main__":
    raise SystemExit(main())
