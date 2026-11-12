"""Conservative independent models and explicitly non-edge paper controls.

No orders, credentials, network transport, or ledger mutations. Parameters are
version-frozen; policy can only propose/activate future suspension, never size up.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import re
from statistics import NormalDist, stdev
from zoneinfo import ZoneInfo

API = 'https://api.elections.kalshi.com/trade-api/v2'
COINBASE = 'https://api.exchange.coinbase.com/products/'
MODEL_VERSION = 'crypto-terminal-lognormal-v1-unvalidated'
# All ten tickers individually verified via public /series/{ticker}, 2026-11-11.
# Nasdaq's official category is Financials; Technology below is an explicit
# technology-heavy-index research grouping, NOT an exact category claim.
EXPANDED_SERIES = ('KXBTC','KXBTCD','KXETH','KXETHD','KXNASDAQ100','KXCPI',
                   'KXFEDDECISION','KXFED','KXCONTROLH','KXCONTROLS','KXAPRPOTUS')
CRYPTO = {'KXBTC': ('BTC', 'Bitcoin', 'BRTI'), 'KXBTCD': ('BTC', 'Bitcoin', 'BRTI'),
          'KXETH': ('ETH', 'Ethereum', 'ERTI'), 'KXETHD': ('ETH', 'Ethereum', 'ERTI')}
CRYPTO_WARNING = ('Unvalidated zero-log-drift lognormal approximation, not a known edge. '
                  'Coinbase USD spot/daily OHLC is NOT official CF Benchmarks BRTI/ERTI '
                  '60-second average settlement; basis, rounding, jumps, and volatility '
                  'regime risk remain. Inflated volatility is uncertainty, not calibration.')


def _time(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone required')
    return value.astimezone(timezone.utc)


def _number(value):
    if isinstance(value, bool):
        raise ValueError('invalid boolean number')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('nonfinite number')
    return value


def _crypto_signal(m, fetch, now):
    asset, name, index = CRYPTO[m['event_ticker'].split('-')[0]]
    if str(m.get('custom_strike',{}).get('round_digits')) != '2':
        raise ValueError('unverified USD settlement precision')
    if re.search(r'\b(touch(?:es)?|barrier|at any time|highest|lowest)\b',m.get('rules_secondary',''),re.I):
        raise ValueError('path/barrier settlement is not a terminal contract')
    close = _time(m['close_time'])
    if not 86400 <= (close-now).total_seconds() <= 7*86400:
        raise ValueError('terminal crypto horizon must be 24h..7d')
    kind = m['strike_type']
    bounds = {k: _number(m[k]) for k in ('floor_strike','cap_strike') if m.get(k) is not None}
    if any(v <= 0 for v in bounds.values()):
        raise ValueError('USD price bounds must be positive')
    if kind == 'greater':
        condition = f"above {bounds['floor_strike']:.12g}"
    elif kind == 'less':
        condition = f"below {bounds['cap_strike']:.12g}"
    elif kind == 'between' and bounds['floor_strike'] < bounds['cap_strike']:
        condition = f"between {bounds['floor_strike']:.12g}-{bounds['cap_strike']:.12g}"
    else:
        raise ValueError('unsupported or reversed continuous price bounds')
    local = close.astimezone(ZoneInfo('America/New_York'))
    clock = f"{local.hour % 12 or 12}" + (f':{local.minute:02d}' if local.minute else '')
    clock += local.strftime(' %p %Z')
    day = f"{local.strftime('%b')} {local.day}, {local.year}"
    expected = (f"If the simple average of the sixty seconds of CF Benchmarks' {name} Real-Time Index ({index}) "
                f"before {clock} is {condition} at {clock} on {day}, then the market resolves to Yes.")
    if ' '.join(m.get('rules_primary','').split()) != expected or close.second or close.microsecond:
        raise ValueError('unverified terminal rules, expiry, source, or USD strike')
    expected_event = f"{m['event_ticker'].split('-')[0]}-{local.strftime('%y%b%d').upper()}{local.hour:02d}"
    if m['event_ticker'] != expected_event:
        raise ValueError('event expiry disagrees with terminal close/rules')
    candle_url = COINBASE+asset+'-USD/candles?granularity=86400'
    spot_url = COINBASE+asset+'-USD/ticker'
    bars, spot = fetch(candle_url), fetch(spot_url)
    completed = []
    for row in bars:
        if len(row) != 6:
            raise ValueError('invalid OHLC bar')
        t, low, high, opened, closed, vol = map(_number, row)
        if t % 86400 or not 0 < low <= min(opened, closed) <= max(opened, closed) <= high or vol <= 0:
            raise ValueError('invalid daily USD OHLC units or timestamp')
        end = datetime.fromtimestamp(t, timezone.utc)+timedelta(days=1)
        if end <= now:
            completed.append((t, closed))
    completed.sort()
    completed = completed[-60:]
    if len(completed) < 20:
        raise ValueError('need >=20 actual completed daily bars')
    if any(b[0]-a[0] != 86400 for a,b in zip(completed, completed[1:])):
        raise ValueError('daily source gap or duplicate bar')
    last_end = datetime.fromtimestamp(completed[-1][0], timezone.utc)+timedelta(days=1)
    if not 0 <= (now-last_end).total_seconds() < 86400:
        raise ValueError('stale daily source')
    spot_time, price = _time(spot['time']), _number(spot['price'])
    if not 0 <= (now-spot_time).total_seconds() <= 120 or price <= 0:
        raise ValueError('stale/future or invalid USD spot')
    if not .75 <= price/completed[-1][1] <= 1.25:
        raise ValueError('source price gap/units mismatch')
    returns = [math.log(b[1]/a[1]) for a,b in zip(completed,completed[1:])]
    sigma_daily = max(.025, stdev(returns)*1.5)
    sigma = sigma_daily*math.sqrt((close-now).total_seconds()/86400)
    distribution = NormalDist(0, sigma)
    cdf = lambda strike: distribution.cdf(math.log(strike/price))
    if kind == 'greater':
        p = 1-cdf(bounds['floor_strike'])
    elif kind == 'less':
        p = cdf(bounds['cap_strike'])
    else:
        p = cdf(bounds['cap_strike'])-cdf(bounds['floor_strike'])
    observed = now.isoformat()
    return dict(ticker=m['ticker'], event_ticker=m['event_ticker'], p_yes=max(0., min(1., p)),
                strategy_id=f'crypto-{asset.lower()}-terminal', category='Crypto', entry_tier='model',
                model_version=MODEL_VERSION, source_url=spot_url, source_observed_at=observed,
                forecast_date=close.date().isoformat(), warning=CRYPTO_WARNING,
                decision_reason='independent completed-bar volatility model; unvalidated, not a demonstrated edge',
                terminal_at=close.isoformat(), completed_bars=len(completed), sigma_daily=sigma_daily,
                sources=[dict(name='Coinbase Exchange daily USD OHLC',url=candle_url,observed_at=observed,
                              last_completed_bar_end=last_end.isoformat()),
                         dict(name='Coinbase Exchange USD ticker',url=spot_url,observed_at=observed,
                              source_time=spot['time'])])


CONTROL_REASON = 'market consensus baseline; no demonstrated edge; small paper research sample'


def _terminal_control_category(m, close):
    """Closed templates, not keyword-based eligibility. New rule forms abstain."""
    series, rules = m['event_ticker'].split('-')[0], ' '.join(m.get('rules_primary','').split())
    local = close.astimezone(ZoneInfo('America/New_York'))
    if series == 'KXNASDAQ100':
        date = f"{local.strftime('%B')} {local.day}, {local.year}"
        floor = _number(m['floor_strike'])
        if (m.get('custom_strike',{}).get('Index') != 'Nasdaq-100' or m.get('strike_type') != 'greater'
            or floor <= 0 or local.hour != 16 or local.minute or local.second or local.microsecond):
            raise ValueError('unverified Nasdaq-100 terminal contract')
        expected = f'If the end-of-day Nasdaq 100 index value on {date} is above {floor:.4f}, then the market resolves to Yes.'
        if rules == expected:
            return 'Technology'
    elif series == 'KXCPI':
        match = re.fullmatch(r'If the Consumer Price Index \(CPI\) increases by more than (-?\d+(?:\.\d+)?)% \(single-decimal\) in ([A-Za-z]+) (\d{4}), then the market resolves to Yes\.', rules)
        if match and m.get('strike_type') == 'greater' and _number(match[1]) == _number(m['floor_strike']):
            month = datetime.strptime(match[2]+' '+match[3], '%B %Y')
            release = re.search(r'data release \(([A-Za-z]+ \d{1,2}, \d{4})\)',m.get('rules_secondary',''))
            month_end = (month.replace(day=28)+timedelta(days=4)).replace(day=1)
            if (release and datetime.strptime(release[1],'%B %d, %Y').date() == local.date()
                and local.hour == 8 and local.minute == 29 and local.second == 0
                and month_end.date() <= local.date() <= (month_end+timedelta(days=40)).date()
                and m['event_ticker'] == 'KXCPI-'+month.strftime('%y%b').upper()):
                return 'Economics'
    elif series in ('KXFEDDECISION','KXFED'):
        if series == 'KXFEDDECISION':
            match = re.fullmatch(r'If the Federal Reserve does a (Hike|Cut) of ([<>]?\d+)bps on ([A-Za-z]+ \d{1,2}, \d{4}), then the market resolves to Yes\.',rules)
            if match and m.get('strike_type') == 'custom' and str(m.get('custom_strike',{}).get(match[1])) == match[2]:
                target = datetime.strptime(match[3],'%B %d, %Y')
                if (target.date() == local.date() and m['event_ticker'] == series+'-'+target.strftime('%y%b').upper()
                    and local.hour == 13 and local.minute == 59 and not local.second):
                    return 'Economics'
        else:
            match = re.fullmatch(r"If the upper bound of the target federal funds rate published on the Federal Reserve's official website is greater than (\d+(?:\.\d+)?)% following the Federal Reserve's ([A-Za-z]+ \d{1,2}, \d{4}) meeting, then the market resolves to Yes\.",rules)
            if match and m.get('strike_type') == 'greater' and _number(match[1]) == _number(m['floor_strike']):
                target = datetime.strptime(match[2],'%b %d, %Y')
                if (target.date() == local.date() and m['event_ticker'] == series+'-'+target.strftime('%y%b').upper()
                    and local.hour == 13 and local.minute == 55 and not local.second):
                    return 'Economics'
    elif series in ('KXCONTROLH','KXCONTROLS'):
        chamber = 'House' if series == 'KXCONTROLH' else 'Senate'
        match = re.fullmatch(r'If the (Democratic|Republican) party has won control of the '+chamber+r' in (\d{4}), then the market resolves to Yes\.',rules)
        if match and int(match[2]) == local.year and m['event_ticker'] == series+'-'+match[2]:
            return 'Politics'
    elif series == 'KXAPRPOTUS':
        if local.hour != 11 or local.minute or local.second or m['event_ticker'] != series+'-'+local.strftime('%y%b%d').upper():
            raise ValueError('approval-rating observation date/time mismatch')
        kind=m.get('strike_type')
        if kind=='between':
            lo,hi=_number(m['floor_strike']),_number(m['cap_strike'])
            if not 0 <= lo <= hi <= 100: raise ValueError('invalid approval percentage bounds')
            condition=f'between {lo:g} and {hi:g}'
        elif kind in ('greater','less'):
            bound=_number(m['floor_strike'] if kind=='greater' else m['cap_strike'])
            if not 0 <= bound <= 100: raise ValueError('invalid approval percentage bound')
            condition=f"{'above' if kind=='greater' else 'below'} {bound:g}"
        else: raise ValueError('unsupported approval-rating strike type')
        date=f"{local.strftime('%B')} {local.day}, {local.year}"
        expected=f"If the President's approval rating is {condition}% at 11:00 AM ET on {date} according to RealClearPolitics, then the market resolves to Yes."
        secondary=f'The market will determine at 11:00 AM ET on {date}.'
        if rules==expected and ' '.join(m.get('rules_secondary','').split())==secondary:
            return 'Politics'
    raise ValueError('unsupported independent model or unverified/changed terminal control rules')


def _control_signal(m, now):
    close = _time(m['close_time'])
    if re.search(r'\b(touch(?:es)?|barrier|at any time|highest|lowest)\b',m.get('rules_secondary',''),re.I):
        raise ValueError('path/barrier settlement is not a terminal control')
    if not 0 < (close-now).total_seconds() <= 45*86400:
        raise ValueError('control must be short-dated terminal event <=45d')
    if not 0 <= (now-_time(m['_fetched_at'])).total_seconds() <= 120:
        raise ValueError('stale/future market quote')
    bid, ask = Decimal(str(m['yes_bid_dollars'])), Decimal(str(m['yes_ask_dollars']))
    volume = _number(m.get('volume_24h_fp', m.get('volume_24h')))
    if not bid.is_finite() or not ask.is_finite() or not 0 <= bid <= ask <= 1:
        raise ValueError('invalid dollar quotes')
    mid = (bid+ask)/2
    if not Decimal('.60') <= max(mid,1-mid) <= Decimal('.85') or ask-bid > Decimal('.04') or volume < 1000:
        raise ValueError('control requires favorite-side consensus .60..85, spread <=.04, volume >=1000')
    category = _terminal_control_category(m, close)
    observed, url = now.isoformat(), API+'/markets/'+m['ticker']
    return dict(ticker=m['ticker'],event_ticker=m['event_ticker'],category=category,
                strategy_id='consensus-control-'+category.lower(),entry_tier='exploratory',p_yes=float(mid),
                model_version='consensus-control-v1-no-edge',source_url=url,source_observed_at=observed,
                decision_reason=CONTROL_REASON,
                warning='Market consensus only, not independently estimated; no demonstrated edge. '
                        'Nasdaq-100 index is not QQQ; no ETF substitution or directional news inference. '
                        'Technology is a research grouping for this technology-heavy index, not an official category.',
                sources=[dict(name='Kalshi public market consensus',url=url,observed_at=observed)])


STRATEGIES = {'crypto-btc-terminal': ('Crypto', MODEL_VERSION),
              'crypto-eth-terminal': ('Crypto', MODEL_VERSION),
              'weather-gaussian-v1-unvalidated': ('Weather','weather-gaussian-v1-unvalidated'),
              **{'consensus-control-'+c.lower(): (c, 'consensus-control-v1-no-edge')
                 for c in ('Technology','Politics','Economics')}}


def _positions(ledger):
    if ledger is None:
        return []
    if hasattr(ledger, 'positions'):
        return ledger.positions()
    if isinstance(ledger, dict):
        return list(ledger.get('positions', []))
    return list(ledger)


def strategy_policy(ledger, now):
    """Pure, event-deduplicated diagnostics; never mutate entry p or bankroll.

    Only genuinely settled positions with pre-outcome entry metadata count.
    Thirty training events plus twenty untouched holdout events are required
    before any suspension can be considered. No automatic risk increases.
    """
    now = _time(now)
    groups = {sid+'/'+cat: [] for sid,(cat,version) in STRATEGIES.items()}
    groups.update({'category/'+cat: [] for cat in ('Weather','Technology','Politics','Economics','Crypto','Other')})
    eligible = []
    for p in _positions(ledger):
        try:
            signal = p.get('signal') or json.loads(p.get('signal_json','{}'))
            sid = signal.get('strategy_id') or p.get('model_version')
            cat = signal.get('category') or STRATEGIES.get(sid,('Other',None))[0]
            if sid not in STRATEGIES or STRATEGIES[sid][0] != cat or p.get('model_version') != STRATEGIES[sid][1]:
                continue
            if p.get('status') != 'settled' or p.get('result') not in ('yes','no') or p.get('side') not in ('yes','no'):
                continue
            opened, settled = _time(p['opened_at']), _time(p['settled_at'])
            if not opened < settled <= now or not p.get('event_ticker'):
                continue
            prob, baseline, net = _number(p['p_yes']), _number(p['baseline_p_yes']), _number(p['net'])
            if not 0 <= prob <= 1 or not 0 <= baseline <= 1:
                continue
            eligible.append(dict(event=p['event_ticker'],opened=opened,settled=settled,sid=sid,cat=cat,
                                 probability=prob,baseline=baseline,net=net,outcome=int(p['result']=='yes'),
                                 loss=p['side'] != p['result']))
        except (ValueError,TypeError,KeyError,OverflowError):
            continue
    seen = set()
    for row in sorted(eligible, key=lambda r:(r['opened'],r['event'],r['sid'])):
        if row['event'] in seen:
            continue
        seen.add(row['event'])
        groups[row['sid']+'/'+row['cat']].append(row)
        groups['category/'+row['cat']].append(row)
    states = {}
    for key, rows in groups.items():
        rows.sort(key=lambda r:(r['settled'],r['event']))
        n = len(rows)
        state = dict(key=key,strategy_id=key.split('/')[0],category=key.split('/')[1],
                     status='warming_up' if n <= 30 else ('holdout' if n < 50 else 'monitoring'),
                     distinct_settled_events=n,training_events=min(n,30),holdout_events=min(max(0,n-30),20),
                     minimum_training_events=30,minimum_holdout_events=20,risk_multiplier=1,
                     parameters_frozen=True,policy_version='conservative-event-holdout-v1',
                     proposed_changes=[],notes='insufficient independent settled events; no learned edge or parameter tuning' if n < 50 else
                     'parameters remain frozen; paper diagnostics, not proof of profitability')
        if rows:
            brier = sum((r['probability']-r['outcome'])**2 for r in rows)/n
            baseline_brier = sum((r['baseline']-r['outcome'])**2 for r in rows)/n
            state.update(brier=brier,baseline_brier=baseline_brier,
                         shrunk_brier=(brier*n+baseline_brier*30)/(n+30),
                         net=sum(r['net'] for r in rows),losses=sum(r['loss'] for r in rows))
        else:
            state.update(brier=None,baseline_brier=None,shrunk_brier=None,net=0,losses=0)
        # Fixed non-overlapping 30-event discovery + 20-event holdout cohorts.
        # Once a completed cohort fails, later wins cannot silently resume it.
        for start in range(0,n-49,50):
            train, holdout = rows[start:start+30], rows[start+30:start+50]
            bad = lambda batch: sum(r['loss'] for r in batch) >= 10 and sum(r['net'] for r in batch) < 0
            if bad(train) and bad(holdout):
                base_version = STRATEGIES[key.split('/')[0]][1] if not key.startswith('category/') else 'category-guard-v1'
                change = dict(change='suspend_new_entries',apply_future_only=True,
                              previous_strategy_version=base_version,
                              new_strategy_version=base_version+'-suspended-policy-v1',
                              effective_at=holdout[-1]['settled'].isoformat(),
                              rationale='sustained net-negative training and untouched holdout; >=10 losses in each',
                              evidence=dict(training_events=30,holdout_events=20,
                                            training_net=sum(r['net'] for r in train),
                                            holdout_net=sum(r['net'] for r in holdout),
                                            training_losses=sum(r['loss'] for r in train),
                                            holdout_losses=sum(r['loss'] for r in holdout),
                                            events=[r['event'] for r in train+holdout]))
                state.update(status='paused',risk_multiplier=0,proposed_changes=[change],
                             notes='future entries suspended; original p_yes and entry versions unchanged; no automatic resume')
                break
        states[key] = state
    return states


def build_diversified_signals(markets, fetch_json, now, news=None, ledger=None):
    result = dict(signals=[], decisions=[], errors=[], strategy_states=[])
    cache, selected_categories = {}, set()
    positions = _positions(ledger)
    try:
        clock = _time(now)
        states = strategy_policy(positions, clock)
    except (ValueError,TypeError,OverflowError) as exc:
        result['errors'].append(dict(ticker=None,error=str(exc)))
        result['decisions'] = [dict(ticker=m.get('ticker'),status='abstain',rationale=str(exc)) for m in markets]
        return result
    result['strategy_states'] = list(states.values())
    used_events = {p.get('event_ticker') for p in positions}
    def fetch(url):
        if url not in cache:
            cache[url] = fetch_json(url)
        return cache[url]
    for m in markets:
        try:
            clock = _time(now)
            if m.get('status') not in ('active', 'open') or m.get('market_type') != 'binary':
                raise ValueError('not an active binary market')
            if any(m.get(k) for k in ('mve_collection_ticker','mve_selected_legs','is_combo')) or m['ticker'].startswith('KXMVE'):
                raise ValueError('combo/multivariate markets are ineligible')
            if m.get('open_time') and _time(m['open_time']) > clock:
                raise ValueError('market not yet open')
            if not m['ticker'].startswith(m['event_ticker']+'-'):
                raise ValueError('ticker/event mismatch')
            if m['event_ticker'] in used_events:
                raise ValueError('event already represented in paper ledger; one event ever')
            series = m['event_ticker'].split('-')[0]
            signal = _crypto_signal(m, fetch, clock) if series in CRYPTO else _control_signal(m, clock)
            if any(states[k]['status'] == 'paused' for k in
                   (signal['strategy_id']+'/'+signal['category'], 'category/'+signal['category'])):
                raise ValueError('strategy/category paused by settled-event holdout policy; future entries only')
            if signal['entry_tier'] == 'exploratory':
                if signal['category'] in selected_categories:
                    raise ValueError('one new control per category per scan')
                selected_categories.add(signal['category'])
            result['signals'].append(signal)
            result['decisions'].append(dict(ticker=m['ticker'],status='signal',rationale=signal['decision_reason']))
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            expected = ('control requires','control must','unsupported independent model',
                'unverified','terminal crypto horizon','event already represented','one new control',
                'combo/','not an active binary','market not yet open','strategy/category paused','ticker/event mismatch')
            kind='eligibility' if reason.startswith(expected) else 'data'
            result['errors'].append(dict(ticker=m.get('ticker'),error=reason,kind=kind))
            result['decisions'].append(dict(ticker=m.get('ticker'),status='abstain',rationale=reason))
    return result
