"""New v2 risk behavior. Fixtures never touch the live paper ledger."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from test_engine import engine,market,signal,NOW


class DiversifiedRiskTests(unittest.TestCase):
    def test_forecast_delete_is_immutable_and_preserves_count(self):
        import sqlite3
        e=engine()
        with tempfile.TemporaryDirectory() as tmp:
            ledger=e.Ledger(Path(tmp)/'paper.db')
            ledger.observe_signal(signal(),market(),NOW)
            before=ledger.db.execute('SELECT count(*) FROM signals').fetchone()[0]
            self.assertEqual(before,1)
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable forecast'):
                ledger.db.execute('DELETE FROM signals')
            self.assertEqual(ledger.db.execute('SELECT count(*) FROM signals').fetchone()[0],before)
            ledger.close()

    def test_consensus_control_can_research_the_no_side_without_fake_edge(self):
        e=engine()
        m=market(yes_bid_dollars='.33',yes_ask_dollars='.34',no_ask_dollars='.67',no_bid_dollars='.66',volume_24h_fp='2000')
        s=signal(p_yes=.335,entry_tier='exploratory',category='Politics',strategy_id='consensus-politics-v1',decision_reason='Baseline research control, no demonstrated edge')
        c=e.evaluate_candidate(m,s,{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW)
        self.assertIsNotNone(c)
        self.assertEqual(c['side'],'no')
        self.assertEqual(c['p_yes'],Decimal('.335'))
        self.assertLess(c['net_margin'],0)
        self.assertLessEqual(c['cost'],2)

    def test_entry_and_official_outcome_write_immutable_atomic_audit_records(self):
        e=engine()
        c=e.evaluate_candidate(market(),signal(),{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW)
        with tempfile.TemporaryDirectory() as tmp:
            ledger=e.Ledger(Path(tmp)/'paper.db')
            self.assertTrue(ledger.enter(c,NOW))
            rows=ledger.db.execute('SELECT action,ticker,payload_json FROM decision_log').fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['action'],'entry')
            ledger.settle(market(status='settled',result='no'),NOW)
            self.assertEqual(ledger.db.execute('SELECT count(*) FROM decision_log WHERE action=?',('settlement',)).fetchone()[0],1)
            ledger.settle(market(status='settled',result='no'),NOW)
            self.assertEqual(ledger.db.execute('SELECT count(*) FROM decision_log').fetchone()[0],2)
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):
                ledger.db.execute("UPDATE decision_log SET reason='rewritten'")
            ledger.close()

    def test_exploration_limits_are_enforced_inside_cash_transaction(self):
        e=engine()
        m=market(yes_bid_dollars='.70',yes_ask_dollars='.72',no_ask_dollars='.30',no_bid_dollars='.28')
        s=signal(p_yes=.71,entry_tier='exploratory',category='Politics',strategy_id='consensus-politics-v1',decision_reason='Baseline exploratory control')
        c=e.evaluate_candidate(m,s,{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW)
        with tempfile.TemporaryDirectory() as tmp:
            ledger=e.Ledger(Path(tmp)/'paper.db')
            self.assertFalse(ledger.enter(dict(c,cost=Decimal('3')),NOW))
            accepted=[]
            for i in range(10):
                accepted.append(ledger.enter(dict(c,ticker=f'P-{i}',event_ticker=f'E-{i}'),NOW))
            self.assertTrue(any(accepted))
            self.assertFalse(all(accepted))
            self.assertLessEqual(ledger.open_cost(),Decimal('10'))
            self.assertTrue(all(Decimal(p['cost'])<=2 for p in ledger.positions()))
            ledger.close()

    def test_small_consensus_control_is_labeled_and_cannot_claim_positive_edge(self):
        e=engine()
        m=market(yes_bid_dollars='.70',yes_ask_dollars='.72',no_ask_dollars='.30',no_bid_dollars='.28')
        s=signal(p_yes=.71,entry_tier='exploratory',category='Politics',strategy_id='consensus-politics-v1',
                 decision_reason='Market consensus baseline; no demonstrated edge; small paper research sample')
        c=e.evaluate_candidate(m,s,{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW)
        self.assertIsNotNone(c)
        self.assertLessEqual(c['cost'],Decimal('2'))
        self.assertLess(c['net_margin'],0)
        self.assertEqual(c['signal']['entry_tier'],'exploratory')
        self.assertEqual(c['side'],'yes')
        self.assertIsNone(e.evaluate_candidate(m,dict(s,p_yes=.80),{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW))
        self.assertIsNone(e.evaluate_candidate(m,dict(s,category='Weather'),{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW))
