"""CLI for QDII ranking artifact validation."""

import argparse
import sys
from pathlib import Path
from typing import Iterable

from .common import ValidationError, current_shanghai_date
from .publish import validate_deployment, validate_local_artifacts

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
        f"Validated {len(payload['records'])} US records and "
        f"{len(payload['global_supplement']['records'])} global records for "
        f"{payload['run_date']}"
        + (" and the deployed page" if args.deployed_url else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
