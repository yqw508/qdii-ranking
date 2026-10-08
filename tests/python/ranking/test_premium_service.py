import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from qdii_ranking.errors import DataError
from qdii_ranking.premium_service import snapshot_from_payload, validate_snapshot, write_snapshot


class PremiumServiceSnapshotTests(unittest.TestCase):
    def payload(self):
        return {'generated_at': '2026-10-08T11:00:00+08:00', 'run_date': '2026-10-08',
                'exchange_premium': {'discovered_count': 2,
                  'catalog': [{'code': '513500', 'market_id': 1, 'name': 'ETF', 'benchmark_group': 'QDII'},
                              {'code': '161125', 'market_id': 0, 'name': 'LOF', 'benchmark_group': 'QDII'}],
                  'records': [{'code': '513500', 'market_price_cny': 2.0}]}}

    def test_keeps_unavailable_discovered_products_in_catalog(self):
        payload = self.payload()
        snapshot = snapshot_from_payload(payload)
        self.assertEqual(['161125', '513500'], [r['code'] for r in snapshot['catalog']])
        self.assertEqual(1, len(snapshot['records']))
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'premium-snapshot.json'
            write_snapshot(path, payload)
            validate_snapshot(path, payload)
            modified = json.loads(path.read_text(encoding='utf-8'))
            modified['catalog'][0]['code'] = '000001'
            path.write_text(json.dumps(modified), encoding='utf-8')
            with self.assertRaises(DataError):
                validate_snapshot(path, payload)

    def test_rejects_incomplete_or_duplicate_catalog(self):
        for mutate in ('missing', 'duplicate'):
            payload = self.payload()
            catalog = payload['exchange_premium']['catalog']
            if mutate == 'missing':
                catalog.pop()
            else:
                catalog[1] = catalog[0]
            with self.assertRaises(DataError):
                snapshot_from_payload(payload)
