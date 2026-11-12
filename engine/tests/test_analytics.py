"""Public analytics contract; synthetic fixtures never enter the live ledger."""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from engine import Ledger, dumps

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 11, 20, tzinfo=timezone.utc)


def export(ledger, report=None, now=NOW):
    path = ROOT / 'analytics.py'
    assert path.exists(), 'public analytics implementation missing'
    spec = importlib.util.spec_from_file_location('analytics_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_public_export(ledger, report or {}, now=now)


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / 'paper.db')
        self.addCleanup(self.ledger.close)

    def enter(self, ticker='WX-A', event='WX-E', when=None, side='yes', signal=None, qty=10):
        candidate = {'ticker': ticker, 'event_ticker': event, 'side': side,
                     'qty': qty, 'entry_price': Decimal('.40'), 'fee': Decimal('.17'),
                     'cost': Decimal('.40') * qty + Decimal('.17'), 'p_yes': Decimal('.8'),
                     'baseline_p_yes': Decimal('.5'), 'model_version': 'weather-gaussian-v1',
                     'signal': {'market_title': 'Daily high temperature', 'fee_multiplier': '1',
                                'model_version': 'weather-gaussian-v1', **(signal or {})}}
        self.assertTrue(self.ledger.enter(candidate, when or NOW - timedelta(hours=1)))

    def settle(self, ticker='WX-A', result='yes', when=None):
        self.ledger.settle({'ticker': ticker, 'status': 'settled', 'result': result}, when or NOW)

    def test_legacy_version_aligns_position_and_adaptation_strategy_identity(self):
        from strategies import strategy_policy
        version='weather-gaussian-v1-unvalidated'
        candidate={'ticker':'WX-A','event_ticker':'WX-E','side':'yes','qty':10,
                   'entry_price':Decimal('.40'),'fee':Decimal('.17'),'cost':Decimal('4.17'),
                   'p_yes':Decimal('.8'),'baseline_p_yes':Decimal('.5'),
                   'model_version':version,'signal':{'model_version':version}}
        self.assertTrue(self.ledger.enter(candidate,NOW-timedelta(hours=1)))
        result=export(self.ledger,{'strategy_states':list(strategy_policy(self.ledger,NOW).values())})
        self.assertEqual(result['positions'][0]['strategy'],version)
        matching=[row for row in result['strategies'] if row['id']==version]
        self.assertEqual(len(matching),1)
        self.assertEqual(matching[0]['state'],'warming_up')
        self.assertFalse(any(row['id']=='weather-gaussian-v1' for row in result['strategies']))

    def test_cash_realized_curve_uses_immutable_entry_and_settlement_times(self):
        opened = NOW - timedelta(days=2)
        won = NOW - timedelta(days=1)
        lost = NOW - timedelta(hours=2)
        self.enter(when=opened)
        self.settle(when=won)
        self.enter('LOSS', 'LOSS-E', when=opened + timedelta(hours=1))
        self.settle('LOSS', result='no', when=lost)
        self.enter('OPEN', 'OPEN-E')
        report = {'simulation': {'mean': '99999'}, 'summary': {'realized_net': '99999'},
                  'positions': [{'ticker': 'invented'}]}
        result = export(self.ledger, report)
        s = result['summary']
        self.assertEqual(Decimal(s['cash']), Decimal('997.49'))
        self.assertEqual(Decimal(s['open_cost']), Decimal('4.17'))
        self.assertEqual(Decimal(s['realized_pnl']), Decimal('1.66'))
        self.assertIsNone(s['unrealized_pnl'])
        self.assertIsNone(s['total_pnl'])
        self.assertEqual((s['wins'], s['losses'], s['settled']), (1, 1, 2))
        self.assertEqual((s['priced_positions'], s['unpriced_positions']), (0, 1))
        self.assertEqual(len(result['positions']), 3)
        self.assertEqual(result['tracking_started_at'], opened.isoformat())
        points = {p['time']: p for p in result['performance']['points']}
        self.assertEqual(Decimal(points[won.isoformat()]['realized_pnl']), Decimal('5.83'))
        self.assertEqual(Decimal(points[lost.isoformat()]['realized_pnl']), Decimal('1.66'))
        self.assertEqual(Decimal(points[opened.isoformat()]['realized_pnl']), Decimal('0'))
        self.assertEqual(Decimal(points[opened.isoformat()]['cash']), Decimal('995.83'))
        self.assertIsNone(points[opened.isoformat()]['total_pnl'])
        self.assertEqual(min(points), opened.isoformat())
        self.assertNotIn('99999', json.dumps(result))
        public = result['positions'][0]
        self.assertEqual(public['category'], 'Weather')
        self.assertEqual(public['strategy'], 'weather-gaussian-v1')
        self.assertEqual(Decimal(public['realized_pnl']), Decimal('5.83'))
        self.assertEqual(Decimal(public['estimated_net_win']), Decimal('5.83'))
        self.assertEqual(Decimal(public['estimated_net_loss']), Decimal('-4.17'))
        self.assertEqual(set(public), {'ticker', 'title', 'category', 'strategy', 'side', 'qty',
            'opened_at', 'status', 'entry_price', 'fee', 'cost', 'p_yes', 'baseline_p_yes',
            'settled_at', 'result', 'payout', 'realized_pnl', 'unrealized_pnl',
            'estimated_net_win', 'estimated_net_loss', 'evidence', 'decision_reason'})

    def test_calendar_periods_use_los_angeles_boundaries_and_charge_fees_on_entry(self):
        times = [datetime(2026, 10, 31, 23, tzinfo=timezone.utc),
                 datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                 datetime(2026, 11, 9, 8, tzinfo=timezone.utc),
                 datetime(2026, 11, 11, 7, 59, tzinfo=timezone.utc), NOW]
        for i, when in enumerate(times):
            self.enter(str(i), str(i), when=when)
        self.settle('0', when=times[1])
        self.settle('1', result='no', when=times[2])
        self.settle('2', when=datetime(2026, 11, 11, 8, tzinfo=timezone.utc))
        self.settle('3', result='no', when=times[3])
        r = export(self.ledger)
        for period, pnl, fees, settled, wins, losses in [
            ('day', '5.83', '.17', 1, 1, 0),
            ('week', '-2.51', '.51', 3, 1, 2),
            ('month', '3.32', '.68', 4, 2, 2),
        ]:
            with self.subTest(period=period):
                row = r['periods'][period]
                self.assertEqual(Decimal(row['realized_pnl']), Decimal(pnl))
                self.assertEqual(Decimal(row['fees']), Decimal(fees))
                self.assertEqual((row['settled'], row['wins'], row['losses']),
                                 (settled, wins, losses))
        self.assertEqual(Decimal(r['counts']['fees_paid']), Decimal('.85'))
        self.assertEqual(Decimal(r['counts']['max_drawdown']), Decimal('8.34'))

    def test_liquidation_uses_bid_visible_depth_and_exit_fees_not_report_marks(self):
        self.enter()
        m = {'ticker': 'WX-A', 'status': 'active', '_fetched_at': NOW.isoformat(),
             'yes_bid_dollars': '.60', 'yes_ask_dollars': '.90',
             'no_bid_dollars': '.10', 'yes_bid_size_fp': '10', 'yes_ask_size_fp': '100'}
        r = export(self.ledger, {'markets': [m], 'positions': [{'mark_net': '99999'}]})
        self.assertIsNotNone(r['summary']['unrealized_pnl'], 'fresh depth-backed bid was not priced')
        self.assertEqual(Decimal(r['summary']['unrealized_pnl']), Decimal('1.56'))
        self.assertEqual(Decimal(r['summary']['total_pnl']), Decimal('1.56'))
        self.assertEqual(Decimal(r['positions'][0]['unrealized_pnl']), Decimal('1.56'))
        self.assertEqual((r['summary']['priced_positions'], r['summary']['unpriced_positions']), (1, 0))
        for changes in ({'yes_bid_size_fp': '9.99'}, {'yes_bid_size_fp': None},
                        {'yes_bid_dollars': None}, {'yes_bid_dollars': 'NaN'},
                        {'yes_bid_dollars': 'Infinity'}, {'status': 'closed'},
                        {'_fetched_at': (NOW - timedelta(hours=2, seconds=1)).isoformat()},
                        {'_fetched_at': (NOW + timedelta(seconds=1)).isoformat()}):
            with self.subTest(changes=changes):
                r = export(self.ledger, {'markets': [dict(m, **changes)]})
                self.assertIsNone(r['summary']['unrealized_pnl'])
                self.assertIsNone(r['summary']['total_pnl'])
                self.assertEqual(r['summary']['unpriced_positions'], 1)
                json.dumps(r, allow_nan=False)
        self.enter('NO', 'NO-E', side='no')
        no = dict(m, ticker='NO', no_bid_dollars='.50')
        r = export(self.ledger, {'markets': [m, no]})
        self.assertEqual(Decimal(r['summary']['unrealized_pnl']), Decimal('2.11'))
        no['yes_ask_size_fp'] = '0'
        r = export(self.ledger, {'markets': [m, no]})
        self.assertIsNone(r['summary']['unrealized_pnl'])
        self.assertIsNone(r['summary']['total_pnl'])
        self.assertEqual(r['summary']['priced_positions'], 1)

    def run_record(self, when, markets=(), **extra):
        self.ledger.db.execute('CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, '
                               'started_at TEXT NOT NULL, completed_at TEXT, report_json TEXT)')
        raw = {'observed_at': when.isoformat(), 'markets': list(markets), **extra}
        self.ledger.db.execute('INSERT INTO runs (started_at,completed_at,report_json) VALUES (?,?,?)',
                               (when.isoformat(), when.isoformat(), dumps(raw)))
        return raw

    def test_history_marks_use_raw_scan_times_and_never_backfill_current_quotes(self):
        inception = NOW - timedelta(days=4)
        self.run_record(inception)
        opened = NOW - timedelta(days=3)
        self.enter(when=opened)
        historical = NOW - timedelta(days=2)
        quote = {'ticker': 'WX-A', 'status': 'active', '_fetched_at': historical.isoformat(),
                 'yes_bid_dollars': '.60', 'yes_bid_size_fp': '10'}
        self.run_record(historical + timedelta(minutes=1), [quote])
        missing = NOW - timedelta(days=1)
        self.run_record(missing, [], simulation={'mean': '99999'})
        settled = NOW - timedelta(hours=2)
        self.settle(when=settled)
        current = self.run_record(NOW, [dict(quote, _fetched_at=NOW.isoformat(), yes_bid_dollars='.90')])
        r = export(self.ledger, current)
        points = {p['time']: p for p in r['performance']['points']}
        self.assertIn(historical.isoformat(), points, 'raw market observation missing from chart')
        self.assertEqual(r['tracking_started_at'], inception.isoformat())
        self.assertEqual(r['observed_at'], NOW.isoformat())
        self.assertEqual(Decimal(points[inception.isoformat()]['cash']), Decimal('1000'))
        self.assertEqual(Decimal(points[inception.isoformat()]['total_pnl']), Decimal('0'))
        self.assertIsNone(points[opened.isoformat()]['total_pnl'])
        self.assertEqual(Decimal(points[historical.isoformat()]['total_pnl']), Decimal('1.56'))
        self.assertEqual(Decimal(points[historical.isoformat()]['realized_pnl']), Decimal('0'))
        self.assertIsNone(points[missing.isoformat()]['total_pnl'])
        self.assertEqual(Decimal(points[settled.isoformat()]['realized_pnl']), Decimal('5.83'))
        self.assertEqual(Decimal(points[settled.isoformat()]['unrealized_pnl']), Decimal('0'))
        self.assertEqual(len(points), len(r['performance']['points']), 'duplicate timestamps')
        self.assertNotIn('99999', json.dumps(r))
        for key, days in [('24h', 1), ('7d', 7), ('1m', 30), ('3m', 90)]:
            self.assertEqual(r['performance']['windows'][key],
                             {'start': max(inception, NOW - timedelta(days=days)).isoformat(),
                              'end': NOW.isoformat()})

    def test_public_allowlist_sanitizes_metadata_evidence_and_does_not_publish_private_logs(self):
        urls = ['https://news.example.org/story', 'http://example.org/research',
                'https://user:password@news.example.org/secret', 'file:///home/private',
                'https://127.0.0.1/x', 'http://10.1.2.3/x', 'http://[::1]/x',
                'http://169.254.169.254/x', 'http://localhost/x', 'http://host.internal/x',
                'http://2130706433/x', 'http://127.1/x', 'javascript:alert(1)',
                'https://example.org/x?api_key=PRIVATE_CANARY']
        evidence = [{'label': 'Public source', 'url': u, 'raw_article': 'PRIVATE_CANARY'} for u in urls]
        signal = {'category': 'Politics', 'strategy_id': 'consensus-politics-v1',
                  'entry_tier': 'exploratory', 'decision_reason': 'Market consensus; no demonstrated edge.',
                  'evidence': evidence, 'private_id': 'PRIVATE_CANARY',
                  'raw_articles': ['PRIVATE_CANARY'], 'environment': {'TOKEN': 'PRIVATE_CANARY'}}
        self.enter(signal=signal, qty=2)
        raw = {'coverage': [{'series': 'POL', 'returned': 3, 'truncated': True, 'max_pages': 2,
                             'dbpath': 'PRIVATE_CANARY'}],
               'counts': {'markets': 3, 'forecasts': 1, 'environment': 'PRIVATE_CANARY'},
               'news': {'articles': [{'title': 'PRIVATE_CANARY'}]},
               'errors': [{'error': 'PRIVATE_CANARY'}], 'warnings': ['PRIVATE_CANARY'],
               'decisions': [{'payload': 'PRIVATE_CANARY'}], 'dbpath': 'PRIVATE_CANARY'}
        before = dumps(raw)
        r = export(self.ledger, raw)
        p = r['positions'][0]
        self.assertEqual((p['category'], p['strategy']), ('Politics', 'consensus-politics-v1'))
        self.assertEqual(p['decision_reason'], signal['decision_reason'])
        self.assertEqual([e['url'] for e in p['evidence']], urls[:2])
        self.assertTrue(all(set(e) == {'label', 'url'} for e in p['evidence']))
        self.assertEqual(r['coverage'], [{'series': 'POL', 'returned': 3, 'truncated': True, 'max_pages': 2}])
        self.assertEqual(r['counts']['markets'], 3)
        self.assertEqual(r['counts']['errors'], 1)
        self.assertNotIn('PRIVATE_CANARY', json.dumps(r, allow_nan=False))
        self.assertEqual(dumps(raw), before)

    def test_strategy_scores_and_sample_counts_group_correlated_forecast_buckets(self):
        observed = NOW - timedelta(days=3)
        for ticker, event, prob in [('A', 'EVENT', '.8'), ('A', 'EVENT', '.1'),
                                     ('B', 'EVENT', '.2'), ('C', 'OTHER', '.5')]:
            signal = {'ticker': ticker, 'event_ticker': event, 'p_yes': prob,
                      'model_version': 'weather-gaussian-v1'}
            self.ledger.observe_signal(signal, {'yes_ask_dollars': '.4', 'yes_bid_dollars': '.4'},
                                       observed + timedelta(minutes=len(self.ledger.forecasts())))
        for ticker, result in [('A', 'yes'), ('B', 'no'), ('C', 'no')]:
            self.settle(ticker, result=result, when=NOW - timedelta(days=1))
        self.enter('CRYPTO', 'CRYPTO-E', signal={'category': 'Crypto', 'strategy_id': 'crypto-btc-terminal'})
        self.settle('CRYPTO', result='no')
        states = [{'strategy_id': 'consensus-politics-v1', 'category': 'Politics',
                   'status': 'warming_up', 'payload': 'PRIVATE_CANARY'}]
        r = export(self.ledger, {'strategy_states': states})
        strategies = {(s['id'], s['category']): s for s in r['strategies']}
        self.assertIn(('weather-gaussian-v1', 'Weather'), strategies, 'missing forecast performance')
        weather = strategies['weather-gaussian-v1', 'Weather']
        self.assertEqual(weather['settled_events'], 2)
        self.assertEqual(Decimal(weather['brier']), Decimal('.145'))
        self.assertEqual(Decimal(weather['baseline_brier']), Decimal('.21'))
        self.assertEqual(Decimal(weather['realized_pnl']), Decimal('0'))
        crypto = strategies['crypto-btc-terminal', 'Crypto']
        self.assertEqual(crypto['settled_events'], 1)
        self.assertEqual(Decimal(crypto['realized_pnl']), Decimal('-4.17'))
        self.assertEqual(strategies['consensus-politics-v1', 'Politics']['state'], 'warming_up')
        self.assertIsNone(strategies['consensus-politics-v1', 'Politics']['brier'])
        self.assertEqual(r['counts']['forecast_observations'], 4)
        self.assertEqual(r['counts']['forecast_markets'], 3)
        self.assertEqual(r['counts']['forecast_events'], 2)
        self.assertEqual(r['counts']['settled_events'], 3)
        self.assertEqual(r['tracking_started_at'], observed.isoformat())
        categories = {c['category']: c for c in r['counts']['categories']}
        self.assertEqual(categories['Weather']['settled_events'], 2)
        self.assertEqual(Decimal(categories['Crypto']['realized_pnl']), Decimal('-4.17'))
        for s in r['strategies']:
            self.assertEqual(set(s), {'id', 'category', 'state', 'settled_events', 'brier',
                                     'baseline_brier', 'realized_pnl', 'note'})
        self.assertNotIn('PRIVATE_CANARY', json.dumps(r))

    def test_ninety_day_history_is_bounded_read_only_and_reports_limited_coverage(self):
        inception = NOW - timedelta(days=110)
        self.run_record(inception)
        for scan in range(181):
            self.run_record(NOW - timedelta(hours=scan * 12))
        self.ledger.db.execute('INSERT INTO runs (started_at,completed_at,report_json) VALUES (?,?,?)',
                               (NOW.isoformat(), NOW.isoformat(), '{not JSON: PRIVATE_CANARY'))
        before = list(self.ledger.db.iterdump())
        self.ledger.db.execute('PRAGMA query_only=ON')
        r = export(self.ledger)
        self.assertEqual(r['counts'].get('scan_observations'), 181,
                         'must expose actual observation coverage')
        self.assertEqual(r['counts']['history_errors'], 1)
        self.assertEqual(r['tracking_started_at'], inception.isoformat())
        self.assertLessEqual(len(r['performance']['points']), 183)
        self.assertEqual(r['performance']['windows']['3m']['start'],
                         (NOW - timedelta(days=90)).isoformat())
        self.assertEqual(list(self.ledger.db.iterdump()), before)
        warnings = ' '.join(r['warnings'])
        self.assertIn('90-day', warnings)
        self.assertIn('America/Los_Angeles', warnings)
        self.assertIn('realized', warnings.lower())
        self.assertNotIn('PRIVATE_CANARY', json.dumps(r))

    def test_legacy_title_falls_back_to_observed_market_without_exporting_raw_payloads(self):
        self.enter(signal={'market_title': '', 'sources': [{'name': 'Forecast',
            'url': 'https://forecast.weather.gov/example', 'raw': 'PRIVATE_CANARY'}]})
        r = export(self.ledger, {'observed_at': NOW.isoformat(), 'markets': [
            {'ticker': 'WX-A', 'title': 'Observed daily high', 'raw': 'PRIVATE_CANARY'}]})
        p = r['positions'][0]
        self.assertEqual(p['title'], 'Observed daily high')
        self.assertEqual(p['category'], 'Weather')
        self.assertEqual(p['strategy'], 'weather-gaussian-v1')
        self.assertEqual(p['evidence'], [{'label': 'Forecast', 'url': 'https://forecast.weather.gov/example'}])
        self.assertNotIn('PRIVATE_CANARY', json.dumps(r))

    def test_empty_schema_has_no_fabricated_inception_or_points(self):
        result = export(self.ledger)
        self.assertEqual(set(result), {'schema_version', 'generated_at', 'observed_at',
            'tracking_started_at', 'paper_only', 'summary', 'periods', 'performance',
            'positions', 'coverage', 'counts', 'strategies', 'warnings'})
        self.assertEqual(result['schema_version'], 2)
        self.assertIs(result['paper_only'], True)
        self.assertIsNone(result['tracking_started_at'])
        self.assertIsNone(result['observed_at'])
        self.assertEqual(result['performance']['points'], [])
        self.assertEqual(result['summary']['initial_bankroll'], '1000.00')
        self.assertEqual(Decimal(result['summary']['cash']), Decimal('1000'))
        self.assertEqual(result['summary']['total_positions'], 0)
        self.assertEqual(result['performance']['windows'].keys(), {'24h', '7d', '1m', '3m'})
        self.assertTrue(result['warnings'])
        json.dumps(result, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
