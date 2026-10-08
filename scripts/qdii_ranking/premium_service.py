"""Versioned public seed for the isolated premium refresh service."""
import hashlib
import json
from pathlib import Path

from .atomic import atomic_write_text
from .errors import DataError

SETTINGS = Path(__file__).resolve().parents[2] / 'references/premium-service.json'


def catalog_fingerprint(catalog):
    canonical = [[e['code'], e['market_id'], e['name'], e['benchmark_group']] for e in catalog]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def snapshot_from_payload(payload):
    premium = payload['exchange_premium']
    catalog = premium.get('catalog')
    if not catalog:
        raise DataError('Premium service snapshot requires the complete discovered catalog')
    catalog = sorted(catalog, key=lambda e: e['code'])
    if len(catalog) != premium['discovered_count'] or len({e['code'] for e in catalog}) != len(catalog):
        raise DataError('Premium service catalog is incomplete or duplicated')
    return {'schema_version': 1, 'generated_at': payload['generated_at'], 'run_date': payload['run_date'],
            'catalog_fingerprint': catalog_fingerprint(catalog), 'catalog': catalog,
            'unavailable_products': premium.get('unavailable_products', []),
            'adapter_version': premium.get('adapter_version', 'eastmoney-1'),
            'records': [r for r in premium['records'] if r.get('market_price_cny') is not None]}


def service_config(premium):
    settings = json.loads(SETTINGS.read_text(encoding='utf-8'))
    catalog = sorted(premium.get('catalog') or premium['records'], key=lambda e: e['code'])
    return {'serviceUrl': settings['endpoint'], 'catalogFingerprint': catalog_fingerprint(catalog),
            'entries': [{'code': r['code'], 'name': r['name'], 'benchmarkGroup': r['benchmark_group'],
                         'referenceType': r.get('reference_value_type'), 'referenceValueCny': r.get('reference_value_cny'),
                         'referenceDate': r.get('reference_value_date'), 'updatedAt': r.get('updated_at'),
                         'quoteDate': r.get('quote_date')} for r in premium['records']]}


def write_snapshot(path, payload):
    snapshot = snapshot_from_payload(payload)
    atomic_write_text(path, json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')


def validate_snapshot(path, payload):
    if json.loads(path.read_text(encoding='utf-8')) != snapshot_from_payload(payload):
        raise DataError('Premium service snapshot differs from ranking artifacts')
