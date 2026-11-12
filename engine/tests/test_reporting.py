import unittest
import tempfile
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from test_engine import engine, market, signal, NOW


class ReportTests(unittest.TestCase):
    def test_daily_plaintext_includes_every_settled_result_without_attachments(self):
        import reporting
        self.assertTrue(callable(getattr(reporting,'daily_text',None)), 'full daily text missing')
        e=engine()
        with tempfile.TemporaryDirectory() as tmp:
            ledger=e.Ledger(Path(tmp)/'paper.db')
            c=e.evaluate_candidate(market(),signal(),{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW)
            for i in range(8):
                ticker,event=f'RESULT-{i}',f'EVENT-{i}'
                ledger.enter(dict(c,ticker=ticker,event_ticker=event),NOW)
                ledger.settle(market(ticker=ticker,event_ticker=event,status='settled',result='yes' if i%2 else 'no'),NOW)
            r=reporting.make_report(ledger,{},NOW)
            r['public_export']={'periods':{
                'day':{'realized_pnl':'1.25'},'week':{'realized_pnl':'-2.50'},'month':{'realized_pnl':'3.75'}}}
            text=reporting.daily_text(r)
            self.assertIn('Day $1.25',text)
            self.assertIn('Week $-2.50',text)
            self.assertIn('Month $3.75',text)
            for i in range(8):self.assertIn(f'RESULT-{i}',text)
            self.assertNotIn('MEDIA:',text)
            self.assertIn('payout',text)
            ledger.close()

    def test_watchlist_ranks_news_liquidity_and_measures_observed_odds_changes(self):
        import reporting
        self.assertTrue(callable(getattr(reporting,'rank_watchlist',None)), 'watchlist ranking missing')
        a=market(title='Will the Fed cut rates?')
        b=market(ticker='UNRELATED',title='Unrelated',volume_24h_fp='200')
        prev=market(yes_ask_dollars='0.30',yes_bid_dollars='0.26')
        matches=[{'ticker':a['ticker'],'article_id':'story','score':3}]
        result=reporting.rank_watchlist([b,a],matches,[prev])
        self.assertEqual(result[0]['ticker'],a['ticker'])
        self.assertEqual(result[0]['news_count'],1)
        self.assertEqual(Decimal(result[0]['mid_change']),Decimal('.10'))
        self.assertIsNone(result[1]['mid_change'])
        self.assertIn('not',result[0]['interpretation'])

    def test_simulation_is_reproducible_and_uses_actual_entry_costs(self):
        import reporting
        self.assertTrue(callable(getattr(reporting,'simulate',None)), 'Monte Carlo missing')
        positions = [dict(status='open',event_ticker='E',side='yes',qty=10,cost='4.20',p_yes='1')]
        r = reporting.simulate(positions,paths=5000,seed=7)
        self.assertEqual(r['p05'],'5.80')
        self.assertEqual(r['p95'],'5.80')
        self.assertEqual(r['paths'],5000)
        self.assertIn('independent',r['assumption'])
        self.assertEqual(r,reporting.simulate(positions,paths=5000,seed=7))
        positions[0]['p_yes']='0'
        self.assertEqual(reporting.simulate(positions)['p95'],'-4.20')
        self.assertEqual(reporting.simulate([])['positions'],0)
        with self.assertRaises(ValueError):
            reporting.simulate(positions+positions)

    def test_realized_open_daily_and_hypothetical_results_are_separate(self):
        import importlib.util
        spec = importlib.util.find_spec('reporting')
        self.assertIsNotNone(spec, 'reporting module missing')
        import reporting
        e = engine()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = e.Ledger(Path(tmp)/'paper.db')
            c = e.evaluate_candidate(market(),signal(),{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW)
            ledger.enter(c,NOW)
            ledger.settle(market(status='settled',result='yes'),NOW)
            ledger.enter(dict(c,ticker='LOSS',event_ticker='EVENTLOSS'),NOW)
            ledger.settle(market(ticker='LOSS',status='settled',result='no'),NOW)
            ledger.enter(dict(c,ticker='OPEN',event_ticker='EVENTOPEN'),NOW)
            r = reporting.make_report(ledger, {'observed_at':NOW.isoformat(), 'counts':{},
                'markets':[market(ticker='OPEN',title='Will the daily high be below 60?')]}, NOW)
            self.assertEqual(r['positions'][-1]['market_title'],'Will the daily high be below 60?')
            s = r['summary']
            self.assertEqual(s['wins'],1)
            self.assertEqual(s['losses'],1)
            self.assertEqual(s['daily_settled'],2)
            self.assertEqual(Decimal(s['realized_net']),Decimal(c['qty'])-2*c['cost'])
            self.assertEqual(Decimal(s['cash']),Decimal('1000')+Decimal(c['qty'])-3*c['cost'])
            self.assertEqual(Decimal(r['positions'][-1]['potential_net_win']),Decimal(c['qty'])-c['cost'])
            self.assertIsNone(r['positions'][-1]['mark_net'])
            self.assertIn('PAPER ONLY',reporting.report_text(r))
            self.assertIn('Realized',reporting.report_text(r))
            self.assertLessEqual(len(reporting.report_text(r)),1900)
            later = reporting.make_report(ledger,{},NOW+timedelta(days=2))
            self.assertEqual(later['summary']['daily_settled'],0)
            ledger.close()
