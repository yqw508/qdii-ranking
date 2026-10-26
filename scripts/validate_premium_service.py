"""Check the deployed quote contract; optionally require live upstream evidence."""
import argparse
import json
import math
from collections import Counter
from datetime import date, datetime, timezone
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

SITE = 'https://qdii-ranking-web-run-cool-d2gy0iw957219659c.webapps.tcloudbase.com'


def validate_payload(payload: dict, seed: dict, require_live: bool = False) -> dict:
    assert payload['schema_version'] == 1
    assert payload['catalog_fingerprint'] == seed['catalog_fingerprint']
    assert payload['request_id'] and payload['status'] in {'fresh', 'partial', 'cached_stale', 'unavailable'}
    codes = {e['code'] for e in seed['catalog']}
    records = payload['records']
    assert len(records) == len({r['code'] for r in records})
    assert all(r['code'] in codes for r in records)
    assert payload['expected_count'] == len(codes)
    assert payload['fresh_count'] == sum(r['status'] == 'fresh' for r in records)
    unavailable = payload.get('unavailable_products', [])
    assert len(unavailable) == len({r['code'] for r in unavailable})
    for item in unavailable:
        assert item['code'] in codes
        assert item['source'] == 'tencent'
        assert item['reason'] == {'U': 'NOT_LISTED', 'D': 'DELISTED', 'S': 'SUSPENDED', 'Z': 'LISTING_SUSPENDED'}.get(item['source_status'])
        assert item['source_url'].startswith('https://gu.qq.com/')
        assert datetime.fromisoformat(item['observed_at'].replace('Z', '+00:00')) <= datetime.now(timezone.utc)
        assert datetime.fromisoformat(item['source_updated_at'].replace('Z', '+00:00')) <= datetime.fromisoformat(item['observed_at'].replace('Z', '+00:00'))
        assert datetime.fromisoformat(item['observed_at'].replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).date() == datetime.fromisoformat(payload['requested_at'].replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).date()
    requested_date = date.fromisoformat(payload['requested_at'][:10])
    stale_ages = []
    for record in records:
        assert record['status'] in {'fresh', 'cached_stale'}
        price, reference = record['marketPriceCny'], record['referenceValueCny']
        assert math.isfinite(price) and math.isfinite(reference) and price > 0 and reference > 0
        assert all(math.isfinite(record[k]) for k in ('premiumPct', 'changePct', 'turnoverCny'))
        assert record['turnoverCny'] >= 0
        assert record['quoteSource'] in {'tencent', 'eastmoney'}
        assert record['adapterVersion'] == record['quoteSource'] + '-1'
        assert record['quoteDelayMinutes'] == (None if record['quoteSource'] == 'tencent' else 15)
        assert abs(record['premiumPct'] - (price / reference - 1) * 100) <= max(.05, .0005 / reference * 100 + .01)
        assert datetime.fromisoformat(record['updatedAt'].replace('Z', '+00:00')) <= datetime.now(timezone.utc)
        quote_date = date.fromisoformat(record['quoteDate'])
        assert quote_date <= requested_date
        if record['status'] == 'cached_stale':
            stale_age = (requested_date - quote_date).days
            assert stale_age <= 7, f"Cached quote {record['code']} exceeds the 7-day hard stale limit"
            stale_ages.append(stale_age)
        if record['referenceType'] == 'nav':
            assert date.fromisoformat(record['referenceDate']) <= requested_date
    assert payload.get('catalog_count', len(codes)) == len(codes)
    assert payload.get('stale_count', sum(r['status'] == 'cached_stale' for r in records)) == len(stale_ages)
    assert payload.get('unavailable_count', len(unavailable)) == len(unavailable)
    result = {'status': payload['status'], 'fresh_count': payload['fresh_count'],
              'expected_count': payload['expected_count'], 'cache_hit': payload['cache_hit'],
              'request_id': payload['request_id'], 'stale_count': len(stale_ages),
              'max_stale_age': max(stale_ages, default=0),
              'publication_decision': payload.get('publication_decision'),
              'unavailable_products': unavailable,
              'error_reasons': dict(Counter(e['reason'] for e in payload['errors']))}
    if require_live:
        assert payload['fresh_count'] > 0 and payload['status'] in {'fresh', 'partial'}, 'No validated live upstream quote'
        assert any(r['status'] == 'fresh' and r['checkedAt'] for r in records)
        fresh_codes = {r['code'] for r in records if r['status'] == 'fresh'}
        stale_codes = {r['code'] for r in records if r['status'] == 'cached_stale'}
        exceptions = {r['code'] for r in unavailable}
        assert not (fresh_codes | stale_codes) & exceptions
        assert fresh_codes | stale_codes | exceptions == codes, 'Unexplained missing products block publication'
        assert payload.get('unaccounted_count', 0) == 0
        catalog = {r['code']: r for r in seed['catalog']}
        for market in (0, 1):
            assert any(r['status'] == 'fresh' and r['quoteSource'] == 'tencent' and r['referenceType'] == 'iopv'
                       and catalog[r['code']]['market_id'] == market for r in records), 'Tencent SH/SZ ETF evidence missing'
        assert any(r['status'] == 'fresh' and r['quoteSource'] == 'tencent' and r['referenceType'] == 'nav'
                   for r in records), 'Tencent LOF evidence missing'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url')
    parser.add_argument('--snapshot', type=Path, default=Path('public/premium-snapshot.json'))
    parser.add_argument('--require-live', action='store_true')
    parser.add_argument('--output', type=Path, help='Save response evidence even when a validation assertion fails')
    args = parser.parse_args()
    url = args.url or json.loads(Path('references/premium-service.json').read_text(encoding='utf-8'))['endpoint']
    seed = json.loads(args.snapshot.read_text(encoding='utf-8'))
    url += '?' + urllib.parse.urlencode({'catalog': seed['catalog_fingerprint']})
    req = urllib.request.Request(url, headers={'Origin': SITE, 'User-Agent': 'QDII-premium-validator/1'})
    with urllib.request.urlopen(req, timeout=35) as response:
        assert response.status == 200
        assert response.headers.get('Access-Control-Allow-Origin') == SITE, 'Site CORS missing'
        payload = json.load(response)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(validate_payload(payload, seed, args.require_live), ensure_ascii=False))


if __name__ == '__main__':
    main()
