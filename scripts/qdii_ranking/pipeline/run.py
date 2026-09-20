"""Top-level coordinator for a complete ranking refresh."""

from __future__ import annotations

import argparse
from typing import Any

from ..runtime import HttpClient, RunMetrics, current_shanghai_date, parse_date
from ..services.context import ExclusionCollector
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
    exclusions = ExclusionCollector()
    discovery = discover_candidates(args, client, as_of, metrics)
    resources, resource_warnings = initialize_resources(args, client, as_of, metrics)
    performance = scan_performance(
        args, client, as_of, metrics, resources, discovery.preliminary
    )
    for code, failures in performance.rejections.items():
        for reason, label in failures:
            exclusions.add(reason, label, code)
    documents = scan_documents(
        args, client, as_of, metrics, resources, performance.qualified
    )
    ranking = route_and_rank(
        args,
        documents.classified,
        discovery.selected_period.report_date,
        exclusions,
    )
    nasdaq = scan_nasdaq_otc(client, discovery, as_of, resources, metrics)
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
    return assemble_payload(
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
