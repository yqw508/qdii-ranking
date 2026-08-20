#!/usr/bin/env python3
"""Validate generated QDII ranking artifacts and an optional deployment."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


SHANGHAI_TZ = timezone(timedelta(hours=8))
EXPECTED_FILTERS = {
    "top": 10,
    "min_scale_billion_cny": 3.0,
    "min_age_years": 3,
    "min_three_year_return_pct": 50.0,
    "min_us_equity_pct": 50.0,
}
EXPECTED_EXCLUDE_KEYWORDS = {"债", "亚洲", "中国", "港"}
MARKDOWN_ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|\s*\[(.+)\s+(\d{6})\]\([^)]+\)\s*\|"
)


class ValidationError(RuntimeError):
    """Raised when an artifact cannot be published safely."""


class RankingHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.codes: list[str] = []
        self.blocks: dict[str, str] = {}
        self.all_text: list[str] = []
        self._current_code: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "details" and "fund-item" in classes:
            code = attributes.get("data-code")
            if not code or self._current_code is not None:
                raise ValidationError("HTML contains an invalid nested fund record")
            self._current_code = code
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "details" and self._current_code is not None:
            text = " ".join(" ".join(self._current_text).split())
            self.codes.append(self._current_code)
            self.blocks[self._current_code] = text
            self._current_code = None
            self._current_text = []

    def handle_data(self, data: str) -> None:
        self.all_text.append(data)
        if self._current_code is not None:
            self._current_text.append(data)


def current_shanghai_date() -> str:
    return datetime.now(SHANGHAI_TZ).date().isoformat()


def parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def as_number(value: Any, label: str) -> float:
    require(isinstance(value, (int, float)), f"{label} must be numeric")
    number = float(value)
    require(math.isfinite(number), f"{label} must be finite")
    return number


def format_percentage(value: Any, show_sign: bool = False) -> str:
    number = as_number(value, "percentage")
    return f"{number:+.2f}%" if show_sign else f"{number:.2f}%"


def format_limit(limit: dict[str, Any]) -> str:
    status = limit.get("status")
    if status == "unlimited":
        return "正常开放"
    if status == "suspended":
        return "暂停申购"
    amount = limit.get("amount_cny")
    if status == "unknown" or amount is None:
        return "待核实"
    amount = int(amount)
    if amount >= 10000 and amount % 10000 == 0:
        return f"{amount // 10000:,}万元"
    return f"{amount:,}元"


def is_reportable_warning(warning: str) -> bool:
    if warning.startswith("跳过未完整披露的持有人报告期"):
        return True
    if "无法按 " in warning and "仓位仅计入可能上限" in warning:
        return True
    if "前十大基金之外尚有" in warning and "仅计入可能上限" in warning:
        return True
    if "美股占比区间" in warning and "按保守规则排除" in warning:
        return True
    return False


def classify_warnings(warnings: Any) -> tuple[list[str], list[str]]:
    require(isinstance(warnings, list), "warnings must be a list")
    require(
        all(isinstance(warning, str) and warning.strip() for warning in warnings),
        "warnings must contain non-empty strings",
    )
    reportable = [warning for warning in warnings if is_reportable_warning(warning)]
    blocking = [warning for warning in warnings if not is_reportable_warning(warning)]
    return reportable, blocking


def load_payload(path: Path) -> dict[str, Any]:
    require(path.is_file(), f"Missing artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Could not read {path}: {exc}") from exc
    require(isinstance(payload, dict), "latest.json must contain an object")
    return payload


def validate_filters(filters: Any) -> None:
    require(isinstance(filters, dict), "filters must be an object")
    for key, expected in EXPECTED_FILTERS.items():
        require(filters.get(key) == expected, f"Unexpected filter {key}: {filters.get(key)!r}")
    require(
        set(filters.get("exclude_keywords", [])) == EXPECTED_EXCLUDE_KEYWORDS,
        "Fund-name exclusion keywords changed",
    )
    require(filters.get("purchasable_only") is True, "purchasable_only must be true")
    require(filters.get("full_scan_completed") is True, "Full scan was not completed")
    require(
        filters.get("performance_candidates_scanned")
        == filters.get("base_candidates_total"),
        "Performance scan count does not match the base candidate count",
    )
    require(
        filters.get("us_equity_candidates_scanned")
        == filters.get("performance_qualified_count"),
        "US-equity scan count does not match the performance-qualified count",
    )
    require(
        isinstance(filters.get("us_equity_qualified_count"), int)
        and filters["us_equity_qualified_count"] >= EXPECTED_FILTERS["top"],
        "Fewer than ten funds qualified for US-equity exposure",
    )


def validate_limit(limit: Any, code: str, channel: str) -> None:
    require(isinstance(limit, dict), f"{code} {channel} limit must be an object")
    status = limit.get("status")
    require(
        status in {"limited", "unlimited", "suspended"},
        f"{code} {channel} limit is unresolved",
    )
    if status == "limited":
        amount = limit.get("amount_cny")
        require(
            isinstance(amount, int) and amount > 0,
            f"{code} {channel} limit has no positive amount",
        )
        require(bool(limit.get("source_url")), f"{code} {channel} limit has no source")


def validate_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records")
    require(isinstance(records, list), "records must be a list")
    require(len(records) == EXPECTED_FILTERS["top"], "Ranking must contain exactly ten records")
    run_date = parse_date(payload["run_date"])
    cutoff = years_ago(run_date, EXPECTED_FILTERS["min_age_years"])
    codes: list[str] = []

    for expected_rank, record in enumerate(records, start=1):
        require(isinstance(record, dict), f"Record {expected_rank} must be an object")
        code = record.get("code")
        require(
            isinstance(code, str) and re.fullmatch(r"\d{6}", code) is not None,
            f"Record {expected_rank} has an invalid fund code",
        )
        require(record.get("rank") == expected_rank, f"{code} has a non-contiguous rank")
        codes.append(code)
        name = record.get("name")
        require(isinstance(name, str) and name, f"{code} has no fund name")
        require(
            not any(keyword in name for keyword in EXPECTED_EXCLUDE_KEYWORDS),
            f"{code} contains an excluded fund-name keyword",
        )
        require(
            record.get("fund_type") not in {"指数型-海外股票", "QDII-纯债"},
            f"{code} has an excluded fund type",
        )
        require(
            record.get("purchase_status") in {"open", "limited"},
            f"{code} is not currently purchasable",
        )
        require(
            as_number(record.get("scale_billion_cny"), f"{code} scale")
            > EXPECTED_FILTERS["min_scale_billion_cny"],
            f"{code} does not meet the strict scale threshold",
        )
        require(
            parse_date(record["inception_date"]) < cutoff,
            f"{code} is not strictly older than three years",
        )
        require(
            as_number(record.get("three_year_return_pct"), f"{code} three-year return")
            >= EXPECTED_FILTERS["min_three_year_return_pct"],
            f"{code} does not meet the three-year return threshold",
        )
        for field in (
            "one_year_return_pct",
            "one_year_max_drawdown_pct",
            "three_year_max_drawdown_pct",
        ):
            as_number(record.get(field), f"{code} {field}")

        exposure = record.get("us_equity_exposure")
        require(isinstance(exposure, dict), f"{code} has no US-equity exposure")
        confirmed = as_number(exposure.get("confirmed_pct"), f"{code} confirmed exposure")
        possible = as_number(exposure.get("possible_pct"), f"{code} possible exposure")
        unresolved = as_number(exposure.get("unresolved_pct"), f"{code} unresolved exposure")
        require(
            confirmed >= EXPECTED_FILTERS["min_us_equity_pct"],
            f"{code} does not meet the confirmed US-equity threshold",
        )
        require(confirmed <= possible <= 100, f"{code} has an invalid exposure interval")
        require(unresolved >= 0, f"{code} has a negative unresolved exposure")
        require(exposure.get("status") == "qualified", f"{code} exposure is not qualified")
        require(bool(exposure.get("source_url")), f"{code} exposure has no source")

        require(record.get("quota_status") != "unknown", f"{code} quota is unresolved")
        require(
            record.get("quota_confidence") in {"medium", "high"},
            f"{code} quota confidence is too low",
        )
        validate_limit(record.get("direct_limit"), code, "direct")
        validate_limit(record.get("agency_limit"), code, "agency")
        quota_sources = record.get("quota_source_urls")
        require(
            isinstance(quota_sources, list)
            and all(isinstance(source, str) and source for source in quota_sources),
            f"{code} quota source list is invalid",
        )
        if record.get("quota_status") == "limited":
            require(quota_sources, f"{code} limited quota has no announcement source")
        for channel in ("direct_limit", "agency_limit"):
            source_url = record[channel].get("source_url")
            if source_url and source_url != record.get("fund_page_url"):
                require(
                    source_url in quota_sources,
                    f"{code} {channel} source is absent from quota_source_urls",
                )

    require(len(codes) == len(set(codes)), "Ranking contains duplicate fund codes")
    expected_order = sorted(
        records,
        key=lambda item: (
            -float(item["us_equity_exposure"]["confirmed_pct"]),
            -float(item["institution_holding_ratio_pct"]),
            -float(item["three_year_return_pct"]),
            item["code"],
        ),
    )
    require(
        [item["code"] for item in records] == [item["code"] for item in expected_order],
        "Ranking order does not match the established sort rule",
    )
    return records


def validate_csv(path: Path, records: list[dict[str, Any]]) -> None:
    require(path.is_file(), f"Missing artifact: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except OSError as exc:
        raise ValidationError(f"Could not read {path}: {exc}") from exc
    require(len(rows) == len(records), "CSV record count differs from JSON")
    numeric_fields = {
        "institution_holding_ratio_pct": "institution_holding_ratio_pct",
        "scale_billion_cny": "scale_billion_cny",
        "one_year_return_pct": "one_year_return_pct",
        "one_year_max_drawdown_pct": "one_year_max_drawdown_pct",
        "three_year_return_pct": "three_year_return_pct",
        "three_year_max_drawdown_pct": "three_year_max_drawdown_pct",
    }
    identical_fields = (
        "holder_report_date",
        "inception_date",
        "scale_report_date",
        "one_year_performance_start_date",
        "one_year_performance_end_date",
        "three_year_performance_start_date",
        "three_year_performance_end_date",
        "purchase_status",
        "share_class_rule",
        "channel_rule",
        "quota_confidence",
        "fund_page_url",
        "performance_source_url",
    )
    for record, row in zip(records, rows):
        code = record["code"]
        require(row.get("code") == code, f"CSV order differs at {code}")
        require(row.get("rank") == str(record["rank"]), f"CSV rank differs for {code}")
        require(row.get("name") == record["name"], f"CSV name differs for {code}")
        for field in identical_fields:
            require(row.get(field) == str(record[field]), f"CSV {field} differs for {code}")
        for csv_field, json_field in numeric_fields.items():
            require(
                math.isclose(float(row[csv_field]), float(record[json_field]), abs_tol=1e-9),
                f"CSV {csv_field} differs for {code}",
            )
        exposure = record["us_equity_exposure"]
        require(
            math.isclose(
                float(row["us_equity_confirmed_pct"]),
                float(exposure["confirmed_pct"]),
                abs_tol=1e-9,
            ),
            f"CSV confirmed exposure differs for {code}",
        )
        require(
            math.isclose(
                float(row["us_equity_possible_pct"]),
                float(exposure["possible_pct"]),
                abs_tol=1e-9,
            ),
            f"CSV possible exposure differs for {code}",
        )
        require(row["us_equity_status"] == exposure["status"], f"CSV exposure status differs for {code}")
        require(
            row["us_equity_report_date"] == exposure["report_date"],
            f"CSV exposure report date differs for {code}",
        )
        require(
            row["us_equity_source_url"] == exposure["source_url"],
            f"CSV exposure source differs for {code}",
        )
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
    parsed: list[tuple[int, str, str, str]] = []
    for line in document.splitlines():
        match = MARKDOWN_ROW_RE.match(line)
        if match:
            parsed.append((int(match.group(1)), match.group(3), match.group(2), line))
    require(len(parsed) == len(records), "Markdown record count differs from JSON")
    for record, (rank, code, name, line) in zip(records, parsed):
        require(rank == record["rank"] and code == record["code"], f"Markdown order differs at {record['code']}")
        require(name == record["name"], f"Markdown name differs for {record['code']}")
        expected_values = (
            format_percentage(record["institution_holding_ratio_pct"]),
            f"{float(record['scale_billion_cny']):.2f}亿元",
            format_percentage(record["one_year_return_pct"], show_sign=True),
            format_percentage(record["one_year_max_drawdown_pct"]),
            format_percentage(record["three_year_return_pct"], show_sign=True),
            format_percentage(record["three_year_max_drawdown_pct"]),
            format_percentage(record["us_equity_exposure"]["confirmed_pct"]),
            format_limit(record["direct_limit"]),
            format_limit(record["agency_limit"]),
        )
        require(all(value in line for value in expected_values), f"Markdown metrics differ for {record['code']}")


def parse_html(document: str) -> RankingHtmlParser:
    parser = RankingHtmlParser()
    try:
        parser.feed(document)
        parser.close()
    except (ValidationError, ValueError) as exc:
        raise ValidationError(f"Could not parse ranking HTML: {exc}") from exc
    return parser


def validate_html_document(
    document: str, payload: dict[str, Any], records: list[dict[str, Any]], label: str
) -> None:
    parser = parse_html(document)
    require(payload["run_date"] in " ".join(parser.all_text), f"{label} run date differs")
    expected_codes = [record["code"] for record in records]
    require(parser.codes == expected_codes, f"{label} fund order differs from JSON")
    for record in records:
        code = record["code"]
        block = parser.blocks[code]
        expected_values = (
            record["name"],
            format_percentage(record["institution_holding_ratio_pct"]),
            f"{float(record['scale_billion_cny']):.2f} 亿元",
            format_percentage(record["one_year_return_pct"], show_sign=True),
            format_percentage(record["one_year_max_drawdown_pct"]),
            format_percentage(record["us_equity_exposure"]["confirmed_pct"]),
            format_percentage(record["three_year_return_pct"], show_sign=True),
            format_percentage(record["three_year_max_drawdown_pct"]),
            format_limit(record["direct_limit"]),
            format_limit(record["agency_limit"]),
        )
        require(all(value in block for value in expected_values), f"{label} metrics differ for {code}")


def validate_local_artifacts(
    output_dir: Path, publish_dir: Path, expected_date: str
) -> tuple[dict[str, Any], list[str]]:
    payload = load_payload(output_dir / "latest.json")
    require(payload.get("schema_version") == 6, "Unexpected JSON schema version")
    require(payload.get("run_date") == expected_date, "Ranking date is not today's Shanghai date")
    require(
        str(payload.get("generated_at", ""))[:10] == expected_date,
        "Generation timestamp does not match the ranking date",
    )
    validate_filters(payload.get("filters"))
    records = validate_records(payload)
    reportable, blocking = classify_warnings(payload.get("warnings"))
    require(not blocking, "Blocking warnings: " + " | ".join(blocking))
    validate_csv(output_dir / "latest.csv", records)
    validate_markdown(output_dir / "latest.md", payload, records)

    generated_html_path = output_dir / "latest.html"
    published_html_path = publish_dir / "index.html"
    require(generated_html_path.is_file(), f"Missing artifact: {generated_html_path}")
    require(published_html_path.is_file(), f"Missing artifact: {published_html_path}")
    generated = generated_html_path.read_bytes()
    published = published_html_path.read_bytes()
    require(generated == published, "Generated and published HTML are not byte-identical")
    validate_html_document(generated.decode("utf-8"), payload, records, "HTML")
    return payload, reportable


def validate_deployment(
    url: str,
    payload: dict[str, Any],
    attempts: int,
    delay_seconds: float,
) -> None:
    records = payload["records"]
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "qdii-ranking-deployment-validator/1.0"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                require(response.status == 200, f"Deployment returned HTTP {response.status}")
                document = response.read().decode("utf-8")
            validate_html_document(document, payload, records, "Deployed HTML")
            return
        except (
            OSError,
            UnicodeDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            ValidationError,
        ) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)
    raise ValidationError(f"Deployment verification failed after {attempts} attempts: {last_error}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("output/qdii-ranking"))
    parser.add_argument("--publish-dir", type=Path, default=Path("public"))
    parser.add_argument("--expected-date", default=current_shanghai_date())
    parser.add_argument("--deployed-url")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--delay-seconds", type=float, default=0)
    args = parser.parse_args(argv)
    if args.attempts <= 0:
        parser.error("--attempts must be positive")
    if args.delay_seconds < 0:
        parser.error("--delay-seconds must be non-negative")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload, warnings = validate_local_artifacts(
            args.output_dir.resolve(), args.publish_dir.resolve(), args.expected_date
        )
        for warning in warnings:
            print(f"REPORTABLE WARNING: {warning}", file=sys.stderr)
        if args.deployed_url:
            validate_deployment(
                args.deployed_url, payload, args.attempts, args.delay_seconds
            )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Validated {len(payload['records'])} records for {payload['run_date']}"
        + (" and the deployed page" if args.deployed_url else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
