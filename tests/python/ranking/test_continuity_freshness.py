import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from types import SimpleNamespace

from qdii_ranking.audit import CandidateAudit, load_baseline, validate_audit
from qdii_ranking.errors import DataError
from qdii_ranking.freshness import freshness, require_fresh, load_calendars, validate_freshness, revalidate_nav_suspensions
from qdii_ranking.models import HolderPeriod
from qdii_ranking.ranking import build_holder_candidates
from qdii_ranking.sources.holder import fetch_holder_rows
from qdii_ranking.sources.performance import latest_series_value
from qdii_ranking.sources.fund import parse_fund_page
from qdii_ranking.services.ranking import route_and_rank, discover_candidates
from qdii_ranking.services.context import ExclusionCollector
from qdii_ranking.runtime import RunMetrics


class PublicationCalendarTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)

    def test_national_holiday_is_not_eight_late_workdays(self):
        result = require_fresh(date(2026, 9, 29), date(2026, 10, 8), now=self.now)
        self.assertEqual(9, result['calendar_lag_days'])
        self.assertEqual(1, result['publication_lag_days'])
        self.assertEqual('calendar_adjusted', result['status'])

    def test_spring_holiday_and_administrative_saturday_are_closed(self):
        result = require_fresh(date(2026, 2, 13), date(2026, 2, 24), now=self.now)
        self.assertEqual(1, result['publication_lag_days'])
        result = require_fresh(date(2026, 2, 27), date(2026, 3, 2), now=self.now)
        self.assertEqual(1, result['publication_lag_days'])

    def test_seven_passes_eight_blocks(self):
        result = require_fresh(date(2026, 3, 2), date(2026, 3, 11), now=self.now)
        self.assertEqual(7, result['publication_lag_days'])
        with self.assertRaisesRegex(DataError, '8 publication'):
            require_fresh(date(2026, 3, 2), date(2026, 3, 12), now=self.now)

    def test_separate_us_and_cn_holidays(self):
        us = freshness(date(2026, 9, 30), date(2026, 10, 7), 'nasdaq', now=self.now)
        cn = freshness(date(2026, 9, 30), date(2026, 10, 7), 'safe_fx', now=self.now)
        self.assertEqual(4, us['publication_lag_days'])
        self.assertEqual(0, cn['publication_lag_days'])

    def test_incomplete_local_day_not_counted(self):
        a = freshness(date(2026, 10, 7), date(2026, 10, 8), now=self.now)
        b = freshness(date(2026, 10, 7), date(2026, 10, 8), now=datetime(2026, 10, 9, tzinfo=timezone.utc))
        self.assertEqual(0, a['publication_lag_days'])
        self.assertEqual(1, b['publication_lag_days'])

    def test_missing_year_blocks_cross_year_evaluation(self):
        with self.assertRaisesRegex(DataError, '2025'):
            freshness(date(2025, 12, 30), date(2026, 1, 5), now=self.now)
        catalog = load_calendars()
        catalog['calendars']['cn_nav']['years']['2025'] = {
            'closed_dates': [], 'source_url': 'https://example.test/calendar', 'verified_on': '2025-12-01'}
        result = freshness(date(2025, 12, 30), date(2026, 1, 5), now=self.now, catalog=catalog)
        self.assertEqual(2, result['publication_lag_days'])

    def test_nav_notice_only_and_tamper_detection(self):
        catalog = load_calendars()
        notice = {'scope': 'nav_publication', 'code': '016701', 'published_date': '2026-03-01',
                  'start': '2026-03-03', 'end': '2026-03-03', 'source_url': 'https://example.test/notice',
                  'evidence_text': '暂停披露基金净值'}
        catalog['nav_suspensions'] = [notice]
        result = freshness(date(2026, 3, 2), date(2026, 3, 11), code='016701', now=self.now, catalog=catalog)
        self.assertEqual(6, result['publication_lag_days'])
        self.assertEqual(7, freshness(date(2026, 3, 2), date(2026, 3, 11), code='000001', now=self.now, catalog=catalog)['publication_lag_days'])
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.json'
            notice['scope'] = 'subscription'
            path.write_text(json.dumps(catalog), encoding='utf-8')
            with self.assertRaises(DataError):
                load_calendars(path)
        with patch('qdii_ranking.freshness.load_calendars', return_value=catalog):
            with self.assertRaises(DataError):
                revalidate_nav_suspensions(Mock(), date(2026, 10, 8))

    def test_evidence_is_recomputed_and_history_alignment_is_unchanged(self):
        observed, as_of = date(2026, 9, 29), date(2026, 10, 8)
        result = require_fresh(observed, as_of, now=self.now)
        validate_freshness(result, observed, as_of, 'cn_nav')
        result['publication_lag_days'] = 0
        with self.assertRaises(DataError):
            validate_freshness(result, observed, as_of, 'cn_nav')
        self.assertIsNone(latest_series_value({observed: 1.0}, [observed], as_of))


class CandidateContinuityTests(unittest.TestCase):
    def setUp(self):
        self.fund = {'code': '016701', 'name': '银华海外数字经济量化选股混合发起式(QDII)A', 'fund_type': 'QDII-混合偏股'}

    def test_full_directory_keeps_missing_and_empty_holder(self):
        for rows in ([], [['016701', '', '', '', '', '']]):
            candidates = build_holder_candidates(rows, {'016701': self.fund}, [])
            self.assertEqual(['016701'], [r['code'] for r in candidates])
            self.assertIsNone(candidates[0]['institution_holding_ratio_pct'])
        records = build_holder_candidates([['016701', '', '19.05', '80.95', '', '11.50']], {'016701': self.fund}, [])
        self.assertEqual(19.05, records[0]['institution_holding_ratio_pct'])

    def test_holder_duplicate_and_numeric_failure_retry_full_scan(self):
        period = HolderPeriod('2026-06-30', 2, '2026_2')
        row = ['016701', '', '19.05', '80.95', '', '11.5']
        client = Mock()
        client.get_text.return_value = 'data:' + json.dumps([row, row]) + ',record:2,pages:"1"'
        with self.assertRaisesRegex(DataError, 'duplicate'):
            fetch_holder_rows(client, period)
        self.assertEqual(2, client.get_text.call_count)
        row[2] = 'NaN'
        client.get_text.return_value = 'data:' + json.dumps([row]) + ',record:1,pages:"1"'
        with self.assertRaisesRegex(DataError, 'numeric range'):
            fetch_holder_rows(client, HolderPeriod('2026-06-30', 1, '2026_2'))

    def test_baseline_cold_start_and_invalid_state(self):
        # No output directory or caches are consulted by the durable baseline loader.
        self.assertTrue(load_baseline(date.max)['records'])
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.json'
            with self.assertRaises(DataError):
                load_baseline(date(2026, 10, 8), path)
            path.write_text('{', encoding='utf-8')
            with self.assertRaises(DataError):
                load_baseline(date(2026, 10, 8), path)

    def test_qualified_missing_holder_blocks_but_failed_quota_is_explained(self):
        with TemporaryDirectory() as directory:
            audit = CandidateAudit(date(2026, 10, 8), Path(directory) / 'audit.json')
            audit.register(self.fund)
            args = SimpleNamespace(_audit=audit, min_us_equity_pct=50, us_main_exclude_keywords=[], min_direct_limit_cny=200, top=10)
            fund = {**self.fund, 'institution_holding_ratio_pct': None, '_document_result': {
                'exposure': {'confirmed_pct': 0, 'possible_pct': 0}, 'exposure_warnings': [],
                'quota': {'direct_limit': {'status': 'limited', 'amount_cny': 200}},
                'quota_warnings': [], 'quota_error': None}}
            with self.assertRaisesRegex(DataError, 'missing institution'):
                route_and_rank(args, (fund,), '2026-06-30', ExclusionCollector(audit=audit))
            self.assertEqual('blocked', audit.items['016701']['status'])
            fund['_document_result']['quota']['direct_limit']['amount_cny'] = 100
            with self.assertRaisesRegex(DataError, 'Both QDII'):
                route_and_rank(args, (fund,), '2026-06-30', ExclusionCollector(audit=audit))
            self.assertEqual('excluded', audit.items['016701']['status'])
            self.assertEqual('direct_limit_below_threshold', audit.items['016701']['reason'])

    def test_metadata_missing_blocks_before_holder_fetch_even_with_new_period(self):
        with TemporaryDirectory() as directory:
            audit = CandidateAudit(date(2026, 10, 8), Path(directory) / 'audit.json')
            audit.baseline = {'holder_report_date': '2025-12-31', 'records': [self.fund]}
            args = SimpleNamespace(_audit=audit)
            with patch('qdii_ranking.services.ranking.fetch_fund_metadata', return_value={}), \
                 patch('qdii_ranking.services.ranking.fetch_holder_periods') as periods:
                with self.assertRaisesRegex(DataError, 'missing from metadata'):
                    discover_candidates(args, Mock(), date(2026, 10, 8), RunMetrics())
                periods.assert_not_called()
            self.assertEqual('metadata_missing', audit.items['016701']['reason'])

    def test_purchase_status_without_nearby_fee_label(self):
        page = ('规模</a>：6.23亿元（2026-06-30）'
                '成 立 日</span>：2021-03-03'
                '交易状态：</span><span>暂停申购</span><span>暂停赎回</span></div>'
                'var fundBuyStatus = "6"; var fundIsSale = true;')
        self.assertEqual('suspended', parse_fund_page(page, '011420')['purchase_status'])
        self.assertEqual('unknown', parse_fund_page(page.replace('暂停申购', '未知'), '011420')['purchase_status'])

    def test_added_rerouted_and_ranking_cap_are_traceable(self):
        with TemporaryDirectory() as directory:
            audit = CandidateAudit(date(2026, 10, 8), Path(directory) / 'audit.json')
            second = {**self.fund, 'code': '000002'}
            third = {**self.fund, 'code': '000003'}
            audit.baseline = {'run_date': '2026-09-24', 'records': [
                {**self.fund, 'ranking_list': 'us_main'}, {**third, 'ranking_list': 'global_supplement'}]}
            for fund in (self.fund, second, third):
                audit.register(fund)
            audit.event('000003', 'ranking', 'excluded', 'ranking_cap', '超过每榜前 10 只上限')
            records = [{**self.fund, 'ranking_list': 'global_supplement', 'rank': 1, 'routing_reason': 'us_exposure_below_threshold'},
                       {**second, 'ranking_list': 'global_supplement', 'rank': 2}]
            payload = {'run_date': '2026-10-08', 'records': [], 'global_supplement': {'records': records}}
            audit.finish(payload)
            self.assertEqual({'added', 'removed', 'rerouted'}, {r['change'] for r in payload['ranking_changes']['records']})

    def test_unexplained_exit_blocks_and_failure_remains_traceable(self):
        with TemporaryDirectory() as directory:
            audit = CandidateAudit(date(2026, 10, 8), Path(directory) / 'audit.json')
            audit.baseline = {'run_date': '2026-09-24', 'records': [{**self.fund, 'ranking_list': 'us_main'}]}
            audit.register(self.fund)
            payload = {'run_date': '2026-10-08', 'records': [], 'global_supplement': {'records': []}}
            with self.assertRaisesRegex(DataError, 'no terminal'):
                audit.finish(payload)
            audit.save('failure', 'source unavailable')
            self.assertEqual('not_evaluated', json.loads(audit.path.read_text(encoding="utf-8"))['records'][0]['status'])
            audit.event('016701', 'preliminary', 'excluded', 'purchase_suspended', '暂停申购')
            audit.finish(payload)
            self.assertEqual('removed', payload['ranking_changes']['records'][0]['change'])
            self.assertEqual('暂停申购', payload['ranking_changes']['records'][0]['reason'])
            validate_audit(payload, audit.path)
            payload['exclusion_summary'] = []
            with self.assertRaises(DataError):
                validate_audit(payload, audit.path)


if __name__ == '__main__':
    unittest.main()
