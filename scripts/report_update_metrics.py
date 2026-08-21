#!/usr/bin/env python3
"""Compare one ranking run with the versioned pre-optimization baseline."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def reduction(before: float, after: float) -> float:
    return 0.0 if before <= 0 else (before - after) / before * 100


def format_seconds(value: float) -> str:
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def build_report(
    baseline: dict[str, Any], current: dict[str, Any], end_to_end: float | None
) -> str:
    summary = baseline["summary"]
    targets = baseline["targets"]
    refresh_before = float(summary["refresh_seconds"]["median"])
    refresh_after = float(current["refresh_seconds"])
    refresh_reduction = reduction(refresh_before, refresh_after)
    announcement = (current.get("http") or {}).get("announcement_index") or {}
    announcement_calls = int(announcement.get("calls") or 0)
    announcement_before = int(summary["announcement_index_calls_minimum"])
    announcement_reduction = reduction(announcement_before, announcement_calls)
    pdf = ((current.get("cache") or {}).get("announcement_pdfs") or {})
    pdf_extractions = int(pdf.get("text_extractions") or 0)
    pdf_before = int(summary["pdf_text_extractions"])
    pdf_reduction = reduction(pdf_before, pdf_extractions)

    rows = [
        (
            "Ranking refresh",
            format_seconds(refresh_before),
            format_seconds(refresh_after),
            refresh_reduction,
            refresh_after <= float(targets["refresh_median_seconds_max"]),
        ),
        (
            "Announcement index calls",
            str(announcement_before),
            str(announcement_calls),
            announcement_reduction,
            announcement_reduction
            >= float(targets["announcement_index_reduction_pct_min"]),
        ),
        (
            "PDF text extractions",
            str(pdf_before),
            str(pdf_extractions),
            pdf_reduction,
            pdf_extractions == 0,
        ),
    ]
    if end_to_end is not None:
        end_before = float(summary["end_to_end_seconds"]["median"])
        end_reduction = reduction(end_before, end_to_end)
        rows.insert(
            1,
            (
                "Job start to email",
                format_seconds(end_before),
                format_seconds(end_to_end),
                end_reduction,
                end_to_end <= float(targets["end_to_end_median_seconds_max"]),
            ),
        )

    lines = [
        "## QDII update performance",
        "",
        "| Metric | Pre-change median/count | Current | Reduction | Target |",
        "|---|---:|---:|---:|:---:|",
    ]
    for label, before, after, reduced, passed in rows:
        lines.append(
            f"| {label} | {before} | {after} | {reduced:.1f}% | "
            f"{'PASS' if passed else 'WARN'} |"
        )
    lines.extend(("", "Performance warnings do not bypass ranking validation."))
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--job-started-epoch", type=float)
    parser.add_argument("--summary", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    end_to_end = None
    if args.job_started_epoch is not None:
        end_to_end = max(0.0, time.time() - args.job_started_epoch)
    report = build_report(read_json(args.baseline), read_json(args.current), end_to_end)
    summary_path = args.summary
    if summary_path is None and os.environ.get("GITHUB_STEP_SUMMARY"):
        summary_path = Path(os.environ["GITHUB_STEP_SUMMARY"])
    if summary_path is not None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("a", encoding="utf-8") as handle:
            handle.write(report)
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
