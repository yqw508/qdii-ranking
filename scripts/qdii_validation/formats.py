"""CSV and Markdown artifact validation."""

import csv
import json
import math
from pathlib import Path
from typing import Any

from .common import (
    MARKDOWN_ROW_RE,
    ValidationError,
    format_beta,
    format_correlation,
    format_limit,
    format_percentage,
    mailer,
    require,
)


def _validate_common_fit_csv(
    row: dict[str, str], record: dict[str, Any], code: str
) -> None:
    fit = record.get("nasdaq100_fit_common_period") or {}
    fields = {
        "nasdaq100_common_correlation": "correlation",
        "nasdaq100_common_beta": "beta",
        "nasdaq100_common_tracking_error_pct": "tracking_error_pct",
        "nasdaq100_common_observations": "observations",
        "nasdaq100_common_start_date": "start_date",
        "nasdaq100_common_end_date": "end_date",
    }
    for csv_field, fit_field in fields.items():
        expected = fit.get(fit_field)
        require(
            row[csv_field] == ("" if expected is None else str(expected)),
            f"CSV {csv_field} differs for {code}",
        )


def validate_csv(path: Path, records: list[dict[str, Any]]) -> None:
    require(path.is_file(), f"Missing artifact: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except OSError as exc:
        raise ValidationError(f"Could not read {path}: {exc}") from exc
    require(len(rows) == len(records), "CSV record count differs from JSON")
    for record, row in zip(records, rows):
        code = record["code"]
        for field in ("ranking_list", "routing_reason", "code", "name"):
            require(row.get(field) == str(record[field]), f"CSV {field} differs for {code}")
        require(row.get("rank") == str(record["rank"]), f"CSV rank differs for {code}")
        require(
            row.get("three_year_boundary_shortfall_days") == str(record["three_year_boundary_shortfall_days"]),
            f"CSV boundary shortfall differs for {code}",
        )
        for field in (
            "institution_holding_ratio_pct",
            "scale_billion_cny",
            "one_year_return_pct",
            "one_year_max_drawdown_pct",
            "three_year_return_pct",
            "three_year_max_drawdown_pct",
        ):
            expected = record.get(field)
            if expected is None:
                require(row[field] == "", f"CSV {field} differs for {code}")
            else:
                require(
                    math.isclose(float(row[field]), float(expected), abs_tol=1e-9),
                    f"CSV {field} differs for {code}",
                )
        for field in (
            "nav_history_start_date",
            "nav_history_end_date",
            "two_year_performance_start_date",
            "two_year_performance_end_date",
            "three_year_performance_start_date",
            "three_year_performance_end_date",
            "five_year_performance_start_date",
            "five_year_performance_end_date",
            "ten_year_performance_start_date",
            "ten_year_performance_end_date",
        ):
            require(row[field] == str(record.get(field) or ""), f"CSV {field} differs for {code}")
        for field in ("five_year_return_pct", "ten_year_return_pct"):
            expected = "" if record.get(field) is None else str(record[field])
            require(row[field] == expected, f"CSV {field} differs for {code}")
        for field in (
            "two_year_return_pct",
            "two_year_max_drawdown_pct",
            "common_period_return_pct",
            "common_period_max_drawdown_pct",
        ):
            expected = "" if record.get(field) is None else str(record[field])
            require(row[field] == expected, f"CSV {field} differs for {code}")
        for field in (
            "common_period_performance_start_date",
            "common_period_performance_end_date",
            "common_period_error",
        ):
            require(
                row[field] == str(record.get(field) or ""),
                f"CSV {field} differs for {code}",
            )
        contract = record["contract_benchmark"]
        contract_fields = {
            "contract_benchmark_status": "status",
            "contract_benchmark_name": "benchmark_name",
            "contract_benchmark_text": "benchmark_text",
            "contract_market_scope": "market_scope",
            "contract_market_label": "market_label",
            "contract_asset_class": "asset_class",
            "contract_style_label": "style_label",
            "contract_structure": "structure",
            "contract_source_url": "source_url",
            "product_summary_status": "product_summary_status",
        }
        for csv_field, json_field in contract_fields.items():
            require(
                row.get(csv_field) == str(contract.get(json_field) or ""),
                f"CSV {csv_field} differs for {code}",
            )
        require(
            json.loads(row["contract_benchmark_components"])
            == contract["components"],
            f"CSV contract components differ for {code}",
        )
        expected_weight = (
            "" if contract["benchmark_weight_pct"] is None else str(contract["benchmark_weight_pct"])
        )
        require(
            row["contract_benchmark_weight_pct"] == expected_weight,
            f"CSV contract benchmark weight differs for {code}",
        )
        cost = record["holding_cost"]
        require(row["holding_cost_status"] == cost["status"], f"CSV holding cost status differs for {code}")
        expected_cost = "" if cost["annualized_pct"] is None else str(cost["annualized_pct"])
        require(row["holding_cost_annualized_pct"] == expected_cost, f"CSV holding cost differs for {code}")
        require(row["holding_cost_measurement_date"] == str(cost.get("measurement_date") or ""), f"CSV holding cost date differs for {code}")
        require(row["holding_cost_source_url"] == str(cost.get("source_url") or ""), f"CSV holding cost source differs for {code}")
        require(
            row.get("product_structure_tags")
            == " | ".join(record["product_structure_tags"]),
            f"CSV product structure tags differ for {code}",
        )
        fit = record["nasdaq100_fit"] or {}
        for csv_field, fit_field in (
            ("nasdaq100_correlation", "correlation"),
            ("nasdaq100_beta", "beta"),
            ("nasdaq100_tracking_error_pct", "tracking_error_pct"),
        ):
            expected = fit.get(fit_field)
            if expected is None:
                require(row[csv_field] == "", f"CSV {csv_field} must be blank for {code}")
            else:
                require(
                    math.isclose(float(row[csv_field]), float(expected), abs_tol=1e-9),
                    f"CSV {csv_field} differs for {code}",
                )
        require(
            row["nasdaq100_observations"] == str(fit.get("observations") or ""),
            f"CSV Nasdaq-100 observations differ for {code}",
        )
        require(
            row["nasdaq100_start_date"] == str(fit.get("start_date") or "")
            and row["nasdaq100_end_date"] == str(fit.get("end_date") or ""),
            f"CSV Nasdaq-100 dates differ for {code}",
        )
        fit_2y = record.get("nasdaq100_fit_2y") or {}
        for csv_field, fit_field in (
            ("nasdaq100_2y_correlation", "correlation"),
            ("nasdaq100_2y_beta", "beta"),
            ("nasdaq100_2y_tracking_error_pct", "tracking_error_pct"),
        ):
            expected = fit_2y.get(fit_field)
            require(
                row[csv_field] == ("" if expected is None else str(expected)),
                f"CSV {csv_field} differs for {code}",
            )
        _validate_common_fit_csv(row, record, code)
        expected_contract_match = record.get("contract_name_match")
        require(
            row["nasdaq100_name_contract_match"]
            == ("" if expected_contract_match is None else str(expected_contract_match)),
            f"CSV contract/name match differs for {code}",
        )
        require(
            row["nasdaq100_name_contract_warning"]
            == str(record.get("contract_name_match_warning") or ""),
            f"CSV contract/name warning differs for {code}",
        )
        for csv_field, fit_field in (
            ("nasdaq100_2y_observations", "observations"),
            ("nasdaq100_2y_start_date", "start_date"),
            ("nasdaq100_2y_end_date", "end_date"),
        ):
            require(
                row[csv_field] == str(fit_2y.get(fit_field) or ""),
                f"CSV {csv_field} differs for {code}",
            )
        exposure = record.get("us_equity_exposure")
        if exposure is not None:
            for field in ("confirmed_pct", "possible_pct"):
                require(
                    math.isclose(
                        float(row[f"us_equity_{field}"]),
                        float(exposure[field]),
                        abs_tol=1e-9,
                    ),
                    f"CSV US exposure {field} differs for {code}",
                )
        else:
            require(row["us_equity_confirmed_pct"] == "", f"CSV US exposure differs for {code}")
            require(row["us_equity_possible_pct"] == "", f"CSV US exposure differs for {code}")
        if record["ranking_list"] == "global_supplement":
            require(
                row["three_year_annualized_return_pct"]
                == str(record["three_year_annualized_return_pct"]),
                f"CSV annualized return differs for {code}",
            )
            expected_ratio = "" if record["return_drawdown_ratio"] is None else str(record["return_drawdown_ratio"])
            require(row["return_drawdown_ratio"] == expected_ratio, f"CSV return/drawdown ratio differs for {code}")
        require(row["direct_limit"] == format_limit(record["direct_limit"]), f"CSV direct limit differs for {code}")
        require(row["agency_limit"] == format_limit(record["agency_limit"]), f"CSV agency limit differs for {code}")
        require(
            row["quota_source_urls"] == " | ".join(record["quota_source_urls"]),
            f"CSV quota sources differ for {code}",
        )


def validate_markdown(path: Path, payload: dict[str, Any], records: list[dict[str, Any]]) -> None:
    require(path.is_file(), f"Missing artifact: {path}")
    document = path.read_text(encoding="utf-8")
    require(f"- 更新日期：{payload['run_date']}" in document, "Markdown run date differs")
    for warning in payload["warnings"]:
        if warning.startswith("三年边界容差 "):
            require(warning in document, "Markdown boundary warning is missing")
    parsed: list[tuple[int, str, str, str]] = []
    for line in document.splitlines():
        match = MARKDOWN_ROW_RE.match(line)
        if match:
            parsed.append((int(match.group(1)), match.group(3), match.group(2), line))
    require(len(parsed) == len(records), "Markdown record count differs from JSON")
    for record, (rank, code, name, line) in zip(records, parsed):
        require(rank == record["rank"] and code == record["code"], f"Markdown order differs at {record['code']}")
        require(name == record["name"], f"Markdown name differs for {record['code']}")
        require(mailer.format_three_year_boundary(record) in document, f"Markdown boundary differs for {code}")
        expected_values = [
            mailer.format_routing_reason(record["routing_reason"]),
            format_percentage(record["three_year_return_pct"], show_sign=True),
            mailer.format_optional_percentage(record["five_year_return_pct"]),
            mailer.format_optional_percentage(record["ten_year_return_pct"]),
            mailer.format_holding_cost(record["holding_cost"]),
            format_limit(record["direct_limit"]),
            format_limit(record["agency_limit"]),
            format_percentage(record["us_equity_exposure"]["confirmed_pct"]),
            format_percentage(record["us_equity_exposure"]["possible_pct"]),
        ]
        if record["ranking_list"] == "us_main":
            expected_values.extend(
                (
                    format_correlation(record["nasdaq100_fit"]["correlation"]),
                    format_beta(record["nasdaq100_fit"]["beta"]),
                )
            )
        else:
            ratio = "∞" if record["return_drawdown_ratio"] is None else f"{record['return_drawdown_ratio']:.2f}"
            expected_values.extend(
                (
                    record["contract_benchmark"]["benchmark_name"],
                    ratio,
                )
            )
        require(all(value in line for value in expected_values), f"Markdown metrics differ for {record['code']}")
        require(
            record["contract_benchmark"]["benchmark_name"] in document,
            f"Markdown contract benchmark differs for {record['code']}",
        )
