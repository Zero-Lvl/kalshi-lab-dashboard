"""Deterministic injected-data tests; fixture prices are not live evidence."""
from datetime import datetime, timedelta, timezone
import copy
import math
from statistics import NormalDist, stdev

import unittest


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(v) for k, v in globals().items()
                              if k.startswith('test_') and callable(v))

NOW = datetime(2026, 11, 11, 19, 0, tzinfo=timezone.utc)


def crypto_market(asset='BTC', kind='greater', floor=74000.25, cap=76000.75):
    name, index = ('Bitcoin', 'BRTI') if asset == 'BTC' else ('Ethereum', 'ERTI')
    event = f'KX{asset}-26NOV1317'
    condition = {'greater': f'above {floor:.12g}', 'less': f'below {cap:.12g}',
                 'between': f'between {floor:.12g}-{cap:.12g}'}[kind]
    return dict(ticker=event+'-TEST', event_ticker=event, status='active', market_type='binary',
                close_time='2026-11-13T22:00:00Z', strike_type=kind,
                floor_strike=floor, cap_strike=cap, custom_strike={'round_digits': '2'},
                rules_primary=f"If the simple average of the sixty seconds of CF Benchmarks' {name} Real-Time Index ({index}) before 5 PM EST is {condition} at 5 PM EST on Nov 13, 2026, then the market resolves to Yes.",
                rules_secondary='The official and final value is the average of these prices.')


def source_data(asset='BTC'):
    midnight = NOW.replace(hour=0)
    bars = []
    for i in range(31):
        start = midnight-timedelta(days=31-i)
        price = (74000 if asset == 'BTC' else 2500)*math.exp(.018*math.sin(i))
        bars.append([int(start.timestamp()), price*.97, price*1.03, price, price, 120])
    return bars, dict(price='74200' if asset == 'BTC' else '2510', time=NOW.isoformat())


def fetcher(asset='BTC', mutate=None):
    bars, spot = source_data(asset)
    if mutate:
        mutate(bars, spot)
    def fetch(url):
        assert f'/{asset}-USD/' in url
        return copy.deepcopy(bars if '/candles?' in url else spot)
    return fetch


def control_market(series='KXNASDAQ100', category='Technology'):
    event = series+'-26NOV12H1600'
    return dict(ticker=event+'-T25000', event_ticker=event, category=category,
                status='active', market_type='binary', close_time='2026-11-12T21:00:00Z',
                _fetched_at=NOW.isoformat(), volume_24h_fp='1500',
                yes_bid_dollars='0.70', yes_ask_dollars='0.72',
                strike_type='greater', floor_strike=25000, custom_strike={'Index':'Nasdaq-100'},
                rules_primary='If the end-of-day Nasdaq 100 index value on November 12, 2026 is above 25000.0000, then the market resolves to Yes.')


def test_political_rcp_numeric_control_validates_date_and_only_records_consensus():
    from strategies import build_diversified_signals
    m=control_market('KXAPRPOTUS','Politics')
    m.update(event_ticker='KXAPRPOTUS-26NOV13',ticker='KXAPRPOTUS-26NOV13-38.8',
        close_time='2026-11-13T16:00:00Z',strike_type='between',floor_strike=38.7,cap_strike=38.9,
        yes_bid_dollars='.33',yes_ask_dollars='.34',
        rules_primary="If the President's approval rating is between 38.7 and 38.9% at 11:00 AM ET on November 13, 2026 according to RealClearPolitics, then the market resolves to Yes.",
        rules_secondary='The market will determine at 11:00 AM ET on November 13, 2026.')
    result=build_diversified_signals([m],lambda u:None,NOW)
    assert len(result['signals'])==1,result
    s=result['signals'][0]
    assert s['category']=='Politics' and s['entry_tier']=='exploratory' and s['p_yes']==.335
    assert 'no demonstrated edge' in s['decision_reason']
    changed=dict(m,rules_primary=m['rules_primary'].replace('November 13','November 14'))
    assert not build_diversified_signals([changed],lambda u:None,NOW)['signals']


def test_control_is_yes_mid_consensus_not_an_independent_model():
    from strategies import build_diversified_signals
    r = build_diversified_signals([control_market()], lambda u: (_ for _ in ()).throw(AssertionError(u)), NOW)
    assert len(r['signals']) == 1, r
    s = r['signals'][0]
    assert s['p_yes'] == .71 and s['entry_tier'] == 'exploratory'
    assert s['strategy_id'] == 'consensus-control-technology'
    assert s['category'] == 'Technology'
    assert s['decision_reason'] == 'market consensus baseline; no demonstrated edge; small paper research sample'
    assert 'not independently estimated' in s['warning']
    assert 'QQQ' in s['warning']
    assert s['sources'][0]['name'] == 'Kalshi public market consensus'
    assert r['decisions'][0]['rationale'] == s['decision_reason']


def cpi_market():
    m = control_market('KXCPI', 'Economics')
    m.update(event_ticker='KXCPI-26NOV', ticker='KXCPI-26NOV-T0.2',
             close_time='2026-12-10T13:29:00Z', floor_strike=.2,
             rules_primary='If the Consumer Price Index (CPI) increases by more than 0.2% (single-decimal) in November 2026, then the market resolves to Yes.',
             rules_secondary='The market will always close at 8:29 AM ET on the scheduled day of the data release (December 10, 2026).')
    return m


def politics_market():
    m = control_market('KXCONTROLH', 'Elections')
    m.update(event_ticker='KXCONTROLH-2026', ticker='KXCONTROLH-2026-D',
             close_time='2026-12-20T15:00:00Z', strike_type='custom',
             rules_primary='If the Democratic party has won control of the House in 2026, then the market resolves to Yes.')
    return m


def test_controls_are_capped_one_per_category_and_never_repeat_a_ledger_event():
    from strategies import build_diversified_signals
    ms = [control_market(), cpi_market(), politics_market()]
    duplicates = [dict(m, ticker=m['ticker']+'-OTHER') for m in ms]
    r = build_diversified_signals(ms+duplicates, lambda u: None, NOW)
    assert len(r['signals']) == 3, r
    assert {s['category'] for s in r['signals']} == {'Technology','Politics','Economics'}
    assert all(s['p_yes'] == .71 and s['entry_tier']=='exploratory' for s in r['signals'])
    assert len(r['decisions']) == 6
    class Ledger:
        def positions(self, status=None):
            return [dict(event_ticker=m['event_ticker'], status='settled') for m in ms]
    r = build_diversified_signals(ms, lambda u: None, NOW, ledger=Ledger())
    assert not r['signals'], r
    assert all('event' in d['rationale'] for d in r['decisions'])


def settled_position(i, *, strategy='crypto-btc-terminal', category='Crypto',
                     p=.8, baseline=.65, result='no', net='-1', status='settled'):
    return dict(event_ticker=f'EVENT-{i}',ticker=f'EVENT-{i}-T1',status=status,
                p_yes=str(p),baseline_p_yes=str(baseline),result=result,side='yes',net=net,
                opened_at=(NOW-timedelta(days=60)).isoformat(),
                settled_at=(NOW-timedelta(days=55-i)).isoformat(),
                model_version='crypto-terminal-lognormal-v1-unvalidated',
                signal={'strategy_id':strategy,'category':category})


def test_legacy_weather_entries_participate_in_conservative_learning_policy():
    from strategies import strategy_policy
    rows=[dict(settled_position(i),model_version='weather-gaussian-v1-unvalidated',signal={}) for i in range(50)]
    state=strategy_policy(rows,NOW)['weather-gaussian-v1-unvalidated/Weather']
    assert state['status']=='paused' and state['distinct_settled_events']==50
    assert state['risk_multiplier']==0
    assert all(p['signal']=={} for p in rows)


def test_policy_is_pure_warms_up_and_counts_distinct_settled_events_only():
    from strategies import strategy_policy, build_diversified_signals
    entries = [settled_position(1),settled_position(1),settled_position(2,status='open'),
               dict(settled_position(3), settled_at=(NOW+timedelta(days=1)).isoformat())]
    before = copy.deepcopy(entries)
    states = strategy_policy(entries, NOW)
    s = states['crypto-btc-terminal/Crypto']
    assert s['status'] == 'warming_up' and s['distinct_settled_events'] == 1
    assert s['training_events'] == 1 and s['holdout_events'] == 0
    assert s['minimum_training_events'] == 30 and s['minimum_holdout_events'] == 20
    assert s['risk_multiplier'] == 1 and not s['proposed_changes']
    assert s['parameters_frozen'] and 'insufficient' in s['notes']
    assert entries == before
    assert states == strategy_policy(entries, NOW)
    assert states['category/Crypto']['distinct_settled_events'] == 1
    r = build_diversified_signals([], lambda u: None, NOW)
    assert len(r['strategy_states']) >= 5
    assert not r['signals'] and not r['errors']


def test_policy_suspends_only_after_training_and_holdout_and_never_rewrites_entries():
    from strategies import strategy_policy, build_diversified_signals
    rows = [settled_position(i) for i in range(50)]
    before = copy.deepcopy(rows)
    for n in (10,30,49):
        state = strategy_policy(rows[:n],NOW)['crypto-btc-terminal/Crypto']
        assert state['status'] != 'paused' and state['risk_multiplier'] == 1
    state = strategy_policy(rows,NOW)['crypto-btc-terminal/Crypto']
    assert state['status'] == 'paused' and state['risk_multiplier'] == 0
    assert state['brier'] > state['baseline_brier']
    assert state['baseline_brier'] < state['shrunk_brier'] < state['brier']
    change = state['proposed_changes'][0]
    assert change['apply_future_only'] and change['new_strategy_version'] != change['previous_strategy_version']
    assert change['change'] == 'suspend_new_entries'
    assert change['evidence']['training_events'] == 30 and change['evidence']['holdout_events'] == 20
    r = build_diversified_signals([crypto_market()],fetcher(),NOW,ledger=rows)
    assert not r['signals'] and 'paused' in r['decisions'][0]['rationale']
    assert rows == before
    wins = [dict(settled_position(i), settled_at=(NOW-timedelta(hours=1)).isoformat(),
                 net='1',result='yes') for i in range(50,60)]
    later = strategy_policy(rows+wins,NOW)['crypto-btc-terminal/Crypto']
    assert later['status'] == 'paused'  # no automatic resume after a few wins
    winners = [dict(p,net='1',result='yes') for p in rows]
    winstate = strategy_policy(winners,NOW)['crypto-btc-terminal/Crypto']
    assert winstate['risk_multiplier'] == 1 and not winstate['proposed_changes']


def test_combo_future_open_and_naive_clock_abstain_without_forced_trades():
    from strategies import build_diversified_signals
    variants = [dict(control_market(),mve_collection_ticker='COMBO'),
                dict(control_market(),mve_selected_legs=[{'ticker':'LEG'}]),
                dict(control_market(),open_time=(NOW+timedelta(hours=1)).isoformat()),
                dict(crypto_market(),open_time=(NOW+timedelta(hours=1)).isoformat())]
    for m in variants:
        r = build_diversified_signals([m],fetcher(),NOW)
        assert not r['signals'] and r['errors'], (m,r)
    r = build_diversified_signals([crypto_market()],fetcher(),NOW.replace(tzinfo=None))
    assert not r['signals'] and 'timezone' in r['errors'][0]['error']
    assert not build_diversified_signals([],fetcher(),NOW)['signals']


def test_crypto_abstains_on_wrong_units_rules_expiry_and_source_health():
    from strategies import build_diversified_signals
    market_changes = [dict(custom_strike={'round_digits':'0'}),
                      dict(rules_secondary='Resolves if the price touches the barrier at any time.'),
                      dict(close_time='2026-11-13T21:00:00Z'),
                      dict(event_ticker='KXBTC-26NOV1316',ticker='KXBTC-26NOV1316-TEST'),
                      dict(floor_strike=float('nan')),dict(floor_strike=-1),
                      dict(strike_type='between',floor_strike=76000,cap_strike=74000),
                      dict(rules_primary='If Bitcoin touches $75000 before Friday, resolves Yes.'),
                      dict(status='settled'),dict(market_type='scalar')]
    for change in market_changes:
        r = build_diversified_signals([dict(crypto_market(),**change)],fetcher(),NOW)
        assert not r['signals'] and r['errors'], (change,r)
    mutations = [lambda bars,spot: bars.pop(8),
                 lambda bars,spot: bars.__delitem__(slice(0,15)),
                 lambda bars,spot: bars.append(copy.deepcopy(bars[-1])),
                 lambda bars,spot: spot.update(time=(NOW-timedelta(seconds=121)).isoformat()),
                 lambda bars,spot: spot.update(time=(NOW+timedelta(seconds=1)).isoformat()),
                 lambda bars,spot: spot.update(time='2026-11-11T19:00:00'),
                 lambda bars,spot: spot.update(price='NaN'),
                 lambda bars,spot: spot.update(price='742'),
                 lambda bars,spot: bars[0].__setitem__(4,float('nan')),
                 lambda bars,spot: [row.__setitem__(0,row[0]-86400*2) for row in bars]]
    for mutate in mutations:
        r = build_diversified_signals([crypto_market()],fetcher(mutate=mutate),NOW)
        assert not r['signals'] and r['errors'], r


def test_verified_expanded_series_and_fed_terminal_consensus():
    from strategies import EXPANDED_SERIES, build_diversified_signals
    assert isinstance(EXPANDED_SERIES,tuple) and len(EXPANDED_SERIES) == len(set(EXPANDED_SERIES)) <= 20
    assert {'KXBTC','KXBTCD','KXETH','KXETHD','KXNASDAQ100','KXCPI','KXFEDDECISION','KXFED','KXCONTROLH','KXCONTROLS','KXAPRPOTUS'} == set(EXPANDED_SERIES)
    m = control_market('KXFEDDECISION','Economics')
    m.update(event_ticker='KXFEDDECISION-26DEC',ticker='KXFEDDECISION-26DEC-H25',
             close_time='2026-12-09T18:59:00Z',strike_type='custom',custom_strike={'Hike':'25'},
             rules_primary='If the Federal Reserve does a Hike of 25bps on December 9, 2026, then the market resolves to Yes.')
    r = build_diversified_signals([m],lambda u: None,NOW)
    assert len(r['signals']) == 1, r
    assert r['signals'][0]['category'] == 'Economics' and r['signals'][0]['p_yes'] == .71
    m['rules_primary'] = m['rules_primary'].replace('December 9','December 10')
    assert not build_diversified_signals([m],lambda u: None,NOW)['signals']
    m.update(event_ticker='KXFED-26DEC',ticker='KXFED-26DEC-T4.00',strike_type='greater',floor_strike=4,
             close_time='2026-12-09T18:55:00Z',
             rules_primary="If the upper bound of the target federal funds rate published on the Federal Reserve's official website is greater than 4.00% following the Federal Reserve's Dec 9, 2026 meeting, then the market resolves to Yes.")
    r = build_diversified_signals([m],lambda u: None,NOW)
    assert len(r['signals']) == 1, r


def test_control_gates_are_strict_and_news_does_not_create_probability():
    from strategies import build_diversified_signals
    invalid = [dict(yes_bid_dollars='.58',yes_ask_dollars='.60'),
               dict(yes_bid_dollars='.85',yes_ask_dollars='.87'),
               dict(yes_bid_dollars='.68',yes_ask_dollars='.73'),
               dict(volume_24h_fp='999.99'),dict(yes_bid_dollars='NaN'),
               dict(yes_bid_dollars='70',yes_ask_dollars='72'),
               dict(_fetched_at=(NOW-timedelta(seconds=121)).isoformat()),
               dict(_fetched_at=(NOW+timedelta(seconds=1)).isoformat()),
               dict(close_time=(NOW+timedelta(days=46)).isoformat()),
               dict(rules_secondary='If price touches the barrier at any time, Yes.'),
               dict(custom_strike={'Index':'QQQ'}),
               dict(event_ticker='KXPERSON-26NOV',ticker='KXPERSON-26NOV-YES',category='Politics')]
    for change in invalid:
        r = build_diversified_signals([dict(control_market(),**change)],lambda u:None,NOW)
        assert not r['signals'] and r['decisions'][0]['status']=='abstain', (change,r)
    news = dict(matches=[dict(ticker=control_market()['ticker'],relevance='strong',sentiment='bullish')])
    s = build_diversified_signals([control_market()],lambda u:None,NOW,news=news)['signals'][0]
    assert s['p_yes'] == .71 and s['decision_reason'].startswith('market consensus baseline')
    for bid,ask in [('.59','.61'),('.83','.87')]:
        assert build_diversified_signals([dict(control_market(),yes_bid_dollars=bid,yes_ask_dollars=ask)],lambda u:None,NOW)['signals']


def test_continuous_price_precision_and_probability_math():
    from strategies import build_diversified_signals
    bars, spot = source_data()
    sigma = max(.025, stdev([math.log(b[4]/a[4]) for a,b in zip(bars,bars[1:])])*1.5)
    scale = sigma*math.sqrt(51/24)
    cdf = lambda x: NormalDist().cdf(math.log(x/float(spot['price']))/scale)
    for kind, expected in [('greater', 1-cdf(74000.25)), ('less', cdf(76000.75)),
                           ('between', cdf(76000.75)-cdf(74000.25))]:
        m = crypto_market(kind=kind)
        r = build_diversified_signals([m], fetcher(), NOW)
        assert len(r['signals']) == 1, r
        assert abs(r['signals'][0]['p_yes']-expected) < 1e-12


def test_btc_independent_terminal_model_uses_actual_completed_bars():
    from strategies import build_diversified_signals
    result = build_diversified_signals([crypto_market()], fetcher(), NOW)
    assert len(result['signals']) == 1, result
    s = result['signals'][0]
    assert s['category'] == 'Crypto' and s['entry_tier'] == 'model'
    assert s['strategy_id'] == 'crypto-btc-terminal'
    assert 0 < s['p_yes'] < 1
    assert s['completed_bars'] == 31
    assert s['source_observed_at'] == NOW.isoformat()
    assert len(s['sources']) == 2
    assert '60-second' in s['warning'] and 'Coinbase' in s['warning']
    assert 'unvalidated' in s['model_version']
    assert result['errors'] == []
    assert result['decisions'][0]['status'] == 'signal'
