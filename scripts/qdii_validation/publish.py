"""Local artifact and deployment validation."""

import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from qdii_ranking.config import RANKING_SCHEMA_VERSION

from .common import (
    BOUNDARY_WARNING_RE,
    EXPECTED_GLOBAL_RANKING_METHOD,
    ValidationError,
    classify_warnings,
    mailer,
    parse_date,
    require,
)
from .formats import validate_csv, validate_markdown
from .premium import validate_exchange_premium, validate_html_document
from .schema import (
    load_payload,
    validate_benchmark,
    validate_exclusion_summary,
    validate_filters,
    validate_nasdaq100_otc_section,
    validate_records,
)

def validate_email_rendering(payload: dict[str, Any], records: list[dict[str, Any]]) -> None:
    page_url = f"https://example.test/?v={payload['run_date']}"
    plain = mailer.success_plain_text(payload, page_url)
    html_document = mailer.success_html(payload, page_url)
    for document, label in ((plain, "Plain email"), (html_document, "HTML email")):
        for warning in payload["warnings"]:
            if warning.startswith("三年边界容差 "):
                require(warning in document, f"{label} boundary warning is missing")
        positions = [document.find(record["code"]) for record in records]
        require(all(position >= 0 for position in positions), f"{label} is missing a fund code")
        require(positions == sorted(positions), f"{label} fund order differs from JSON")
        for record in records:
            require(
                mailer.format_three_year_boundary(record) in document,
                f"{label} boundary differs for {record['code']}",
            )
            expected = [
                record["contract_benchmark"]["benchmark_name"],
                mailer.format_routing_reason(record["routing_reason"]),
                mailer.format_percentage(record["three_year_return_pct"], show_sign=True),
                mailer.format_optional_percentage(record["five_year_return_pct"]),
                mailer.format_optional_percentage(record["ten_year_return_pct"]),
                mailer.format_holding_cost(record["holding_cost"]),
                mailer.format_percentage(record["us_equity_exposure"]["confirmed_pct"]),
                mailer.format_percentage(record["us_equity_exposure"]["possible_pct"]),
                mailer.format_limit(record["direct_limit"]),
                mailer.format_limit(record["agency_limit"]),
            ]
            if record["ranking_list"] == "us_main":
                expected.extend(
                    (
                        mailer.format_correlation(record["nasdaq100_fit"]["correlation"]),
                        mailer.format_beta(record["nasdaq100_fit"]["beta"]),
                    )
                )
            else:
                expected.append(mailer.format_ratio(record["return_drawdown_ratio"]))
            require(
                all(value in document for value in expected),
                f"{label} metrics differ for {record['code']}",
            )


def validate_local_artifacts(
    output_dir: Path, publish_dir: Path, expected_date: str
) -> tuple[dict[str, Any], list[str]]:
    payload = load_payload(output_dir / "latest.json")
    require(
        payload.get("schema_version") == RANKING_SCHEMA_VERSION,
        "Unexpected JSON schema version",
    )
    require(payload.get("run_date") == expected_date, "Ranking date is not today's Shanghai date")
    require(
        str(payload.get("generated_at", ""))[:10] == expected_date,
        "Generation timestamp does not match the ranking date",
    )
    validate_filters(payload.get("filters"))
    validate_exclusion_summary(payload.get("exclusion_summary"))
    validate_benchmark(payload.get("benchmark"), parse_date(expected_date))
    validate_exchange_premium(payload.get("exchange_premium"), parse_date(expected_date))
    global_section = payload.get("global_supplement")
    require(isinstance(global_section, dict), "global_supplement must be an object")
    require(
        global_section.get("ranking_method") == EXPECTED_GLOBAL_RANKING_METHOD,
        "Global supplement ranking method differs from filters",
    )
    us_records = validate_records(payload, payload.get("records"), "us_main")
    global_records = validate_records(
        payload, global_section.get("records"), "global_supplement"
    )
    nasdaq_records = validate_nasdaq100_otc_section(
        payload.get("nasdaq100_otc"), parse_date(expected_date)
    )
    require(us_records or global_records, "Both ranking lists are empty")
    records = [*us_records, *global_records]
    codes = [record["code"] for record in records]
    require(len(codes) == len(set(codes)), "Fund codes are duplicated across ranking lists")
    reportable, blocking = classify_warnings(payload.get("warnings"), parse_date(expected_date))
    require(not blocking, "Blocking warnings: " + " | ".join(blocking))
    by_code = {record["code"]: record for record in records}
    for warning in reportable:
        match = BOUNDARY_WARNING_RE.fullmatch(warning)
        if match and match["code"] in by_code:
            record = by_code[match["code"]]
            require(
                record["three_year_boundary_shortfall_days"] == int(match["days"])
                and record["inception_date"] == match["inception"]
                and record["three_year_performance_start_date"] == match["start"]
                and record["three_year_performance_end_date"] == match["end"],
                f"{record['code']} boundary warning contradicts record",
            )
    validate_csv(output_dir / "latest.csv", [*records, *nasdaq_records])
    validate_markdown(output_dir / "latest.md", payload, records)
    validate_email_rendering(payload, records)

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
    records = [
        *payload["records"],
        *payload["global_supplement"]["records"],
    ]
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
