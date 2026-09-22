"""CSV renderer for the ranking public contract."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..atomic import atomic_render
from ..sources.otc_shares import EVIDENCE_FIELDS
from .common import format_limit


def _all_ranking_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *payload.get("records", []),
        *((payload.get("global_supplement") or {}).get("records") or []),
        *((payload.get("nasdaq100_otc") or {}).get("records") or []),
    ]


def _write_csv(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "ranking_list",
        "routing_reason",
        "rank",
        "code",
        "name",
        "fund_type",
        "management_style",
        "product_structure_tags",
        "contract_benchmark_status",
        "contract_benchmark_name",
        "contract_benchmark_text",
        "contract_benchmark_weight_pct",
        "contract_benchmark_components",
        "contract_market_scope",
        "contract_market_label",
        "contract_asset_class",
        "contract_style_label",
        "contract_structure",
        "nasdaq100_name_contract_match",
        "nasdaq100_name_contract_warning",
        "contract_prospectus_published_date",
        "contract_source_url",
        "product_summary_status",
        "product_summary_source_url",
        "holding_cost_status",
        "holding_cost_annualized_pct",
        "holding_cost_measurement_date",
        "holding_cost_source_url",
        "institution_holding_ratio_pct",
        "holder_report_date",
        "inception_date",
        "scale_billion_cny",
        "scale_report_date",
        "nav_history_start_date",
        "nav_history_end_date",
        "one_year_return_pct",
        "one_year_max_drawdown_pct",
        "one_year_performance_start_date",
        "one_year_performance_end_date",
        "two_year_return_pct",
        "two_year_max_drawdown_pct",
        "two_year_performance_start_date",
        "two_year_performance_end_date",
        "common_period_return_pct",
        "common_period_max_drawdown_pct",
        "common_period_performance_start_date",
        "common_period_performance_end_date",
        "common_period_error",
        "three_year_return_pct",
        "three_year_max_drawdown_pct",
        "three_year_performance_start_date",
        "three_year_performance_end_date",
        "three_year_boundary_shortfall_days",
        "five_year_return_pct",
        "five_year_performance_start_date",
        "five_year_performance_end_date",
        "ten_year_return_pct",
        "ten_year_performance_start_date",
        "ten_year_performance_end_date",
        "nasdaq100_correlation",
        "nasdaq100_beta",
        "nasdaq100_tracking_error_pct",
        "nasdaq100_observations",
        "nasdaq100_start_date",
        "nasdaq100_end_date",
        "nasdaq100_2y_correlation",
        "nasdaq100_2y_beta",
        "nasdaq100_2y_tracking_error_pct",
        "nasdaq100_2y_observations",
        "nasdaq100_2y_start_date",
        "nasdaq100_2y_end_date",
        "nasdaq100_common_correlation",
        "nasdaq100_common_beta",
        "nasdaq100_common_tracking_error_pct",
        "nasdaq100_common_observations",
        "nasdaq100_common_start_date",
        "nasdaq100_common_end_date",
        "three_year_annualized_return_pct",
        "return_drawdown_ratio",
        "us_equity_confirmed_pct",
        "us_equity_possible_pct",
        "us_equity_status",
        "us_equity_report_date",
        "us_equity_source_url",
        "purchase_status",
        "direct_limit",
        "agency_limit",
        "share_class_rule",
        "channel_rule",
        "quota_confidence",
        "fund_page_url",
        "performance_source_url",
        "quota_source_urls",
        *(f"share_evidence_{field}" for field in EVIDENCE_FIELDS),
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in _all_ranking_records(payload):
            fit = item.get("nasdaq100_fit") or {}
            fit_2y = item.get("nasdaq100_fit_2y") or {}
            fit_common = item.get("nasdaq100_fit_common_period") or {}
            exposure = item.get("us_equity_exposure") or {}
            contract = item["contract_benchmark"]
            holding_cost = item["holding_cost"]
            writer.writerow(
                {
                    **{field: item.get(field) for field in fields},
                    **{f"share_evidence_{field}": (item.get("share_class_evidence") or {}).get(field)
                       for field in EVIDENCE_FIELDS},
                    "product_structure_tags": " | ".join(item["product_structure_tags"]),
                    "contract_benchmark_status": contract["status"],
                    "contract_benchmark_name": contract["benchmark_name"],
                    "contract_benchmark_text": contract["benchmark_text"],
                    "contract_benchmark_weight_pct": contract["benchmark_weight_pct"],
                    "contract_benchmark_components": json.dumps(
                        contract["components"], ensure_ascii=False, separators=(",", ":")
                    ),
                    "contract_market_scope": contract["market_scope"],
                    "contract_market_label": contract["market_label"],
                    "contract_asset_class": contract["asset_class"],
                    "contract_style_label": contract["style_label"],
                    "contract_structure": contract["structure"],
                    "nasdaq100_name_contract_match": item.get("contract_name_match"),
                    "nasdaq100_name_contract_warning": item.get("contract_name_match_warning"),
                    "nasdaq100_common_correlation": fit_common.get("correlation"),
                    "nasdaq100_common_beta": fit_common.get("beta"),
                    "nasdaq100_common_tracking_error_pct": fit_common.get(
                        "tracking_error_pct"
                    ),
                    "nasdaq100_common_observations": fit_common.get("observations"),
                    "nasdaq100_common_start_date": fit_common.get("start_date"),
                    "nasdaq100_common_end_date": fit_common.get("end_date"),
                    "contract_prospectus_published_date": contract[
                        "prospectus_published_date"
                    ],
                    "contract_source_url": contract["source_url"],
                    "product_summary_status": contract["product_summary_status"],
                    "product_summary_source_url": contract.get(
                        "product_summary_source_url"
                    ),
                    "holding_cost_status": holding_cost["status"],
                    "holding_cost_annualized_pct": holding_cost["annualized_pct"],
                    "holding_cost_measurement_date": holding_cost["measurement_date"],
                    "holding_cost_source_url": holding_cost["source_url"],
                    "nasdaq100_correlation": fit.get("correlation"),
                    "nasdaq100_beta": fit.get("beta"),
                    "nasdaq100_tracking_error_pct": fit.get("tracking_error_pct"),
                    "nasdaq100_observations": fit.get("observations"),
                    "nasdaq100_start_date": fit.get("start_date"),
                    "nasdaq100_end_date": fit.get("end_date"),
                    "nasdaq100_2y_correlation": fit_2y.get("correlation"),
                    "nasdaq100_2y_beta": fit_2y.get("beta"),
                    "nasdaq100_2y_tracking_error_pct": fit_2y.get("tracking_error_pct"),
                    "nasdaq100_2y_observations": fit_2y.get("observations"),
                    "nasdaq100_2y_start_date": fit_2y.get("start_date"),
                    "nasdaq100_2y_end_date": fit_2y.get("end_date"),
                    "us_equity_confirmed_pct": exposure.get("confirmed_pct"),
                    "us_equity_possible_pct": exposure.get("possible_pct"),
                    "us_equity_status": exposure.get("status"),
                    "us_equity_report_date": exposure.get("report_date"),
                    "us_equity_source_url": exposure.get("source_url"),
                    "direct_limit": format_limit(item["direct_limit"]),
                    "agency_limit": format_limit(item["agency_limit"]),
                    "quota_source_urls": " | ".join(item["quota_source_urls"]),
                }
            )


def render_csv(path: Path, payload: dict[str, Any]) -> None:
    atomic_render(path, lambda temporary: _write_csv(temporary, payload))
