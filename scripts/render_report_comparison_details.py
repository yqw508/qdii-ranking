#!/usr/bin/env python3
"""Regenerate curated comparisons and their index without fetching or reranking."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from qdii_ranking.report_comparison_details import comparison_entry, load_comparison_details, render_comparison_detail
from qdii_ranking.report_comparisons import load_report_comparison_index, render_report_comparison_list, validate_report_comparison_index


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=root / "references/report-comparisons/2026-q2-vs-q3.json")
    parser.add_argument("--publish-dir", type=Path, default=root / "public")
    args = parser.parse_args()
    data = load_comparison_details(args.data)
    index_path = args.publish_dir / "report-comparisons/index.json"
    index = load_report_comparison_index(index_path)
    for code, fund in data["funds"].items():
        entry = comparison_entry(data, code)
        existing = index["funds"].get(code, {}).get("comparisons", [])
        index["funds"][code] = {"name": fund["name"], "list_url": f"report-comparisons/{code}/",
                                 "comparisons": [entry] + [c for c in existing if c["id"] != entry["id"]]}
    index = validate_report_comparison_index(index)
    # Touch only funds covered by the curated input; earlier comparisons stay byte-identical.
    for code in data["funds"]:
        entry = comparison_entry(data, code)
        path = args.publish_dir / entry["detail_url"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_comparison_detail(data, code), encoding="utf-8")
        render_report_comparison_list(path.parent / "index.html", index["funds"][code])
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Rendered {len(data['funds'])} paired report comparisons; no ranking refresh performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
