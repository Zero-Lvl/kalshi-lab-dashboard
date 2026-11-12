import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from test_engine import engine,market,signal,NOW


class ScanModeTests(unittest.TestCase):
    def test_existing_positions_are_refreshed_before_batched_shadow_forecasts(self):
        from urllib.parse import urlsplit,parse_qs
        e=engine()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'paper.db';l=e.Ledger(path)
            c=e.evaluate_candidate(market(),signal(),{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW)
            l.enter(c,NOW)
            for ticker in ('SHADOW-A','SHADOW-B'):
                l.observe_signal(signal(ticker=ticker,event_ticker=ticker+'-E'),market(ticker=ticker),NOW)
            l.close();calls=[]
            def fetch(url):
                calls.append(url)
                if '/markets/WX-A' in url:return {'market':market()}
                q=parse_qs(urlsplit(url).query)
                self.assertIn('tickers',q)
                return {'markets':[market(ticker=t,event_ticker=t+'-E',status='settled',result='no') for t in q['tickers'][0].split(',')],'cursor':''}
            r=e.scan(path,fetch_json=fetch,now=NOW+timedelta(days=1),no_news=True,settlement_only=True)
            self.assertIn('/markets/WX-A',calls[0])
            self.assertEqual(len(calls),2)
            l=e.Ledger(path)
            self.assertEqual(l.db.execute('SELECT count(*) FROM settlements').fetchone()[0],2)
            l.close()

    def _weather_policy_scan(self, policy, builder):
        from types import SimpleNamespace
        from unittest.mock import patch
        from test_signals import market as wm,om,nws,NOW as WNOW
        e=engine();m=dict(wm('greater',60),yes_bid_dollars='.36',yes_ask_dollars='.40',
            no_ask_dollars='.64',yes_bid_size_fp='100',yes_ask_size_fp='100',volume_24h_fp='1000')
        fake=SimpleNamespace(EXPANDED_SERIES=(),build_diversified_signals=builder)
        if policy is not None: fake.strategy_policy=policy
        def fetch(url):
            if 'open-meteo' in url:return om()
            if 'MapClick' in url:return nws()
            if '/series/' in url:return {'series':{'fee_type':'quadratic','fee_multiplier':1}}
            return {'markets':[m] if 'series_ticker=KXHIGHNY' in url else [],'cursor':''}
        with tempfile.TemporaryDirectory() as tmp,patch.dict('sys.modules',{'strategies':fake}):
            path=Path(tmp)/'paper.db'
            report=e.scan(path,fetch_json=fetch,now=WNOW,no_news=True)
            ledger=e.Ledger(path)
            audit=[dict(row) for row in ledger.db.execute('SELECT action,reason FROM decision_log')]
            self.assertEqual(len(ledger.forecasts()),1)
            ledger.close()
            return report,audit

    def test_independent_paused_weather_survives_builder_exception(self):
        from strategies import strategy_policy
        states=strategy_policy([],NOW)
        states['category/Weather']['status']='paused'
        def broken(*a,**k): raise RuntimeError('builder unavailable')
        report,audit=self._weather_policy_scan(lambda *a:states,broken)
        self.assertEqual(report['counts']['new_positions'],0)
        self.assertEqual(report['counts']['forecasts'],1)
        self.assertTrue(any(row['action']=='paused' for row in audit))
        self.assertEqual(report['strategy_states'],list(states.values()))

    def test_policy_failure_blocks_entries_but_retains_forecast_with_audit(self):
        def broken(*a,**k): raise RuntimeError('policy unavailable')
        report,audit=self._weather_policy_scan(broken,lambda *a,**k:{'signals':[],'strategy_states':[]})
        self.assertEqual(report['counts']['new_positions'],0)
        self.assertEqual(report['counts']['forecasts'],1)
        self.assertTrue(any(e['source']=='strategy policy' and 'policy unavailable' in e['error'] for e in report['errors']))
        self.assertTrue(any(row['action']=='paused' and 'policy' in row['reason'] for row in audit))

    def test_unpaused_weather_can_enter_during_unrelated_builder_outage(self):
        from strategies import strategy_policy
        def broken(*a,**k): raise RuntimeError('unrelated builder outage')
        report,_=self._weather_policy_scan(strategy_policy,broken)
        self.assertEqual(report['counts']['new_positions'],1)

    def test_builder_states_cannot_override_independent_weather_pause(self):
        from strategies import strategy_policy
        states=strategy_policy([],NOW)
        states['category/Weather']['status']='paused'
        report,_=self._weather_policy_scan(lambda *a:states,lambda *a,**k:{'signals':[],'strategy_states':[]})
        self.assertEqual(report['counts']['new_positions'],0)
        self.assertEqual(report['strategy_states'],list(states.values()))

    def test_unavailable_and_malformed_policy_fail_closed(self):
        from strategies import strategy_policy
        states=strategy_policy([],NOW)
        malformed=dict(states)
        malformed['category/Weather']=dict(states['category/Weather'],status='bogus')
        incomplete=dict(states)
        del incomplete['category/Weather']
        for policy in (None,lambda *a:None,lambda *a:[],lambda *a:{},lambda *a:malformed,lambda *a:incomplete):
            with self.subTest(policy=policy):
                report,audit=self._weather_policy_scan(policy,lambda *a,**k:{'signals':[],'strategy_states':[]})
                self.assertEqual(report['counts']['new_positions'],0)
                self.assertTrue(any(e['source']=='strategy policy' for e in report['errors']))
                self.assertTrue(any(row['action']=='policy_error' for row in audit))

    def test_weather_policy_pause_blocks_new_entry_but_keeps_forecast(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from test_signals import market as wm,om,nws,NOW as WNOW
        e=engine();m=dict(wm('greater',60),yes_bid_dollars='.36',yes_ask_dollars='.40',
            no_ask_dollars='.64',yes_bid_size_fp='100',yes_ask_size_fp='100',volume_24h_fp='1000')
        from strategies import strategy_policy
        states=strategy_policy([],NOW)
        states['weather-gaussian-v1-unvalidated/Weather']['status']='paused'
        fake=SimpleNamespace(EXPANDED_SERIES=(),strategy_policy=lambda *a:states,build_diversified_signals=lambda *a,**k:{
            'signals':[],'decisions':[],'errors':[], 'strategy_states':[{
                'strategy_id':'weather-gaussian-v1-unvalidated','category':'Weather','status':'paused'}]})
        def fetch(url):
            if 'open-meteo' in url:return om()
            if 'MapClick' in url:return nws()
            if '/series/' in url:return {'series':{'fee_type':'quadratic','fee_multiplier':1}}
            return {'markets':[m] if 'series_ticker=KXHIGHNY' in url else [],'cursor':''}
        with tempfile.TemporaryDirectory() as tmp,patch.dict('sys.modules',{'strategies':fake}):
            r=e.scan(Path(tmp)/'paper.db',fetch_json=fetch,now=WNOW,no_news=True)
            self.assertEqual(r['counts']['new_positions'],0)
            self.assertEqual(r['counts']['forecasts'],1)

    def test_expected_strategy_exclusions_are_not_reported_as_transport_errors(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        e=engine()
        from strategies import strategy_policy
        fake=SimpleNamespace(EXPANDED_SERIES=(),strategy_policy=strategy_policy,build_diversified_signals=lambda *a,**k:{
            'signals':[],'decisions':[],'strategy_states':[],
            'errors':[{'kind':'eligibility','ticker':'SKIP','error':'unsupported rules; abstain'}]})
        def fetch(url):
            if '/series/' in url:return {'series':{'fee_type':'quadratic','fee_multiplier':1}}
            return {'markets':[],'cursor':''}
        with tempfile.TemporaryDirectory() as tmp,patch.dict('sys.modules',{'strategies':fake}):
            r=e.scan(Path(tmp)/'paper.db',fetch_json=fetch,now=NOW,no_news=True)
            self.assertEqual(r['errors'],[])
            self.assertTrue(any(x['ticker']=='SKIP' for x in r['abstentions']))

    def test_expanded_category_signal_enters_small_paper_control_with_audit(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        e=engine()
        m=market(ticker='POL-A',event_ticker='POL-E',title='Will the Senate vote?',
                 yes_bid_dollars='.70',yes_ask_dollars='.72',no_ask_dollars='.30',no_bid_dollars='.28')
        s=signal(ticker='POL-A',event_ticker='POL-E',p_yes=.71,entry_tier='exploratory',
                 category='Politics',strategy_id='consensus-politics-v1',decision_reason='Market consensus baseline; no demonstrated edge')
        from strategies import strategy_policy
        states=strategy_policy([],NOW)
        states['consensus-politics-v1/Politics']=dict(states['consensus-control-politics/Politics'],strategy_id='consensus-politics-v1')
        module=SimpleNamespace(EXPANDED_SERIES=('POL',),strategy_policy=lambda *a:states,build_diversified_signals=lambda *a,**k:
            {'signals':[s],'decisions':[],'errors':[],'strategy_states':[]})
        def fetch(url):
            if '/series/' in url:return {'series':{'category':'Politics','fee_type':'quadratic','fee_multiplier':1}}
            return {'markets':[m] if 'series_ticker=POL' in url else [],'cursor':''}
        with tempfile.TemporaryDirectory() as tmp,patch.dict('sys.modules',{'strategies':module}):
            path=Path(tmp)/'paper.db'
            report=e.scan(path,fetch_json=fetch,now=NOW,no_news=True)
            self.assertEqual(report['counts']['new_positions'],1)
            ledger=e.Ledger(path)
            self.assertEqual(ledger.positions()[0]['signal']['category'],'Politics')
            self.assertLessEqual(ledger.open_cost(),2)
            self.assertGreater(ledger.db.execute('SELECT count(*) FROM decision_log').fetchone()[0],0)
            ledger.close()

    def test_settlement_refresh_never_scans_for_or_opens_new_positions(self):
        e=engine()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'paper.db'
            ledger=e.Ledger(path)
            c=e.evaluate_candidate(market(),signal(),{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW)
            ledger.enter(c,NOW);ledger.close()
            calls=[]
            def fetch(url):
                calls.append(url)
                self.assertIn('/markets/WX-A',url)
                return {'market':market(status='settled',result='yes')}
            report=e.scan(path,fetch_json=fetch,now=NOW+timedelta(days=1),no_news=True,settlement_only=True)
            self.assertEqual(report['counts']['new_positions'],0)
            self.assertEqual(report['counts']['forecasts'],0)
            self.assertEqual(len(calls),1)
            ledger=e.Ledger(path)
            self.assertEqual(ledger.positions()[0]['status'],'settled')
            self.assertEqual(len(ledger.positions()),1)
            ledger.close()
