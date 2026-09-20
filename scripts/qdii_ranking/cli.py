"""Command-line interface and atomic ranking publication."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterable

from .artifacts import write_csv, write_html, write_json, write_markdown
from .config import (
    DEFAULT_CONTRACT_BENCHMARK_CATALOG,
    DEFAULT_EXCLUDE_KEYWORDS,
    DEFAULT_MIN_DIRECT_LIMIT_CNY,
    DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
    DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
    DEFAULT_MIN_THREE_YEAR_RETURN_PCT,
    DEFAULT_US_EQUITY_CATALOG,
)
from .errors import DataError
from .pipeline import build_payload
from .runtime import HttpClient, RunMetrics, current_shanghai_date, parse_date


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=10, help="Maximum result count")
    parser.add_argument(
        "--min-scale",
        type=float,
        default=None,
        help="Optional strict minimum scale in CNY 100m; omitted by default",
    )
    parser.add_argument(
        "--min-age-years",
        type=int,
        default=3,
        help="Require inception strictly earlier than this many years before --as-of",
    )
    parser.add_argument(
        "--min-three-year-return-pct",
        type=float,
        default=DEFAULT_MIN_THREE_YEAR_RETURN_PCT,
        help="Minimum trailing three-year adjusted return percentage",
    )
    parser.add_argument(
        "--min-five-year-return-pct",
        type=float,
        default=DEFAULT_MIN_FIVE_YEAR_RETURN_PCT,
        help="Minimum five-year adjusted return when a complete five-year window exists",
    )
    parser.add_argument(
        "--min-ten-year-return-pct",
        type=float,
        default=DEFAULT_MIN_TEN_YEAR_RETURN_PCT,
        help="Minimum ten-year adjusted return when a complete ten-year window exists",
    )
    parser.add_argument(
        "--min-us-equity-pct",
        type=float,
        default=50.0,
        help="Minimum confirmed US-equity exposure percentage",
    )
    parser.add_argument(
        "--min-direct-limit-cny",
        type=int,
        default=DEFAULT_MIN_DIRECT_LIMIT_CNY,
        help="Inclusive minimum manager direct-sale daily limit in CNY",
    )
    parser.add_argument(
        "--us-main-exclude-keywords",
        "--exclude-keywords",
        dest="us_main_exclude_keywords",
        nargs="*",
        default=DEFAULT_EXCLUDE_KEYWORDS,
        help="Fund-name keywords routed away from the US main list",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / "output" / "qdii-ranking",
        help="Output directory",
    )
    parser.add_argument(
        "--publish-dir",
        type=Path,
        default=Path.cwd() / "public",
        help="Static-site directory that receives index.html",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="Persistent cache directory (defaults to <output-dir>/cache)",
    )
    parser.add_argument(
        "--us-equity-catalog",
        type=Path,
        default=DEFAULT_US_EQUITY_CATALOG,
        help="Underlying instrument classification catalog",
    )
    parser.add_argument(
        "--contract-benchmark-catalog",
        type=Path,
        default=DEFAULT_CONTRACT_BENCHMARK_CATALOG,
        help="Contract benchmark classification catalog",
    )
    parser.add_argument(
        "--us-equity-etf-catalog",
        type=Path,
        default=None,
        help="Optional legacy static ETF catalog; the default discovers all listed QDII funds",
    )
    parser.add_argument("--as-of", help="Evaluation date in YYYY-MM-DD format")
    parser.add_argument(
        "--allow-partial-holder-period",
        action="store_true",
        help="Use the newest holder period even when coverage is below 95%%",
    )
    args = parser.parse_args(argv)
    if args.top <= 0:
        parser.error("--top must be positive")
    if args.min_scale is not None and args.min_scale < 0:
        parser.error("--min-scale must be non-negative")
    if args.min_age_years < 0:
        parser.error("--min-age-years must be non-negative")
    if args.min_five_year_return_pct < -100:
        parser.error("--min-five-year-return-pct must be at least -100")
    if args.min_ten_year_return_pct < -100:
        parser.error("--min-ten-year-return-pct must be at least -100")
    if not 0 <= args.min_us_equity_pct <= 100:
        parser.error("--min-us-equity-pct must be between 0 and 100")
    if args.min_direct_limit_cny < 0:
        parser.error("--min-direct-limit-cny must be non-negative")
    if args.as_of:
        try:
            parse_date(args.as_of)
        except ValueError:
            parser.error("--as-of must use YYYY-MM-DD")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    metrics = RunMetrics()
    client = HttpClient()
    started = time.perf_counter()
    output_dir = args.output_dir.resolve()
    publish_dir = args.publish_dir.resolve()
    try:
        payload = build_payload(args, client, metrics)
        with metrics.phase("artifact_rendering"):
            write_json(output_dir / "latest.json", payload)
            write_csv(output_dir / "latest.csv", payload)
            write_markdown(output_dir / "latest.md", payload)
            write_html(output_dir / "latest.html", payload)
            write_html(publish_dir / "index.html", payload)
            write_json(output_dir / "history" / f"{payload['run_date']}.json", payload)
    except (DataError, OSError, ValueError) as exc:
        try:
            write_json(
                output_dir / "run-metrics.json",
                {
                    "schema_version": 1,
                    "status": "failure",
                    "run_date": (
                        parse_date(args.as_of).isoformat()
                        if args.as_of
                        else current_shanghai_date().isoformat()
                    ),
                    "refresh_seconds": round(time.perf_counter() - started, 3),
                    **metrics.snapshot(),
                    "http": client.metrics(),
                    "error": str(exc),
                },
            )
        except (OSError, ValueError):
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    refresh_seconds = round(time.perf_counter() - started, 3)
    run_metrics = {
        "schema_version": 1,
        "status": "success",
        "run_date": payload["run_date"],
        "generated_at": payload.get("generated_at"),
        "refresh_seconds": refresh_seconds,
        **metrics.snapshot(),
        "http": client.metrics(),
        "cache": payload.get("cache", {}),
        "candidate_counts": {
            key: payload.get("filters", {}).get(key)
            for key in (
                "base_candidates_total",
                "performance_candidates_scanned",
                "performance_qualified_count",
                "contract_candidates_scanned",
                "us_equity_candidates_scanned",
                "us_quota_candidates_scanned",
                "global_quota_candidates_scanned",
            )
        },
        "nasdaq100_otc_candidate_count": len(
            payload.get("nasdaq100_otc", {}).get("records", [])
        ),
    }
    write_json(output_dir / "run-metrics.json", run_metrics)
    print(
        f"Wrote {len(payload['records'])} US records and "
        f"{len(payload['global_supplement']['records'])} global records and "
        f"{len(payload.get('nasdaq100_otc', {}).get('records', []))} OTC Nasdaq-100 records to {output_dir} "
        f"and static site to {publish_dir} in {refresh_seconds:.1f}s"
    )
    for warning in payload["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0
