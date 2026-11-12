"""Synthetic fixtures only; no fixture data is used by the live CLI."""
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)


def engine():
    spec = importlib.util.spec_from_file_location('engine', ROOT / 'engine.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def market(**changes):
    data = {'ticker': 'WX-A', 'event_ticker': 'WX-E', 'series_ticker': 'WX',
            'status': 'active', 'close_time': (NOW + timedelta(hours=8)).isoformat(),
            'yes_ask_dollars': '0.40', 'yes_bid_dollars': '0.36',
            'no_ask_dollars': '0.64', 'no_bid_dollars': '0.60',
            'yes_ask_size_fp': '100.0', 'yes_bid_size_fp': '100.0',
            'volume_24h_fp': '1000.0'}
    data.update(changes)
    return data


def signal(**changes):
    data = {'ticker': 'WX-A', 'event_ticker': 'WX-E', 'p_yes': 0.85,
            'model_version': 'fixture-v1', 'forecast_mean_f': 90,
            'sigma_f': 3, 'source_url': 'https://example.test/forecast',
            'source_observed_at': NOW.isoformat(), 'warning': '',
            'forecast_date': '2026-07-01'}
    data.update(changes)
    return data


class EngineTests(unittest.TestCase):
    def test_known_settlement_blocks_first_entry_even_if_listing_is_stale_open(self):
        e=engine()
        with tempfile.TemporaryDirectory() as tmp:
            ledger=e.Ledger(Path(tmp)/'paper.db')
            c=e.evaluate_candidate(market(),signal(),{'fee_type':'quadratic','fee_multiplier':1},NOW,NOW)
            ledger.settle(market(status='settled',result='yes'),NOW)
            self.assertFalse(ledger.enter(c,NOW))
            self.assertFalse(ledger.enter(dict(c,ticker='OTHER-BUCKET'),NOW))
            self.assertFalse(ledger.observe_signal(signal(),market(),NOW))
            self.assertEqual(ledger.forecasts(),[])
            self.assertEqual(ledger.cash(),Decimal('1000'))
            self.assertEqual(ledger.positions(),[])
            ledger.close()

    def test_stale_or_future_forecast_is_not_executable_even_with_fresh_quote(self):
        e=engine()
        fees={'fee_type':'quadratic','fee_multiplier':1}
        for observed in (NOW-timedelta(minutes=5),NOW+timedelta(seconds=1)):
            with self.subTest(observed=observed):
                self.assertIsNone(e.evaluate_candidate(market(),signal(source_observed_at=observed.isoformat()),fees,NOW,NOW))

    def test_scan_records_inputs_forecasts_and_paper_entry_without_duplicates(self):
        e = engine()
        self.assertTrue(callable(getattr(e, 'scan', None)), 'live scan orchestration missing')
        from test_signals import market as weather_market, om, nws, NOW as weather_now
        m = dict(weather_market('greater', 60), yes_ask_dollars='0.40', yes_bid_dollars='0.36',
                 no_ask_dollars='0.64', no_bid_dollars='0.60', yes_ask_size_fp='100',
                 yes_bid_size_fp='100', volume_24h_fp='1000')
        def fetch(url):
            if 'open-meteo.com' in url: return om()
            if 'forecast.weather.gov' in url: return nws()
            if '/series/' in url: return {'series': {'fee_type': 'quadratic', 'fee_multiplier': 1}}
            if 'series_ticker=KXHIGHNY' in url: return {'markets': [m], 'cursor': ''}
            if '/markets/' in url: return {'market': m}
            return {'markets': [], 'cursor': ''}
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'data/paper.db'
            r = e.scan(db, fetch_json=fetch, now=weather_now, no_news=True)
            self.assertEqual(r['counts']['markets'], 1)
            self.assertEqual(r['counts']['forecasts'], 1)
            self.assertEqual(r['counts']['new_positions'], 1)
            again = e.scan(db, fetch_json=fetch, now=weather_now, no_news=True)
            self.assertEqual(again['counts']['new_positions'], 0)
            ledger = e.Ledger(db)
            self.assertEqual(len(ledger.positions()), 1)
            self.assertEqual(len(ledger.forecasts()), 2)
            self.assertGreater(ledger.db.execute('SELECT count(*) FROM snapshots').fetchone()[0], 0)
            self.assertEqual(ledger.db.execute('SELECT count(*) FROM runs').fetchone()[0], 2)
            ledger.close()

    def test_quote_sizes_fees_slippage_and_net_edge_gate(self):
        self.assertTrue((ROOT / 'engine.py').exists(), 'engine implementation missing')
        e = engine()
        c = e.evaluate_candidate(market(), signal(),
                                 {'fee_type': 'quadratic', 'fee_multiplier': 1}, NOW, NOW)
        self.assertEqual(c['side'], 'yes')
        self.assertEqual(c['entry_price'], Decimal('0.41'))
        self.assertLessEqual(c['cost'], Decimal('20'))
        self.assertEqual(c['qty'], 46)
        self.assertEqual(c['fee'], Decimal('0.78'))
        self.assertGreaterEqual(c['net_margin'], Decimal('.03'))
        titled=e.evaluate_candidate(market(title='Will the maximum temperature be below 60?', rules_primary='Fixture rule'),
                                   signal(), {'fee_type':'quadratic','fee_multiplier':2}, NOW,NOW)
        self.assertEqual(titled['signal']['market_title'],'Will the maximum temperature be below 60?')
        self.assertEqual(titled['signal']['fee_multiplier'],'2')
        self.assertEqual(titled['signal']['rules_primary'],'Fixture rule')
        self.assertIsNone(e.evaluate_candidate(market(yes_ask_size_fp=None), signal(),
                          {'fee_type': 'quadratic', 'fee_multiplier': 1}, NOW, NOW))

    def test_durable_cash_reservation_and_one_exposure_per_event(self):
        e = engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'paper.db'
            ledger = e.Ledger(path)
            c = e.evaluate_candidate(market(), signal(),
                {'fee_type': 'quadratic', 'fee_multiplier': 1}, NOW, NOW)
            self.assertTrue(ledger.enter(c, NOW))
            self.assertFalse(ledger.enter(c, NOW))
            same_event = dict(c, ticker='WX-B')
            self.assertFalse(ledger.enter(same_event, NOW))
            self.assertEqual(ledger.cash(), Decimal('1000') - c['cost'])
            for i in range(10):
                ledger.enter(dict(c, ticker=f'T{i}', event_ticker=f'E{i}'), NOW)
            self.assertLessEqual(ledger.open_cost(), Decimal('100'))
            saved = ledger.positions()[0]
            self.assertEqual(Decimal(saved['p_yes']), Decimal('.85'))
            self.assertEqual(saved['model_version'], 'fixture-v1')
            ledger.close()
            ledger = e.Ledger(path)
            self.assertLess(ledger.cash(), Decimal('1000'))
            self.assertEqual(ledger.positions()[0]['signal']['forecast_mean_f'], 90)
            ledger.close()

    def test_forecasts_preserved_without_trade_and_only_official_binary_settlement(self):
        e = engine()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = e.Ledger(Path(tmp) / 'paper.db')
            ledger.observe_signal(signal(), market(), NOW)
            ledger.observe_signal(signal(p_yes=.10), market(), NOW + timedelta(minutes=2))
            self.assertEqual(len(ledger.forecasts()), 2)
            self.assertEqual(ledger.calibration()['n'], 0)
            c = e.evaluate_candidate(market(), signal(),
                {'fee_type': 'quadratic', 'fee_multiplier': 1}, NOW, NOW)
            ledger.enter(c, NOW)
            self.assertFalse(ledger.settle(market(status='closed', result='yes'), NOW))
            self.assertFalse(ledger.settle(market(status='finalized', result='0.5'), NOW))
            self.assertEqual(ledger.positions()[0]['status'], 'open')
            self.assertTrue(ledger.settle(market(status='finalized', result='yes'), NOW))
            self.assertFalse(ledger.settle(market(status='finalized', result='yes'), NOW))
            self.assertEqual(ledger.cash(), Decimal('1000') - c['cost'] + c['qty'])
            stats = ledger.calibration()
            self.assertEqual(stats['n'], 1)
            self.assertEqual(stats['brier'], Decimal('.0225'))
            self.assertEqual(stats['baseline_brier'], Decimal('.3844'))
            ledger.observe_signal(signal(ticker='OTHER', event_ticker='OTHER-E'),
                                  market(ticker='OTHER'), NOW)
            ledger.settle(market(ticker='OTHER', status='settled', result='no'), NOW)
            self.assertEqual(ledger.calibration()['n'], 2)
            self.assertEqual(len(ledger.positions()), 1)
            self.assertFalse(ledger.enter(c, NOW))
            ledger.close()


if __name__ == '__main__':
    unittest.main()
