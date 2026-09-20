import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import send_qdii_email as mailer
import report_update_metrics as metrics_reporter
import update_qdii_ranking as ranking
import validate_qdii_ranking as validator

RUN_DATE = "2026-08-20"

def make_exchange_premium():
    entries, fingerprint = ranking.load_exchange_premium_catalog(
        ranking.DEFAULT_US_EQUITY_ETF_CATALOG
    )
    records = []
    for index, entry in enumerate(entries):
        premium = round((index - 10) / 10, 2)
        records.append(
            {
                **entry,
                "market_price_cny": round(1 + premium / 100, 4),
                "iopv_cny": 1.0,
                "reference_value_type": "iopv",
                "reference_value_cny": 1.0,
                "reference_value_date": None,
                "reference_value_source_url": ranking.ETF_QUOTE_PAGE_URL,
                "source_discount_pct": -premium,
                "premium_pct": premium,
                "change_pct": round(index / 100, 2),
                "turnover_cny": float(10_000_000 + index),
                "quote_date": RUN_DATE,
                "updated_at": f"{RUN_DATE}T15:00:00+08:00",
                "quote_source_url": f"https://example.test/quote/{entry['code']}",
                "quote_status": "fresh",
                "holding_cost": {
                    "status": "parsed",
                    "annualized_pct": round(0.6 + index / 100, 2),
                    "measurement_date": None,
                    "source_title": f"{entry['name']}基金产品资料概要更新",
                    "source_published_date": RUN_DATE,
                    "source_url": f"https://example.test/fee/{entry['code']}.pdf",
                },
            }
        )
    records.sort(key=ranking.exchange_premium_sort_key)
    return {
        "schema_version": 2,
        "status": "fresh",
        "requested_at": f"{RUN_DATE}T07:07:00+08:00",
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


def make_record(rank, ranking_list="us_main"):
    code = f"{rank if ranking_list == 'us_main' else 100000 + rank:06d}"
    source = f"https://example.test/{code}/notice.pdf"
    confirmed = 100.0 - rank
    possible = confirmed + (0.5 if rank == 1 else 0.0)
    return {
        "rank": rank,
        "ranking_list": ranking_list,
        "routing_reason": (
            "confirmed_us_exposure"
            if ranking_list == "us_main"
            else "us_exposure_below_threshold"
        ),
        "code": code,
        "name": f"全球科技精选{rank}(QDII)A",
        "fund_type": "QDII-普通股票",
        "management_style": "active",
        "product_structure_tags": ["主动", "股票", "大盘成长"],
        "contract_benchmark": {
            "status": "recognized",
            "benchmark_text": "纳斯达克100指数收益率×95%+人民币活期存款利率×5%",
            "benchmark_id": "nasdaq-100" if ranking_list == "us_main" else "dax",
            "benchmark_name": "纳斯达克100指数" if ranking_list == "us_main" else "德国DAX指数",
            "benchmark_weight_pct": 95.0,
            "market_scope": "us" if ranking_list == "us_main" else "non_us",
            "market_label": "美国" if ranking_list == "us_main" else "德国",
            "asset_class": "equity",
            "style_label": "大盘成长" if ranking_list == "us_main" else "大盘宽基",
            "structure": "standard",
            "excluded_target": False,
            "components": [
                {
                    "benchmark_id": "nasdaq-100" if ranking_list == "us_main" else "dax",
                    "benchmark_name": "纳斯达克100指数" if ranking_list == "us_main" else "德国DAX指数",
                    "weight_pct": 95.0,
                    "market_scope": "us" if ranking_list == "us_main" else "non_us",
                    "market_label": "美国" if ranking_list == "us_main" else "德国",
                    "asset_class": "equity",
                    "style_label": "大盘成长" if ranking_list == "us_main" else "大盘宽基",
                    "structure": "standard",
                    "excluded_target": False,
                }
            ],
            "management_style": "active",
            "prospectus_title": "更新招募说明书",
            "prospectus_published_date": "2026-06-01",
            "source_url": f"https://example.test/{code}/prospectus.pdf",
            "product_summary_status": "matched",
            "product_summary_published_date": "2026-06-02",
            "product_summary_source_url": f"https://example.test/{code}/summary.pdf",
            "catalog_fingerprint": "a" * 64,
        },
        "holding_cost": {
            "status": "parsed",
            "annualized_pct": 0.66 + rank / 100,
            "measurement_date": "2026-06-01",
            "source_title": "人民币产品资料概要",
            "source_published_date": "2026-06-02",
            "source_url": f"https://example.test/{code}/summary.pdf",
        },
        "institution_holding_ratio_pct": 50.0 - rank,
        "holder_report_date": "2025-12-31",
        "inception_date": "2010-01-01",
        "scale_billion_cny": 10.0 + rank,
        "scale_report_date": "2026-06-30",
        "purchase_status": "limited",
        "purchase_status_text": "限额申购",
        "fund_page_url": f"https://example.test/fund/{code}",
        "performance_source_url": f"https://example.test/performance/{code}.js",
        "nav_history_start_date": "2010-01-04",
        "nav_history_end_date": "2026-08-18",
        "one_year_return_pct": 20.0 + rank,
        "one_year_max_drawdown_pct": -10.0 - rank,
        "one_year_performance_start_date": "2025-08-18",
        "one_year_performance_end_date": "2026-08-18",
        "three_year_return_pct": 80.0 + rank,
        "three_year_boundary_shortfall_days": 0,
        "three_year_max_drawdown_pct": -20.0 - rank,
        "three_year_performance_start_date": "2023-08-18",
        "three_year_performance_end_date": "2026-08-18",
        "five_year_return_pct": 120.0 + rank,
        "five_year_performance_start_date": "2021-08-18",
        "five_year_performance_end_date": "2026-08-18",
        "ten_year_return_pct": 220.0 + rank,
        "ten_year_performance_start_date": "2016-08-18",
        "ten_year_performance_end_date": "2026-08-18",
        "nasdaq100_fit": {
            "correlation": round(1.0 - rank / 100, 4),
            "beta": round(1.0 + rank / 100, 4),
            "tracking_error_pct": 4.0 + rank,
            "observations": 154,
            "start_date": "2023-08-18",
            "end_date": "2026-08-18",
        },
        "us_equity_exposure": {
            "confirmed_pct": confirmed,
            "possible_pct": possible,
            "direct_us_pct": confirmed,
            "lookthrough_confirmed_pct": 0.0,
            "unresolved_pct": possible - confirmed,
            "report_date": "2026-06-30",
            "published_date": "2026-07-21",
            "source_url": f"https://example.test/{code}/report.pdf",
            "components": [],
            "status": "qualified",
        },
        "quota_status": "limited",
        "quota_confidence": "high",
        "direct_limit": {
            "status": "limited",
            "amount_cny": 100000,
            "effective_date": "2026-08-01",
            "source_url": source,
            "confidence": "high",
        },
        "agency_limit": {
            "status": "limited",
            "amount_cny": 1000,
            "effective_date": "2026-08-01",
            "source_url": source,
            "confidence": "high",
        },
        "share_class_rule": "A/C separate",
        "channel_rule": "direct and agency limits differ",
        "quota_source_urls": [source],
    }


def make_global_record(rank):
    record = make_record(rank, "global_supplement")
    record["us_equity_exposure"].update(
        {
            "confirmed_pct": 30.0 - rank,
            "possible_pct": 35.0 - rank,
            "direct_us_pct": 30.0 - rank,
            "unresolved_pct": 5.0,
            "status": "excluded",
        }
    )
    total_return = float(record["three_year_return_pct"])
    span_days = 1096
    annualized = ((1 + total_return / 100) ** (365 / span_days) - 1) * 100
    record["three_year_annualized_return_pct"] = round(annualized, 2)
    record["return_drawdown_ratio"] = round(
        annualized / abs(float(record["three_year_max_drawdown_pct"])), 4
    )
    return record


def make_payload():
    return {
        "schema_version": 15,
        "run_date": RUN_DATE,
        "generated_at": "2026-08-20T09:08:00+08:00",
        "holder_report_date": "2025-12-31",
        "holder_period_fund_count": 24000,
        "filters": {
            "top": 10,
            "min_scale_billion_cny": None,
            "min_age_years": 3,
            "min_three_year_return_pct": 30.0,
            "three_year_boundary_tolerance_days": 7,
            "min_five_year_return_pct_if_available": 50.0,
            "min_ten_year_return_pct_if_available": 100.0,
            "min_us_equity_pct": 50.0,
            "min_direct_limit_cny_inclusive": 200,
            "base_candidates_total": 42,
            "performance_candidates_scanned": 42,
            "performance_qualified_count": 27,
            "contract_candidates_scanned": 27,
            "contract_metadata_resolved_count": 8,
            "us_equity_candidates_scanned": 27,
            "us_routed_count": 12,
            "global_routed_count": 15,
            "us_quota_candidates_scanned": 12,
            "us_quota_qualified_count": 3,
            "global_quota_candidates_scanned": 15,
            "global_quota_qualified_count": 3,
            "full_scan_completed": True,
            "ranking_method": validator.EXPECTED_RANKING_METHOD,
            "global_supplement_ranking_method": validator.EXPECTED_GLOBAL_RANKING_METHOD,
            "us_equity_method": "conservative confirmed lower bound",
            "contract_benchmark_method": "latest prospectus",
            "us_main_exclude_keywords": ["亚洲", "中国", "港"],
            "global_exclude_keywords": [],
            "exclude_fund_types": ["QDII-商品", "QDII-混合债", "QDII-纯债"],
            "exclude_asset_classes": ["bond", "commodity"],
            "share_class": "OTC RMB A or explicit RMB primary share without C/D marker",
            "purchasable_only": True,
        },
        "cache": {
            "nasdaq100_benchmark": {"cache_hits": 1, "fetches": 2, "fallbacks": 0},
            "performance": {"hits": 42, "misses": 0, "corrupt_rebuilds": 0},
            "fund_us_equity_exposures": {
                "hits": 27,
                "misses": 0,
                "corrupt_rebuilds": 0,
            },
            "announcement_pdfs": {
                "hits": 0,
                "downloads": 0,
                "corrupt_redownloads": 0,
            },
            "underlying_exposures": {"hits": 0, "misses": 0},
        },
        "benchmark": {
            "symbol": "XNDX",
            "name": "NASDAQ-100 Total Return",
            "return_type": "gross_total_return",
            "currency": "CNY",
            "window_years": 3,
            "frequency": "weekly",
            "max_source_staleness_days": 7,
            "min_observations": 140,
            "min_span_days": 1000,
            "index_source_url": "https://example.test/xndx",
            "fx_source_url": "https://example.test/usd-cny",
            "index_start_date": "2023-08-06",
            "index_latest_date": "2026-08-19",
            "fx_start_date": "2023-08-06",
            "fx_latest_date": "2026-08-19",
        },
        "exchange_premium": make_exchange_premium(),
        "records": [make_record(rank) for rank in range(1, 4)],
        "global_supplement": {
            "ranking_method": validator.EXPECTED_GLOBAL_RANKING_METHOD,
            "qualified_count": 3,
            "records": sorted(
                [make_global_record(rank) for rank in range(1, 4)],
                key=lambda item: -float(item["return_drawdown_ratio"]),
            ),
        },
        "nasdaq100_otc": {
            "ranking_method": validator.EXPECTED_NASDAQ100_OTC_RANKING_METHOD,
            "candidate_count": 0,
            "missing_fields": {
                "two_year_return": 0,
                "holding_cost": 0,
                "nasdaq100_fit_2y": 0,
                "common_period_return": 0,
                "common_period_max_drawdown": 0,
                "nasdaq100_fit_common_period": 0,
                "inception_date": 0,
            },
            "comparison_window": {
                "status": "unavailable",
                "minimum_anchor_age_years": 1,
                "max_boundary_delay_days": 7,
                "anchor_inception_date": None,
                "anchor_funds": [],
                "start_date": None,
                "end_date": None,
                "comparable_count": 0,
                "error": "No candidates",
            },
            "records": [],
        },
        "exclusion_summary": [
            {
                "reason": "direct_limit_below_threshold",
                "label": "直销额度低于 200 元",
                "count": 2,
                "codes": ["000099", "000100"],
            }
        ],
        "warnings": [
            "跳过未完整披露的持有人报告期 2026-06-30：覆盖率不足。",
            "000001 Sample ETF 无法按 2026-06-30 的可用数据穿透，其 0.50% 仓位仅计入可能上限。",
            "000099 美股占比区间 49.00%-51.00% 跨越 50% 阈值，按确认下限进入全球补充榜。",
        ],
        "sources": {},
    }


def write_artifacts(root, payload):
    output_dir = root / "output"
    publish_dir = root / "public"
    ranking.write_json(output_dir / "latest.json", payload)
    ranking.write_csv(output_dir / "latest.csv", payload)
    ranking.write_markdown(output_dir / "latest.md", payload)
    ranking.write_html(output_dir / "latest.html", payload)
    ranking.write_html(publish_dir / "index.html", payload)
    return output_dir, publish_dir
