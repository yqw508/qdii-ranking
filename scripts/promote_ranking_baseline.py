"""Promote continuity state only after both artifact validators and unit tests pass."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from qdii_ranking.audit import BASELINE, baseline_from_payload, validate_audit
from qdii_ranking.cache.base import write_json_atomic
from qdii_validation.publish import validate_local_artifacts
from validate_index_valuation import validate_local_artifacts as validate_valuation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('output/qdii-ranking'))
    parser.add_argument('--valuation-output-dir', type=Path, default=Path('output/index-valuation'))
    parser.add_argument('--publish-dir', type=Path, default=Path('public'))
    args = parser.parse_args()
    payload = json.loads((args.output_dir / 'latest.json').read_text(encoding='utf-8'))
    validate_local_artifacts(args.output_dir, args.publish_dir, payload['run_date'])
    validate_valuation(args.valuation_output_dir, args.publish_dir, payload['run_date'])
    audit = validate_audit(payload, args.output_dir / 'candidate-audit.json')
    write_json_atomic(BASELINE, baseline_from_payload(payload, audit))
    changes = payload['ranking_changes']
    lines = [f"Ranking {payload['run_date']} (baseline {changes['baseline_date']})", '']
    lines += [f"- {r['code']} {r['name']}: {r['change']} — {r['reason']}" for r in changes['records']]
    if not changes['records']:
        lines.append('No membership or routing changes.')
    summary = '\n'.join(lines) + '\n'
    (args.output_dir / 'ranking-changes.md').write_text(summary, encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(summary)
    print(summary)


if __name__ == '__main__':
    main()
