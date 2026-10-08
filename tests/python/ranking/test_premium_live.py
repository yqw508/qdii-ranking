import json
import shutil
import subprocess
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from qdii_ranking.sources.premium_live import fetch_live_quotes, quote_rows_from_live
from qdii_ranking.sources.premium import normalize_exchange_premium_quote
from qdii_ranking.errors import DataError

ROOT = Path(__file__).resolve().parents[3]


class SharedPremiumAdapterTests(unittest.TestCase):
    def test_shared_fixture_produces_identical_python_values(self):
        script = r'''
const f=require('./tests/fixtures/tencent-quotes.json');
const {parseTencent}=require('./functions/qdii-premium-api/tencent.js');
const {normalizeQuote}=require('./functions/qdii-premium-api/premium_quotes.js');
const catalog=f.records.map(r=>({code:r.fields[2],name:r.fields[61],market_id:r.symbol.startsWith('sh')?1:0}));
const text=f.records.map(r=>`v_${r.symbol}="${r.fields.join('~')}";`).join('\n');
const parsed=parseTencent(text,catalog), quotes={};
for(const r of parsed.rows) quotes[r.f12]=normalizeQuote(r,{code:r.f12,name:r.f14,
referenceType:'nav',referenceValueCny:3.1328,referenceDate:'2026-09-29'},f.as_of);
process.stdout.write(JSON.stringify({quotes,errors:parsed.errors,unavailable:parsed.unavailable}));
'''
        p = subprocess.run([shutil.which('node'), '-e', script], cwd=ROOT, capture_output=True,
                           encoding='utf-8', check=True)
        payload = json.loads(p.stdout)
        for code, raw in quote_rows_from_live(payload).items():
            actual = normalize_exchange_premium_quote(raw, {'code': code, 'name': 'fixture', 'market_id': 1},
                                                     date(2026, 10, 8), raw['_reference'])
            expected = payload['quotes'][code]
            self.assertEqual(actual['market_price_cny'], expected['marketPriceCny'])
            self.assertEqual(actual['premium_pct'], expected['premiumPct'])
            self.assertEqual(actual['reference_value_cny'], expected['referenceValueCny'])
            self.assertEqual(actual['turnover_cny'], round(expected['turnoverCny'], 3))
            self.assertEqual(actual['quote_source'], 'tencent')
            self.assertIsNone(actual['quote_delay_minutes'])
        self.assertEqual(payload['unavailable'][0]['reason'], 'NOT_LISTED')

    def test_missing_runtime_and_child_failure_are_data_errors(self):
        with patch('shutil.which', return_value=None), self.assertRaises(DataError):
            fetch_live_quotes([])
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('node', 30)), self.assertRaises(DataError):
            fetch_live_quotes([])

    def test_adapter_code_conflict_rejected(self):
        with self.assertRaises(DataError):
            quote_rows_from_live({'quotes': {'513500': {'code': '161125'}}})
