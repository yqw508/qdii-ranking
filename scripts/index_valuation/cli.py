"""CLI for refreshing valuation artifacts."""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .common import (
    DEFAULT_CACHE_DIR,
    DEFAULT_CATALOG,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PAGE_SCRIPT,
    DEFAULT_PUBLISH_DIR,
    HttpClient,
    PERFORMANCE_TARGETS,
    SCHEMA_VERSION,
    ValuationError,
    atomic_write_json,
    atomic_write_text,
    current_shanghai_time,
)
from .cache import cache_fingerprint, load_catalog, load_manifest, load_source_caches
from .pipeline import build_payload
from .renderer import render_html

def build_performance_metrics(
    *, payload: dict[str, Any], request_metrics: dict[str, Any], total_seconds: float
) -> dict[str, Any]:
    total_bytes = sum(int(item.get("bytes", 0)) for item in request_metrics["sources"].values())
    startup = payload["cache"]["startup"]
    warnings: list[str] = []
    seconds_target = PERFORMANCE_TARGETS[f"{startup}_seconds"]
    if total_seconds >= seconds_target:
        warnings.append(
            f"{startup} startup took {total_seconds:.3f}s; target is <{seconds_target:g}s"
        )
    if startup == "hot" and total_bytes >= PERFORMANCE_TARGETS["hot_download_bytes"]:
        warnings.append(
            f"hot startup downloaded {total_bytes} bytes; target is <{PERFORMANCE_TARGETS['hot_download_bytes']}"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "generated_at": payload["generated_at"],
        "asset_status": payload["status"],
        "total_seconds": round(total_seconds, 3),
        "request_wall_seconds": request_metrics["request_wall_seconds"],
        "parse_seconds": request_metrics["parse_seconds"],
        "total_download_bytes": total_bytes,
        "startup": startup,
        "refresh_mode": payload["cache"]["refresh_mode"],
        "cache_hit": payload["cache"]["hit"],
        "fallback": payload["cache"]["fallback"],
        "sources": request_metrics["sources"],
        "targets": PERFORMANCE_TARGETS,
        "warnings": warnings,
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--publish-dir", type=Path, default=DEFAULT_PUBLISH_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--page-script", type=Path, default=DEFAULT_PAGE_SCRIPT)
    parser.add_argument("--as-of", help="Evaluation date in YYYY-MM-DD format")
    args = parser.parse_args(argv)
    if args.as_of:
        try:
            datetime.strptime(args.as_of, "%Y-%m-%d")
        except ValueError:
            parser.error("--as-of must use YYYY-MM-DD")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.perf_counter()
    output_dir = args.output_dir.resolve()
    publish_dir = args.publish_dir.resolve()
    cache_dir = args.cache_dir.resolve()
    try:
        as_of = (
            datetime.strptime(args.as_of, "%Y-%m-%d").date()
            if args.as_of
            else current_shanghai_time().date()
        )
        now = current_shanghai_time()
        catalog, catalog_hash = load_catalog(args.catalog.resolve())
        fingerprint = cache_fingerprint(catalog, catalog_hash)
        caches, cache_states = load_source_caches(cache_dir, catalog, catalog_hash, as_of)
        manifest, manifest_state = load_manifest(cache_dir, fingerprint)
        payload, new_caches, new_manifest, request_metrics = build_payload(
            as_of=as_of,
            now=now,
            catalog=catalog,
            catalog_hash=catalog_hash,
            caches=caches,
            cache_states=cache_states,
            manifest=manifest,
            manifest_state=manifest_state,
            client=HttpClient(),
        )
        if payload["status"] == "unavailable":
            raise ValuationError("All valuation assets are unavailable")
        script = args.page_script.resolve().read_text(encoding="utf-8")
        document = render_html(payload, script)
        for source_id, cached in new_caches.items():
            atomic_write_json(cache_dir / f"{source_id}.json", cached)
        atomic_write_json(cache_dir / "manifest.json", new_manifest)
        atomic_write_json(output_dir / "latest.json", payload)
        atomic_write_text(output_dir / "latest.html", document)
        atomic_write_text(publish_dir / "valuation" / "index.html", document)
        metrics = build_performance_metrics(
            payload=payload,
            request_metrics=request_metrics,
            total_seconds=time.perf_counter() - started,
        )
        atomic_write_json(output_dir / "run-metrics.json", metrics)
        for warning in metrics["warnings"]:
            print(f"::warning title=Index valuation performance::{warning}", file=sys.stderr)
        for warning in payload["warnings"]:
            print(f"SOURCE WARNING: {warning}", file=sys.stderr)
    except (OSError, ValueError, ValuationError) as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "status": "failure",
            "generated_at": current_shanghai_time().isoformat(),
            "total_seconds": round(time.perf_counter() - started, 3),
            "error": str(exc),
        }
        try:
            atomic_write_json(output_dir / "run-metrics.json", failure)
        except OSError:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    available = sum(asset["status"] != "unavailable" for asset in payload["assets"])
    print(
        f"Generated {available}/{len(payload['assets'])} valuation assets "
        f"with status {payload['status']} in {metrics['total_seconds']:.3f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
