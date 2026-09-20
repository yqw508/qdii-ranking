import unittest
import json
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import update_qdii_ranking as ranking

def sample_exchange_premium(run_date="2026-08-20"):
    entries, fingerprint = ranking.load_exchange_premium_catalog(
        ranking.DEFAULT_US_EQUITY_ETF_CATALOG
    )
    records = []
    for index, entry in enumerate(entries):
        premium = round((index - 10) / 10, 2)
        iopv = 1.0
        records.append(
            {
                **entry,
                "market_price_cny": round(iopv * (1 + premium / 100), 4),
                "iopv_cny": iopv,
                "reference_value_type": "iopv",
                "reference_value_cny": iopv,
                "reference_value_date": None,
                "reference_value_source_url": ranking.ETF_QUOTE_PAGE_URL,
                "source_discount_pct": -premium,
                "premium_pct": premium,
                "change_pct": round(index / 100, 2),
                "turnover_cny": float(10_000_000 + index),
                "quote_date": run_date,
                "updated_at": f"{run_date}T15:00:00+08:00",
                "quote_source_url": f"https://example.test/quote/{entry['code']}",
                "quote_status": "fresh",
                "holding_cost": {
                    "status": "parsed",
                    "annualized_pct": round(0.6 + index / 100, 2),
                    "measurement_date": None,
                    "source_title": f"{entry['name']}基金产品资料概要更新",
                    "source_published_date": run_date,
                    "source_url": f"https://example.test/fee/{entry['code']}.pdf",
                },
            }
        )
    records.sort(key=ranking.exchange_premium_sort_key)
    return {
        "schema_version": 2,
        "status": "fresh",
        "requested_at": f"{run_date}T07:07:00+08:00",
        "quote_delay_minutes": 15,
        "expected_count": len(entries),
        "fresh_count": len(entries),
        "cache_hit_count": 0,
        "catalog_fingerprint": fingerprint,
        "group_order": list(ranking.ETF_PREMIUM_GROUP_ORDER),
        "source_name": "东方财富ETF行情",
        "source_url": ranking.ETF_QUOTE_PAGE_URL,
        "refresh_url": ranking.exchange_premium_quote_url(entries),
        "records": records,
    }
