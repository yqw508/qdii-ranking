"""Run diagnostics and durable, validated ranking continuity state."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from .cache.base import write_json_atomic
from .errors import DataError
from .config import ROUTING_REASON_LABELS

BASELINE = Path(__file__).resolve().parents[2] / 'references/ranking-baseline.json'


def load_baseline(as_of: date, path: Path = BASELINE) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if value['schema_version'] != 1:
            raise ValueError('unsupported baseline version')
        if not date.fromisoformat(value['holder_report_date']) <= date.fromisoformat(value['run_date']) <= as_of:
            raise ValueError('invalid baseline dates')
        codes = value['candidate_codes']
        if not codes or len(codes) != len(set(codes)) or any(not re.fullmatch(r'\d{6}', c) for c in codes):
            raise ValueError('invalid candidate codes')
        records = value['records']
        if not records or len({r['code'] for r in records}) != len(records):
            raise ValueError('empty or duplicate ranked records')
        for record in records:
            if record['code'] not in codes or record['ranking_list'] not in ('us_main', 'global_supplement'):
                raise ValueError('invalid ranked record')
            if not isinstance(record.get('name'), str) or not record['name'].strip():
                raise ValueError('missing ranked fund name')
        for group in ('us_main', 'global_supplement'):
            ranks = [r.get('rank') for r in records if r['ranking_list'] == group]
            if len(ranks) > 10 or ranks != list(range(1, len(ranks) + 1)):
                raise ValueError('invalid baseline ranks')
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DataError(f'Ranking baseline is required and must be valid: {exc}') from exc


class CandidateAudit:
    def __init__(self, as_of: date, path: Path):
        self.as_of, self.path = as_of, path
        self.baseline = None
        self.items = {}
        self.stage = 'baseline'

    def register(self, fund: dict):
        self.items.setdefault(fund['code'], {'code': fund['code'], 'name': fund['name'],
                                           'status': 'pending', 'stage': 'discovery', 'events': []})

    def event(self, code, stage, status, reason, label, *, values=None, thresholds=None, sources=None, data_date=None):
        item = self.items[code]
        event = {'stage': stage, 'status': status, 'reason': reason, 'label': label,
                 'values': values or {}, 'thresholds': thresholds or {},
                 'source_urls': sources or [], 'data_date': data_date or self.as_of.isoformat()}
        item['events'].append(event)
        item.update(status=status, stage=stage, reason=reason, label=label)

    def exclusions(self):
        groups = {}
        for code, item in sorted(self.items.items()):
            if item['status'] != 'excluded':
                continue
            for event in item['events']:
                if event['status'] != 'excluded':
                    continue
                key = event['reason']
                group = groups.setdefault(key, {'reason': key, 'label': event['label'], 'codes': []})
                if code not in group['codes']:
                    group['codes'].append(code)
        return [{**g, 'count': len(g['codes'])} for g in groups.values()]

    def finish(self, payload):
        ranked = {r['code']: r for r in [*payload['records'], *payload['global_supplement']['records']]}
        for code, record in ranked.items():
            self.event(code, 'ranking', 'ranked', 'selected', '入榜',
                       values={'ranking_list': record['ranking_list'], 'rank': record['rank']})
        for code, item in self.items.items():
            if item['status'] not in ('ranked', 'excluded'):
                raise DataError(f'{code}: candidate has no terminal assessment')
        previous = {r['code']: r for r in self.baseline['records']}
        changes = []
        for code in sorted(previous.keys() | ranked.keys()):
            before, after = previous.get(code), ranked.get(code)
            if before and not after:
                item = self.items.get(code)
                if not item or item['status'] != 'excluded':
                    raise DataError(f'{code}: previous ranked fund disappeared without an explanation')
                kind, reason = 'removed', item['label']
            elif after and not before:
                kind, reason = 'added', '通过完整评估后入榜'
            elif before['ranking_list'] != after['ranking_list']:
                kind, reason = 'rerouted', ROUTING_REASON_LABELS.get(after['routing_reason'], after['routing_reason'])
            else:
                continue
            record = after or before
            changes.append({'code': code, 'name': record['name'], 'change': kind, 'reason': reason,
                            'previous_list': before['ranking_list'] if before else None,
                            'current_list': after['ranking_list'] if after else None})
        payload['exclusion_summary'] = self.exclusions()
        payload['ranking_changes'] = {'baseline_date': self.baseline['run_date'], 'records': changes}
        payload['candidate_audit_version'] = 1
        self.save('success')
        payload['candidate_audit_sha256'] = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def save(self, status, error=None):
        records = []
        for _, item in sorted(self.items.items()):
            item = {**item, 'events': list(item['events'])}
            if status == 'failure' and item['status'] not in ('excluded', 'blocked', 'ranked'):
                item.update(status='not_evaluated', reason='run_aborted', label=str(error), stage=self.stage)
            records.append(item)
        write_json_atomic(self.path, {'schema_version': 1, 'run_date': self.as_of.isoformat(),
                          'status': status, 'error': str(error) if error else None, 'failed_stage': self.stage if error else None,
                          'baseline_date': self.baseline['run_date'] if self.baseline else None,
                          'records': records, 'exclusion_summary': self.exclusions()})


def baseline_from_payload(payload, audit):
    return {'schema_version': 1, 'run_date': payload['run_date'],
            'holder_report_date': payload['holder_report_date'],
            'candidate_codes': sorted(r['code'] for r in audit['records']),
            'records': [{k: r[k] for k in ('code', 'name', 'rank', 'ranking_list')}
                        for r in [*payload['records'], *payload['global_supplement']['records']]]}


def validate_audit(payload, path):
    try:
        raw = path.read_bytes()
        audit = json.loads(raw)
        if hashlib.sha256(raw).hexdigest() != payload['candidate_audit_sha256']:
            raise ValueError('audit digest mismatch')
        if audit['schema_version'] != 1 or audit['status'] != 'success' or audit['run_date'] != payload['run_date']:
            raise ValueError('audit version/status/date mismatch')
        records = audit['records']
        if len(records) != len({r['code'] for r in records}) or any(r['status'] not in ('ranked', 'excluded') for r in records):
            raise ValueError('duplicate or unfinished candidate')
        ranked = {r['code'] for r in [*payload['records'], *payload['global_supplement']['records']]}
        if ranked != {r['code'] for r in records if r['status'] == 'ranked'}:
            raise ValueError('ranked audit codes disagree')
        collector = CandidateAudit(date.fromisoformat(payload['run_date']), path)
        collector.items = {r['code']: r for r in records}
        if collector.exclusions() != payload['exclusion_summary'] or audit['exclusion_summary'] != payload['exclusion_summary']:
            raise ValueError('audit exclusions disagree')
        if payload.get("filters") is not None:
            performance_count = sum(any(e["reason"] == "performance_calculated" for e in r["events"]) for r in records)
            documents_count = sum(any(e["reason"] == "documents_resolved" for e in r["events"]) for r in records)
            if performance_count != payload["filters"]["performance_candidates_scanned"] or documents_count != payload["filters"]["contract_candidates_scanned"]:
                raise ValueError("audit scan counters disagree")
        return audit
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DataError(f'Candidate audit validation failed: {exc}') from exc
