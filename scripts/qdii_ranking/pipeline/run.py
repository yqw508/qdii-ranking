"""Top-level coordinator for a complete ranking refresh."""

from __future__ import annotations

import argparse
from typing import Any

from ..runtime import HttpClient, RunMetrics, current_shanghai_date, parse_date
from ..services.context import ExclusionCollector
from ..audit import CandidateAudit, load_baseline
from ..freshness import load_calendars, require_fresh, revalidate_nav_suspensions
from ..services.ranking import (
    discover_candidates,
    initialize_resources,
    route_and_rank,
    scan_documents,
    scan_exchange_premium,
    scan_nasdaq_otc,
    scan_performance,
)
from .payload import assemble_payload


def build_payload(
    args: argparse.Namespace,
    client: HttpClient,
    run_metrics: RunMetrics | None = None,
) -> dict[str, Any]:
    metrics = run_metrics or RunMetrics()
    as_of = parse_date(args.as_of) if args.as_of else current_shanghai_date()
    audit = CandidateAudit(as_of, args.output_dir / "candidate-audit.json")
    args._audit = audit
    try:
        audit.baseline = load_baseline(as_of)
        for record in audit.baseline["records"]:
            audit.register(record)
        load_calendars()
        revalidate_nav_suspensions(client, as_of)
        return _build_payload(args, client, metrics, as_of, audit)
    except Exception as exc:
        audit.save("failure", exc)
        raise


def _build_payload(args, client, metrics, as_of, audit):
    exclusions = ExclusionCollector(audit=audit)
    audit.stage = "discovery"
    discovery = discover_candidates(args, client, as_of, metrics)
    audit.stage = "benchmark"
    resources, resource_warnings = initialize_resources(args, client, as_of, metrics)
    audit.stage = "performance"
    performance = scan_performance(
        args, client, as_of, metrics, resources, discovery.preliminary
    )
    for code, failures in performance.rejections.items():
        for reason, label in failures:
            exclusions.add(reason, label, code)
    audit.stage = "documents"
    documents = scan_documents(
        args, client, as_of, metrics, resources, performance.qualified
    )
    audit.stage = "ranking"
    ranking = route_and_rank(
        args,
        documents.classified,
        discovery.selected_period.report_date,
        exclusions,
    )
    audit.stage = "nasdaq100_otc"
    nasdaq = scan_nasdaq_otc(client, discovery, as_of, resources, metrics)
    audit.stage = "premium"
    premium = scan_exchange_premium(
        args, client, discovery.metadata, as_of, resources, metrics
    )
    warnings = [
        *discovery.warnings,
        *resource_warnings,
        *performance.warnings,
        *documents.warnings,
        *ranking.warnings,
        *nasdaq.warnings,
        *premium.warnings,
    ]
    payload = assemble_payload(
        args,
        as_of,
        discovery,
        resources,
        performance,
        documents,
        ranking,
        nasdaq,
        premium,
        exclusions,
        warnings,
    )

    payload["nasdaq100_otc"]["freshness_policy_version"] = 1
    for record in payload["nasdaq100_otc"]["records"]:
        if record.get("common_period_return_pct") is not None:
            record["common_end_freshness"] = require_fresh(parse_date(record["common_period_performance_end_date"]), as_of, code=record["code"])
    audit.stage = "continuity"
    audit.finish(payload)
    return payload
