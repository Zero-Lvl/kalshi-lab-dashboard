"""Read-only, explicitly allowlisted public paper-account analytics.

No source snapshots or model simulations belong in this export. Money is always
finite decimal text; unknown liquidation values remain null.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import json
import sqlite3
import ipaddress
import re
from urllib.parse import urlsplit, parse_qsl

from engine import INITIAL_CASH, decimal, timestamp, trading_fee


def _settled(position, at):
    return (position['status'] == 'settled' and position.get('settled_at') is not None
            and timestamp(position['settled_at']) <= at)


def _mark(position, market, at):
    if not market or market.get('status') not in ('active', 'open'):
        return None
    try:
        age = (at - timestamp(market['_fetched_at'])).total_seconds()
        key, size = (('yes_bid_dollars', 'yes_bid_size_fp') if position['side'] == 'yes'
                     else ('no_bid_dollars', 'yes_ask_size_fp'))
        bid, depth = decimal(market[key]), decimal(market[size])
        multiplier = decimal(position['signal'].get('fee_multiplier', 1))
        if not (0 <= age <= 7200 and 0 < bid <= 1 and depth >= position['qty'] and multiplier >= 0):
            return None
        price = max(Decimal(0), bid - Decimal('.01'))
        return price * position['qty'] - trading_fee(position['qty'], price, multiplier) - decimal(position['cost'])
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return None


def _account(positions, at, markets=None):
    entered = [p for p in positions if timestamp(p['opened_at']) <= at]
    settled = [p for p in entered if _settled(p, at)]
    opened = [p for p in entered if not _settled(p, at)]
    cost = sum((decimal(p['cost']) for p in entered), Decimal(0))
    payout = sum((decimal(p['payout']) for p in settled), Decimal(0))
    realized = sum((decimal(p['net']) for p in settled), Decimal(0))
    marks = [_mark(p, (markets or {}).get(p['ticker']), at) for p in opened]
    unrealized = None if any(mark is None for mark in marks) else sum(marks, Decimal(0))
    return {'time': at.isoformat(), 'realized_pnl': str(realized),
            'unrealized_pnl': str(unrealized) if unrealized is not None else None,
            'total_pnl': str(realized + unrealized) if unrealized is not None else None,
            'cash': str(INITIAL_CASH - cost + payout),
            'open_cost': str(sum((decimal(p['cost']) for p in opened), Decimal(0)))}


def _text(value, fallback='', limit=240):
    return ' '.join(value.split())[:limit] if isinstance(value, str) else fallback


def _url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value) or '\\' in value:
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').lower().rstrip('.')
        if parts.scheme not in ('http', 'https') or not host or parts.username is not None or parts.password is not None:
            return None
        if parts.port not in (None, 80, 443):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if ('.' not in host or host.endswith(('.local', '.internal', '.localhost', '.lan', '.home', '.invalid'))
                    or not re.fullmatch(r'[a-z0-9.-]+', host) or not host.rsplit('.', 1)[-1].isalpha()
                    or any(not label or label.startswith('-') or label.endswith('-') for label in host.split('.'))):
                return None
        sensitive = {'password', 'passwd', 'token', 'access_token', 'api_key', 'apikey', 'secret', 'key', 'authorization'}
        if any(key.lower() in sensitive for key, _ in parse_qsl(parts.query)):
            return None
        return value
    except (ValueError, TypeError):
        return None


def _evidence(signal):
    candidates = []
    for key in ('evidence', 'sources'):
        value = signal.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    if signal.get('source_url'):
        candidates.append({'url': signal['source_url'], 'label': 'Model source'})
    result, seen = [], set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = _url(item.get('url'))
        if url and url not in seen:
            result.append({'label': _text(item.get('label') or item.get('name'), 'Public evidence', 120), 'url': url})
            seen.add(url)
        if len(result) >= 12:
            break
    return result


def _coverage(report):
    result = []
    for row in report.get('coverage', []):
        if not isinstance(row, dict):
            continue
        result.append({'series': _text(row.get('series'), 'Bounded scan', 80),
                       'returned': _count(row.get('returned')),
                       'truncated': row.get('truncated') is True,
                       'max_pages': _count(row.get('max_pages'))})
    return result


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _identity(signal, model_version=None):
    return (_text(signal.get('category'), 'Weather', 80),
            _text(signal.get('strategy_id'), _text(model_version or signal.get('model_version'), 'weather-gaussian-v1', 100), 100))


def _position(position, now, market=None):
    p = position
    category, strategy = _identity(p['signal'], p.get('model_version'))
    settled = _settled(p, now)
    return {'ticker': p['ticker'], 'title': (_text(p['signal'].get('market_title'))
                or _text((market or {}).get('title')) or p['ticker']),
            'category': category, 'strategy': strategy, 'side': p['side'], 'qty': p['qty'],
            'opened_at': timestamp(p['opened_at']).isoformat(),
            'status': 'settled' if settled else 'open',
            'entry_price': str(decimal(p['entry_price'])), 'fee': str(decimal(p['fee'])),
            'cost': str(decimal(p['cost'])), 'p_yes': str(decimal(p['p_yes'])),
            'baseline_p_yes': str(decimal(p['baseline_p_yes'])) if p.get('baseline_p_yes') is not None else None,
            'settled_at': timestamp(p['settled_at']).isoformat() if settled else None,
            'result': p['result'] if settled else None,
            'payout': str(decimal(p['payout'])) if settled else None,
            'realized_pnl': str(decimal(p['net'])) if settled else None,
            'unrealized_pnl': str(_mark(p, market, now)) if not settled and _mark(p, market, now) is not None else None,
            'estimated_net_win': str(Decimal(p['qty']) - decimal(p['cost'])),
            'estimated_net_loss': str(-decimal(p['cost'])), 'evidence': _evidence(p['signal']),
            'decision_reason': _text(p['signal'].get('decision_reason'), 'Legacy weather-model paper entry.', 600)}


def _periods(positions, now):
    local = now.astimezone(ZoneInfo('America/Los_Angeles'))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = {'day': day, 'week': day - timedelta(days=day.weekday()),
              'month': day.replace(day=1)}
    result = {}
    for key, start in starts.items():
        start = start.astimezone(timezone.utc)
        settled = [p for p in positions if _settled(p, now)
                   and timestamp(p['settled_at']) >= start]
        entries = [p for p in positions if timestamp(p['opened_at']) >= start]
        wins = sum(p['side'] == p['result'] for p in settled)
        result[key] = {'realized_pnl': str(sum((decimal(p['net']) for p in settled), Decimal(0))),
                       'fees': str(sum((decimal(p['fee']) for p in entries), Decimal(0))),
                       'settled': len(settled), 'wins': wins, 'losses': len(settled) - wins}
    return result


def _drawdown(points):
    peak = maximum = Decimal(0)
    for point in points:
        realized = decimal(point['realized_pnl'])
        peak = max(peak, realized)
        maximum = max(maximum, peak - realized)
    return str(maximum)


def _time(value):
    try:
        return timestamp(value) if value is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _history(ledger, report, now, tickers):
    """Read at most 512 raw runs over 90 days; never persist derived marks."""
    runs = []
    first = None
    history_errors = runs_read = 0
    try:
        row = ledger.db.execute("""SELECT started_at FROM runs
            WHERE julianday(completed_at) <= julianday(?)
            ORDER BY julianday(started_at) LIMIT 1""", (now.isoformat(),)).fetchone()
        first = _time(row[0]) if row else None
        rows = ledger.db.execute("""SELECT completed_at, report_json FROM runs
            WHERE julianday(completed_at) BETWEEN julianday(?) AND julianday(?)
            ORDER BY julianday(completed_at) DESC, id DESC LIMIT 512""",
            ((now - timedelta(days=90)).isoformat(), now.isoformat()))
        for row in rows:
            runs_read += 1
            try:
                raw = json.loads(row['report_json'])
                if isinstance(raw, dict):
                    # Retain only immutable-position quote inputs, not the broad
                    # market universe or private reports, across the 90-day window.
                    selected = [m for m in raw.get('markets', []) if isinstance(m, dict)
                                and m.get('ticker') in tickers]
                    runs.append(({'observed_at': raw.get('observed_at'), 'markets': selected},
                                 _time(row['completed_at'])))
            except (TypeError, ValueError):
                history_errors += 1
                continue
    except sqlite3.OperationalError:
        # Standalone/legacy ledgers have no runs table.
        pass
    runs.reverse()
    runs.append((report, None))
    snapshots = {}
    observed = None
    observations = set()
    for raw, completed in runs:
        batch = [m for m in raw.get('markets', []) if isinstance(m, dict) and m.get('ticker') in tickers]
        times = [_time(m.get('_fetched_at')) for m in batch]
        observed_at = _time(raw.get('observed_at')) or completed
        if observed_at is None:
            observed_at = max((t for t in times if t is not None), default=None)
        if observed_at is None or observed_at > now:
            continue
        observations.add(observed_at)
        observed = max(observed, observed_at) if observed else observed_at
        first = min(first, observed_at) if first else observed_at
        quotes = {}
        by_time = {}
        for m, fetched in zip(batch, times):
            fetched = fetched or observed_at
            if fetched > observed_at or fetched > now:
                continue
            first = min(first, fetched)
            by_time.setdefault(fetched, []).append(dict(m, _fetched_at=fetched.isoformat()))
        for fetched in sorted(by_time):
            quotes.update((m['ticker'], m) for m in by_time[fetched])
            snapshots[fetched] = dict(quotes)
        snapshots[observed_at] = dict(quotes)
    return snapshots, first, observed, {'scan_observations': len(observations),
        'history_runs_read': runs_read, 'history_errors': history_errors}


def _strategy_metrics(ledger, positions, report, now):
    """Equal-weight events, not repeated forecasts or correlated bucket counts."""
    settlements = {r['ticker']: dict(r) for r in ledger.db.execute(
        "SELECT ticker,result,observed_at FROM settlements WHERE julianday(observed_at)<=julianday(?)",
        (now.isoformat(),))}
    forecasts = [f for f in ledger.forecasts() if _time(f.get('observed_at')) is not None
                 and timestamp(f['observed_at']) <= now]
    forecasts.sort(key=lambda f: (timestamp(f['observed_at']), f['id']))
    first_time = min((timestamp(f['observed_at']) for f in forecasts), default=None)
    groups, seen, forecast_events, forecast_markets = {}, set(), set(), set()
    for f in forecasts:
        try:
            signal = json.loads(f['signal_json'])
            if not isinstance(signal, dict):
                continue
            key = _identity(signal, f.get('model_version'))
            event = _text(signal.get('event_ticker'), f['ticker'].rsplit('-', 1)[0])
            forecast_events.add(event)
            forecast_markets.add(f['ticker'])
            unique = (key, f['ticker'])
            if unique in seen:
                continue
            seen.add(unique)
            settlement = settlements.get(f['ticker'])
            # A post-resolution observation is never a scored prediction.
            if settlement and timestamp(f['observed_at']) >= timestamp(settlement['observed_at']):
                settlement = None
            groups.setdefault(key, []).append({**f, 'event': event,
                'result': settlement['result'] if settlement else None})
        except (ValueError, TypeError, KeyError):
            continue
    pnl, category_positions = {}, {}
    for p in positions:
        key = _identity(p['signal'], p.get('model_version'))
        pnl[key] = pnl.get(key, Decimal(0)) + (decimal(p['net']) if _settled(p, now) else Decimal(0))
        category_positions.setdefault(key[0], []).append(p)
        if (key, p['ticker']) not in seen:
            groups.setdefault(key, []).append({**p, 'event': p['event_ticker'],
                'result': p['result'] if _settled(p, now) else None})
    states = {}
    allowed_states = {'warming_up', 'holdout', 'monitoring', 'paused', 'inactive', 'active',
                      'observe_only', 'research', 'abstain'}
    for row in report.get('strategy_states', []):
        if not isinstance(row, dict) or not isinstance(row.get('strategy_id') or row.get('id'), str):
            continue
        sid = row.get('strategy_id') or row['id']
        if sid == 'category':
            continue
        key = (_text(row.get('category'), 'Other', 80), _text(sid, limit=100))
        state = row.get('state') or row.get('status')
        states[key] = state if isinstance(state, str) and state in allowed_states else 'research'
        groups.setdefault(key, [])
    strategies, category_events, all_events = [], {}, set()
    for (category, sid), rows in sorted(groups.items()):
        scores, baselines, events = {}, {}, set()
        for row in rows:
            if row.get('result') not in ('yes', 'no'):
                continue
            event = row['event']
            events.add(event)
            try:
                probability = decimal(row['p_yes'])
                if 0 <= probability <= 1:
                    scores.setdefault(event, []).append((probability - int(row['result'] == 'yes')) ** 2)
                baseline = decimal(row['baseline_p_yes'])
                if 0 <= baseline <= 1:
                    baselines.setdefault(event, []).append((baseline - int(row['result'] == 'yes')) ** 2)
            except (InvalidOperation, TypeError, ValueError, KeyError):
                continue
        def average_events(values):
            if not values:
                return None
            return str(sum((sum(v, Decimal(0)) / len(v) for v in values.values()), Decimal(0)) / len(values))
        category_events.setdefault(category, set()).update(events)
        all_events.update(events)
        strategies.append({'id': sid, 'category': category, 'state': states.get((category, sid), 'research'),
            'settled_events': len(events), 'brier': average_events(scores),
            'baseline_brier': average_events(baselines),
            'realized_pnl': str(pnl.get((category, sid), Decimal(0))),
            'note': 'First forecast per market, or immutable entry if absent; bucket scores averaged within events. '
                    'Events are not necessarily independent. Limited samples; no demonstrated edge.'})
    categories = []
    for category in sorted(set(category_positions) | set(category_events)):
        rows = category_positions.get(category, [])
        categories.append({'category': category, 'positions': len(rows),
            'open_positions': sum(not _settled(p, now) for p in rows),
            'settled_events': len(category_events.get(category, set())),
            'realized_pnl': str(sum((decimal(p['net']) for p in rows if _settled(p, now)), Decimal(0)))})
    return strategies, {'forecast_observations': len(forecasts), 'forecast_markets': len(forecast_markets),
                         'forecast_events': len(forecast_events), 'settled_events': len(all_events),
                         'categories': categories}, first_time


def build_public_export(ledger, report, now=None):
    """Build the v2 public view without modifying the supplied ledger or report."""
    now = timestamp(now or datetime.now(timezone.utc))
    positions = [p for p in ledger.positions() if timestamp(p['opened_at']) <= now]
    snapshots, first_scan, observed, history_counts = _history(ledger, report, now, {p['ticker'] for p in positions})
    settled = [p for p in positions if _settled(p, now)]
    opened = [p for p in positions if not _settled(p, now)]
    strategies, sample_counts, first_forecast = _strategy_metrics(ledger, positions, report, now)
    times = {timestamp(p['opened_at']) for p in positions}
    if first_forecast is not None:
        times.add(first_forecast)
    times.update(timestamp(p['settled_at']) for p in settled)
    times.update(snapshots)
    if first_scan is not None and first_scan <= now:
        times.add(first_scan)
    inception = min(times) if times else None
    if inception:
        times.add(now)
    points, markets = [], {}
    for at in sorted(times):
        if at in snapshots:
            markets = snapshots[at]
        points.append(_account(positions, at, markets))
    account = _account(positions, now, markets)
    priced = sum(_mark(p, markets.get(p['ticker']), now) is not None for p in opened)
    wins = sum(p['side'] == p['result'] for p in settled)
    return {
        'schema_version': 2, 'generated_at': now.isoformat(),
        'observed_at': observed.isoformat() if observed else None,
        'tracking_started_at': inception.isoformat() if inception else None, 'paper_only': True,
        'summary': {'initial_bankroll': str(INITIAL_CASH), 'cash': account['cash'],
                    'open_cost': account['open_cost'], 'realized_pnl': account['realized_pnl'],
                    'unrealized_pnl': account['unrealized_pnl'], 'total_pnl': account['total_pnl'],
                    'priced_positions': priced, 'unpriced_positions': len(opened) - priced,
                    'total_positions': len(positions), 'open_positions': len(opened),
                    'wins': wins, 'losses': len(settled) - wins, 'settled': len(settled)},
        'periods': _periods(positions, now),
        'performance': {'points': points, 'windows': {
            key: {'start': max(inception, now - timedelta(days=days)).isoformat() if inception else None,
                  'end': now.isoformat()}
            for key, days in (('24h', 1), ('7d', 7), ('1m', 30), ('3m', 90))}},
        'positions': [_position(p, now, markets.get(p['ticker'])) for p in positions], 'coverage': _coverage(report),
        'counts': {**{key: _count(report.get('counts', {}).get(key)) for key in
                     ('markets', 'forecasts', 'new_positions', 'articles', 'news_matches', 'feeds_ok')},
                   'errors': len(report.get('errors', [])), 'abstentions': len(report.get('abstentions', [])),
                   'fees_paid': str(sum((decimal(p['fee']) for p in positions), Decimal(0))),
                   'max_drawdown': _drawdown(points), **sample_counts, **history_counts}, 'strategies': strategies,
        'warnings': [
            'Paper only; estimated fills and fees, not real-money results.',
            'Default performance is realized net P&L, including entry fees; forecasts and simulations are not actual returns.',
            'Calendar day/week/month use America/Los_Angeles; weeks begin Monday. Rolling windows are 24h/7d/30d/90d.',
            'Limited observed coverage; no pre-inception history is fabricated. Historical quotes use a bounded 90-day, 512-run window.',
            'Open marks require a fresh bid and visible depth for the full position, less estimated exit fees and $0.01/share slippage. Missing marks make aggregate open and total P&L unknown.',
            'Cash plus open cost is cost accounting, not mark-to-market equity or profit. Fees are tallied at entry, not charged again at settlement.',
            'Event-grouped sample counts do not establish statistical independence or profitability.',
        ],
    }
