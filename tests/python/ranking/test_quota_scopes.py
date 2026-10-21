"""Full-document regression for cross-fund and cross-investor quota contamination."""
import copy
import json
import re
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import update_qdii_ranking as ranking
from qdii_ranking.audit import validate_quota_evidence
from qdii_ranking.cache.quota import PARSER_FINGERPRINT


class ScopedQuotaTests(unittest.TestCase):
    notice = {'id': 'AN202610151830528221', 'title': '旗下部分基金暂停及恢复申购公告',
              'published': date(2026, 10, 16), 'url': 'https://pdf.dfcfw.com/pdf/H2_AN202610151830528221_1.pdf'}

    def setUp(self):
        self.text = (Path(__file__).resolve().parents[2] / 'fixtures/quota/yinhua-multi-20261016.txt').read_text(encoding='utf-8-sig')

    def fund(self, code):
        return {'code': code, 'purchase_status': 'limited', 'page_agency_limit_cny': None,
                'fund_page_url': f'https://example.test/{code}'}

    def resolve(self, code='016701', text=None, as_of=date(2026, 10, 21), cache=None, documents=None):
        documents = documents or Mock()
        documents.get_text.return_value = self.text if text is None else text
        with patch('qdii_ranking.sources.quota.fetch_announcements', return_value=[self.notice]):
            return ranking.resolve_quota(object(), self.fund(code), as_of, documents, notice_cache=cache)

    def values(self, quota):
        return tuple(quota[key]['amount_cny'] for key in ('direct_limit', 'institutional_direct_limit', 'agency_limit'))

    def test_full_notice_scopes_and_actual_effective_dates(self):
        quota, warnings = self.resolve()
        self.assertEqual([], warnings)
        self.assertEqual((2000000, 100000, 10000), self.values(quota))
        self.assertEqual('2026-10-20', quota['direct_limit']['effective_date'])
        self.assertTrue(ranking.direct_limit_qualifies(quota['direct_limit'], 200))
        dollar, warnings = self.resolve('007204')
        self.assertEqual([], warnings)
        self.assertEqual((100, 100, 100), self.values(dollar))
        other, _ = self.resolve('015204')
        self.assertEqual('not_applicable', other['notice_evidence'][0]['status'])
        self.assertEqual('unknown', other['direct_limit']['status'])

    def test_order_wrapping_and_page_continuations(self):
        blocks = re.split(r'(?m)^\s*\d+[.]\s*', self.text)
        reversed_text = blocks[0] + '\n' + '\n'.join(f'{i}. {block}' for i, block in enumerate(reversed(blocks[1:]), 1))
        for text in (reversed_text, self.text.replace('200 万', '200\n万').replace('万元', '万\n元').replace('机构', '机\n构')):
            quota, warnings = self.resolve(text=text)
            self.assertEqual([], warnings)
            self.assertEqual((2000000, 100000, 10000), self.values(quota))

    def test_future_transition_does_not_apply_early(self):
        text = self.text.replace('10 月 20 日', '10 月 25 日').replace('10 月 20\n日', '10 月 25\n日')
        quota, warnings = self.resolve(text=text)
        self.assertEqual([], warnings)
        self.assertLess(quota['direct_limit']['effective_date'], '2026-10-25')
        self.assertEqual('2026-10-14', quota['direct_limit']['effective_date'])

    def test_shared_document_cache_cold_hot_and_fund_query_order(self):
        for codes in (('016701', '007204'), ('007204', '016701')):
            with TemporaryDirectory() as directory:
                documents = Mock()
                documents.get_text.return_value = self.text
                for code in (*codes, *reversed(codes)):
                    cache = ranking.QuotaNoticeParseCache(Path(directory))
                    quota, warnings = self.resolve(code, cache=cache, documents=documents)
                    self.assertEqual([], warnings)
                    self.assertEqual((2000000, 100000, 10000) if code == '016701' else (100, 100, 100), self.values(quota))
                self.assertEqual(1, documents.get_text.call_count)

    def test_method_three_cache_is_refetched(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / (self.notice['id'] + '.json')
            path.write_text(json.dumps({'schema_version': 1, 'method_version': 3,
                'parser_fingerprint': PARSER_FINGERPRINT, 'identity': ranking.QuotaNoticeParseCache._identity(self.notice),
                'ok': True, 'transitions': [{'effective_date': '2026-10-20', 'direct_amount_cny': 100}]}), encoding='utf8')
            documents = Mock()
            documents.get_text.return_value = self.text
            quota, warnings = self.resolve(cache=ranking.QuotaNoticeParseCache(Path(directory)), documents=documents)
            self.assertEqual([], warnings)
            self.assertEqual((2000000, 100000, 10000), self.values(quota))
            self.assertTrue(documents.get_text.call_args.kwargs['force_refresh'])
            self.assertEqual(4, json.loads(path.read_text(encoding='utf8'))['method_version'])

    def test_ambiguous_owner_or_body_remains_unknown(self):
        for text in ('公告正文无法解析', '银华旗下部分基金公告，基金代码：016701、016702。自2026年10月20日起直销机构100元以上大额申购。',
                     '旗下部分基金 007204 016701\n1. 自2026年10月20日起直销机构100元以上大额申购。'):
            quota, warnings = self.resolve(text=text)
            self.assertEqual('unknown', quota['direct_limit']['status'])
            self.assertTrue(any('could not be parsed' in w for w in warnings))
            self.assertEqual('unresolved', quota['notice_evidence'][0]['status'])

    def test_personal_and_institutional_do_not_override_each_other(self):
        text = '自2026年10月20日起直销机构个人投资者200万元以上大额申购、直销机构机构投资者10万元以上大额申购、代销机构1万元以上大额申购。'
        quota, warnings = self.resolve(text=text)
        self.assertEqual([], warnings)
        self.assertEqual((2000000, 100000, 10000), self.values(quota))

    def test_validator_rejects_foreign_institutional_unbound_or_future_proof(self):
        quota, _ = self.resolve()
        audit = {'records': [{'code': '016701', 'events': [{'reason': 'documents_resolved', 'values': {'quota': quota}}]}]}
        payload = {'run_date': '2026-10-21'}
        validate_quota_evidence(payload, audit)
        for field, value in [('share_codes', ['007204']), ('investor_type', 'institutional'),
                             ('share_codes', []), ('effective_date', '2026-10-25')]:
            broken = copy.deepcopy(audit)
            evidence = broken['records'][0]['events'][0]['values']['quota']['notice_evidence']
            for event in evidence[0]['transitions']:
                if event['investor_type'] == 'personal':
                    event[field] = value
            with self.assertRaisesRegex(ranking.DataError, 'no effective fund/investor quota proof'):
                validate_quota_evidence(payload, broken)


