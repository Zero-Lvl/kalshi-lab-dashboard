"""Auditable reports: realized settlement is never an open mark or simulation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from engine import decimal, dumps, timestamp, INITIAL_CASH, trading_fee


def money(value):
    return f'${decimal(value):,.2f}'


def safe(value, limit=160):
    return ' '.join(str(value).replace('@','@\u200b').replace('`','').split())[:limit]


def rank_watchlist(markets, matches, previous_markets=()):
    import math
    from collections import Counter
    counts = Counter(m['ticker'] for m in matches)
    previous = {m['ticker']:m for m in previous_markets}
    ranked = []
    for m in markets:
        try:
            bid,ask = decimal(m['yes_bid_dollars']),decimal(m['yes_ask_dollars'])
            vol = decimal(m.get('volume_24h_fp','0'))
            if m.get('status') not in ('active','open') or not 0 <= bid <= ask <= 1 or ask-bid > Decimal('.20') or vol < 100:
                continue
            midpoint = (bid+ask)/2
            change = None
            old = previous.get(m['ticker'])
            if old:
                ob,oa = decimal(old['yes_bid_dollars']),decimal(old['yes_ask_dollars'])
                if 0 <= ob <= oa <= 1: change = str(midpoint-(ob+oa)/2)
            ranked.append({'ticker':m['ticker'],'title':m.get('title',''),
                           'event_ticker':m['event_ticker'],'close_time':m.get('close_time'),
                           'yes_bid':str(bid),'yes_ask':str(ask),'midpoint':str(midpoint),
                           'mid_change':change,'previous_observed_at':old.get('_fetched_at') if old else None,
                           'observed_at':m.get('_fetched_at'),'news_count':counts[m['ticker']],
                           'volume_24h':str(vol),'score':counts[m['ticker']]*3+math.log1p(float(vol))-float(ask-bid)*10,
                           'interpretation':'topic/liquidity research priority, not independent trading edge or causal news correlation'})
        except (KeyError,ValueError,TypeError,InvalidOperation):
            continue
    return sorted(ranked,key=lambda row:(-row['score'],row['ticker']))[:30]


def simulate(positions, paths=5000, seed=20261111):
    """Model-implied settlement scenarios, not historical or realized returns."""
    import random
    opened = [p for p in positions if p['status']=='open']
    if len({p['event_ticker'] for p in opened}) != len(opened):
        raise ValueError('correlated duplicate event positions not supported')
    if not 1 <= paths <= 100000:
        raise ValueError('invalid path count')
    specs = []
    for p in opened:
        py = decimal(p['p_yes'])
        if not 0 <= py <= 1: raise ValueError('invalid probability')
        specs.append((float(py if p['side']=='yes' else 1-py),int(p['qty']),decimal(p['cost'])))
    rng = random.Random(seed)
    samples = sorted(sum((Decimal(qty if rng.random()<prob else 0)-cost for prob,qty,cost in specs),Decimal(0))
                     for _ in range(paths))
    return {'positions':len(opened),'paths':paths,'seed':seed,
            'p05':str(samples[int((paths-1)*.05)]),'p50':str(samples[int((paths-1)*.5)]),
            'p95':str(samples[int((paths-1)*.95)]),
            'mean':str((sum(samples,Decimal(0))/paths).quantize(Decimal('.01'))),
            'assumption':'independent events; immutable entry probabilities; uncalibrated model, not actual profit'}


def make_report(ledger, run, now=None):
    now = now or datetime.now(timezone.utc)
    r = dict(run)
    markets = {m['ticker']:m for m in r.get('markets',[])}
    positions = ledger.positions()
    for p in positions:
        cost = decimal(p['cost'])
        p['potential_net_win'], p['potential_net_loss'] = str(Decimal(p['qty'])-cost), str(-cost)
        p['mark_net'] = None
        m = markets.get(p['ticker'])
        p['market_title'] = p['signal'].get('market_title') or (m or {}).get('title') or p['ticker']
        if p['market_title'] == p['ticker'] and p['status']=='settled':
            import json
            stored = ledger.db.execute('SELECT market_json FROM settlements WHERE ticker=?',(p['ticker'],)).fetchone()
            if stored: p['market_title'] = json.loads(stored[0]).get('title') or p['ticker']
        # A quoted bid is only a liquidity-capped hypothetical liquidation, not a fill.
        if m and p['status'] == 'open' and m.get('status') in ('active','open'):
            try:
                key = 'yes_bid_dollars' if p['side']=='yes' else 'no_bid_dollars'
                size_key = 'yes_bid_size_fp' if p['side']=='yes' else 'yes_ask_size_fp'
                bid = decimal(m[key])
                age = (now-timestamp(m['_fetched_at'])).total_seconds()
                multiplier = decimal(p['signal'].get('fee_multiplier',1))
                if 0 < bid < 1 and 0 <= age <= 7200 and decimal(m[size_key]) >= p['qty']:
                    exit_price = max(Decimal(0),bid-Decimal('.01'))
                    p['mark_net'] = str(exit_price*p['qty']-trading_fee(p['qty'],exit_price,multiplier)-cost)
            except (KeyError,ValueError,TypeError,InvalidOperation):
                pass
    settled = [p for p in positions if p['status']=='settled']
    daily = [p for p in settled if now-timedelta(hours=24) <= timestamp(p['settled_at']) <= now]
    net = sum((decimal(p['net']) for p in settled),Decimal(0))
    r['summary'] = {'initial_cash':str(INITIAL_CASH), 'cash':str(ledger.cash()),
                    'open_cost':str(ledger.open_cost()), 'realized_net':str(net),
                    'open_positions':len(positions)-len(settled), 'settled':len(settled),
                    'wins':sum(p['side']==p['result'] for p in settled),
                    'losses':sum(p['side']!=p['result'] for p in settled),
                    'daily_settled':len(daily),
                    'daily_net':str(sum((decimal(p['net']) for p in daily),Decimal(0))),
                    'forecast_observations':len(ledger.forecasts()),
                    'calibration':ledger.calibration(), 'trade_calibration':ledger.calibration(trades=True)}
    r['positions'], r['daily_results'] = positions, daily
    previous = []
    try:
        row = ledger.db.execute('SELECT report_json FROM runs WHERE id < ? AND completed_at IS NOT NULL ORDER BY id DESC LIMIT 1',
                                (run.get('run_id',0),)).fetchone()
        if row:
            import json
            previous = json.loads(row[0]).get('markets',[])
    except Exception as exc:
        # Older fixture/standalone ledgers have no scan table.
        import sqlite3
        if not isinstance(exc,sqlite3.OperationalError): raise
    r['watchlist'] = rank_watchlist(r.get('markets',[]),r.get('news',{}).get('matches',[]),previous)
    r['generated_at'] = now.isoformat()
    r['warnings'] = [
        'Unvalidated weather model; no demonstrated profitability. NYC Central Park, Chicago Midway, Denver, LAX and Austin airport daily highs modeled.',
        'News uses RSS headlines/descriptions; matches mean topic relevance, not causality or directional evidence.',
        'Estimated 0.07 quadratic ceil-cent fees plus $0.01/share adverse entry adjustment; simulated fills only.',
        'Coverage is bounded, not every Kalshi market. Final settlement can lag market close.',
    ]
    return r


def daily_text(r):
    """Full daily outcome lines as text; works without Discord file attachments."""
    overview = dict(r,positions=[],daily_results=[])
    lines = [report_text(overview)]
    periods = r.get('public_export',{}).get('periods',{})
    if periods:
        lines.append('\n**Calendar realized P&L · Pacific time**')
        lines.append(' | '.join(label+' '+money(periods[key]['realized_pnl'])
            for key,label in (('day','Day'),('week','Week'),('month','Month')) if key in periods))
    lines.append('\n**Every paper result settled in the last 24 hours**')
    results = r.get('daily_results',[])
    if not results: lines.append('None yet; open positions are not wins or losses.')
    for p in results:
        result = 'WIN' if p['side']==p['result'] else 'LOSS'
        lines.append(f"\n{safe(p.get('market_title',p['ticker']),115)}")
        lines.append(f"- {result} {safe(p['ticker'],70)}: {p['side'].upper()} ×{p['qty']} @ {money(p['entry_price'])}; fee {money(p['fee'])}; payout {money(p['payout'])}; net {money(p['net'])}.")
    lines.append('\n**Every open paper position**')
    for p in r.get('positions',[]):
        if p['status']=='open':
            lines.append(f"\n{safe(p.get('market_title',p['ticker']),115)}")
            lines.append(f"- {safe(p['ticker'],70)}: {p['side'].upper()} ×{p['qty']} @ {money(p['entry_price'])}; cost incl. fee {money(p['cost'])}; if win +{money(p['potential_net_win'])}, if lose {money(p['potential_net_loss'])}.")
    return '\n'.join(lines)


def report_text(r):
    s, c = r['summary'], r.get('counts',{})
    lines = ['**Kalshi Lab — PAPER ONLY**',
             f"Data: {safe(r.get('observed_at','no completed scan'),40)}",
             f"Cash {money(s['cash'])} | open cost {money(s['open_cost'])} ({s['open_positions']} positions)",
             f"Realized net {money(s['realized_net'])} | {s['wins']} wins / {s['losses']} losses",
             f"Settled last 24h: {s['daily_settled']} | net {money(s['daily_net'])}",
             f"{'Settlement/quote refresh' if r.get('mode')=='settlement_refresh' else 'Entry scan'}: {c.get('markets',0)} markets; {c.get('forecasts',0)} estimates; {c.get('new_positions',0)} new paper entries."]
    if r.get('mode')=='settlement_refresh':
        lines.append('New-position selection runs twice daily; this refresh cannot open positions.')
    else:
        lines.append(f"News: {c.get('articles',0)} items, {c.get('news_matches',0)} topic matches; {c.get('feeds_ok',0)}/4 feeds.")
    for p in r.get('daily_results',[])[-3:]:
        lines.append(f"{'WIN' if p['side']==p['result'] else 'LOSS'} {safe(p['ticker'],45)} {p['side'].upper()} ×{p['qty']}: net {money(p['net'])}")
    for p in [p for p in r.get('positions',[]) if p['status']=='open'][:5]:
        lines.append(f"Open {safe(p['ticker'],42)} {p['side'].upper()} ×{p['qty']} @ {money(p['entry_price'])}; if win +{money(p['potential_net_win'])}, if lose {money(p['potential_net_loss'])}.")
    cal = s['calibration']
    if cal['n']:
        lines.append(f"First-forecast Brier {float(cal['brier']):.4f} (lower better), n={cal['n']}; market baseline {float(cal['baseline_brier']):.4f}." if cal['baseline_brier'] is not None else f"Forecast scoring n={cal['n']}; baseline unavailable.")
    else:
        lines.append('No scored outcomes yet. No historical wins or returns invented.')
    articles = {a['id']:a for a in r.get('news',{}).get('articles',[])}
    used = set()
    for match in sorted(r.get('news',{}).get('matches',[]),key=lambda m:-m['score']):
        a = articles.get(match['article_id'])
        if a and a['id'] not in used:
            line = f"Related: {safe(a['title'],95)} — {a['url']}"
            if sum(map(len,lines))+len(line)<1450:
                lines.append(line)
                used.add(a['id'])
            if len(used)>=1: break
    if r.get('errors'):
        lines.append(f"Data warnings: {len(r['errors'])}; {safe(r['errors'][0].get('error',''),120)}")
    if r.get('abstentions'):
        lines.append(f"Model abstentions: {len(r['abstentions'])}; {safe(r['abstentions'][0].get('error',''),100)}")
    mc = r.get('simulation',{})
    if mc.get('positions'):
        lines.append(f"Model-only {mc['paths']:,}-path net range (5–95%): {money(mc['p05'])} to {money(mc['p95'])}; not actual profit.")
    lines.append('Experimental model. News relevance ≠ price direction. Fees/fills estimated. No real bets.')
    return '\n'.join(lines)[:1900]
