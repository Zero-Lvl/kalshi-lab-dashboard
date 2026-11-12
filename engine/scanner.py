"""Bounded, credential-free collection and local simulated execution."""
import fcntl
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request
from public_http import open_public
from urllib.error import HTTPError, URLError

API = 'https://api.elections.kalshi.com/trade-api/v2'
WATCH_SERIES = ('KXFEDDECISION', 'KXCPI', 'KXBTC', 'KXINX')


def get_json(url):
    allowed = {'api.elections.kalshi.com', 'api.open-meteo.com', 'forecast.weather.gov',
               'api.exchange.coinbase.com'}
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in allowed or parts.username:
        raise ValueError('non-allowlisted public data URL')
    if parts.hostname == 'api.elections.kalshi.com' and not parts.path.startswith(('/trade-api/v2/markets','/trade-api/v2/series/')):
        raise ValueError('non-public market-data path')
    for attempt in range(2):
        try:
            with open_public(Request(url, headers={'User-Agent': 'KalshiLab/1.0 public-paper-research'}), timeout=7) as r:
                body = r.read(8_388_609)
            if len(body) > 8_388_608:
                raise ValueError('JSON response exceeds 8 MiB')
            return json.loads(body)
        except (HTTPError, URLError, TimeoutError) as exc:
            if attempt or isinstance(exc, HTTPError) and exc.code not in (429,500,502,503,504):
                raise
            time.sleep(1)


def scan(db_path, *, fetch_json=None, fetch_bytes=None, now=None, no_news=False, dry_run=False, settlement_only=False):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.scan.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another paper scan is already running')
        return _scan(path, fetch_json, fetch_bytes, now, no_news, dry_run, settlement_only)


def _scan(path, fetch_json, fetch_bytes, fixed_now, no_news, dry_run, settlement_only):
    from engine import Ledger, dumps, evaluate_candidate
    from signals import SERIES, build_weather_signals
    from news import collect_news
    clock = (lambda: fixed_now) if fixed_now is not None else (lambda: datetime.now(timezone.utc))
    transport = fetch_json or get_json
    ledger = Ledger(path)
    ledger.db.executescript('''
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
          completed_at TEXT, report_json TEXT);
        CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL,
          endpoint TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS snapshots_run ON snapshots(run_id);
        CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL,
          published_at TEXT NOT NULL, article_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS news_matches (run_id INTEGER NOT NULL, ticker TEXT NOT NULL,
          article_id TEXT NOT NULL, match_json TEXT NOT NULL,
          PRIMARY KEY(run_id,ticker,article_id));
    ''')
    run_id = ledger.db.execute('INSERT INTO runs(started_at) VALUES (?)', (clock().isoformat(),)).lastrowid
    deadline = time.monotonic()+105
    errors, coverage = [], []
    markets, fetched, cache = {}, {}, {}
    def fetch(url):
        if url in cache: return cache[url]
        if time.monotonic() > deadline: raise TimeoutError('scan request budget exceeded')
        value = transport(url)
        observed = clock()
        ledger.db.execute('INSERT INTO snapshots(run_id,endpoint,observed_at,payload_json) VALUES (?,?,?,?)',
                          (run_id,url,observed.isoformat(),dumps(value)))
        cache[url], fetched[url] = value, observed
        return value
    def add_error(source, exc):
        errors.append({'source': source, 'error': str(exc)[:500]})
    def get_markets(series=None, pages=1):
        cursor, count, seen = '', 0, set()
        for _ in range(pages):
            query = {'limit': 200 if series is None else 100, 'status':'open', 'mve_filter':'exclude',
                     'max_close_ts':int((clock()+timedelta(days=45)).timestamp())}
            if series: query['series_ticker'] = series
            if cursor: query['cursor'] = cursor
            url = API+'/markets?'+urlencode(query)
            try:
                data = fetch(url)
                batch = data['markets']
                if not isinstance(batch,list): raise ValueError('markets not a list')
                for m in batch:
                    if not isinstance(m,dict) or not m.get('ticker') or not m.get('event_ticker'): continue
                    markets[m['ticker']] = dict(m, _fetched_at=fetched[url].isoformat())
                    count += 1
                cursor = data.get('cursor','')
                if not cursor: break
                if cursor in seen: raise ValueError('repeated pagination cursor')
                seen.add(cursor)
            except Exception as exc:
                add_error(series or 'broad markets',exc)
                break
        coverage.append({'series':series or 'broad recent non-combo', 'returned':count,
                         'truncated': bool(cursor), 'max_pages':pages})
    try:
        tracked = {r[0] for r in ledger.db.execute('''SELECT DISTINCT s.ticker FROM signals s
            LEFT JOIN settlements z ON z.ticker=s.ticker WHERE z.ticker IS NULL''')}
        open_tickers = {p['ticker'] for p in ledger.positions('open')}
        for ticker in sorted(open_tickers):
            url = API+'/markets/'+quote(ticker,safe='')
            try:
                m = fetch(url)['market']
                if m.get('ticker') != ticker: raise ValueError('market response ticker mismatch')
                ledger.settle(m,clock())
                if m.get('status') in ('active','open'):
                    markets[ticker] = dict(m,_fetched_at=fetched[url].isoformat())
                elif m.get('status') in ('settled','finalized') and m.get('result') not in ('yes','no'):
                    add_error(ticker,'nonbinary/missing final result: left unresolved')
            except Exception as exc: add_error(ticker,exc)
        # Forecast-only scoring must not starve actual portfolio monitoring as
        # the multi-month sample grows. Public ticker batches cap request count.
        shadows = sorted(tracked-open_tickers)
        for start in range(0,len(shadows),100):
            batch=shadows[start:start+100]
            url=API+'/markets?'+urlencode({'tickers':','.join(batch),'limit':100})
            try:
                data=fetch(url)
                returned=set()
                for m in data['markets']:
                    ticker=m.get('ticker')
                    if ticker not in batch: raise ValueError('unrequested ticker in forecast refresh')
                    returned.add(ticker)
                    ledger.settle(m,clock())
                    if m.get('status') in ('active','open'):
                        markets[ticker]=dict(m,_fetched_at=fetched[url].isoformat())
                if returned != set(batch):
                    add_error('forecast refresh',f'{len(set(batch)-returned)} tickers missing; unresolved, not losses')
            except Exception as exc: add_error('forecast refresh',exc)
        metadata, strategy_states = {}, []
        policy_ok, policy = False, {}
        news = {'articles': [], 'matches': [], 'errors': [], 'feeds_ok':0}
        if not settlement_only:
            from strategies import EXPANDED_SERIES, build_diversified_signals
            for series in SERIES: get_markets(series,pages=2)
            extra_series = list(dict.fromkeys((*WATCH_SERIES,*EXPANDED_SERIES)))
            for series in extra_series:
                get_markets(series)
                try:
                    metadata[series] = fetch(API+'/series/'+quote(series,safe=''))['series']
                except Exception as exc: add_error(series+' metadata',exc)
            get_markets(pages=3)
            for m in markets.values():
                info = metadata.get(m['event_ticker'].split('-')[0],{})
                if info.get('category'): m['category'] = info['category']
        modeled = [m for m in markets.values() if m['event_ticker'].split('-')[0] in SERIES]
        signals, abstentions = build_weather_signals(modeled,fetch,clock()) if not settlement_only else ([],[])
        for signal in signals:
            signal.update(category='Weather',strategy_id=signal['model_version'],entry_tier='model',
                          decision_reason='Independent forecast estimate passed contract validation; experimental Gaussian model')
        if not no_news and not settlement_only and time.monotonic() < deadline:
            news = collect_news(list(markets.values()),clock(),fetch_bytes)
        elif not no_news and not settlement_only:
            add_error('news','skipped: scan time budget exceeded')
        if not settlement_only:
            try:
                from strategies import strategy_policy
                policy = strategy_policy(ledger,clock())
                if not isinstance(policy,dict) or not policy or any(
                    not isinstance(state,dict) or
                    not isinstance(state.get('strategy_id'),str) or not state['strategy_id'] or
                    not isinstance(state.get('category'),str) or not state['category'] or
                    key != state['strategy_id']+'/'+state['category'] or
                    state.get('status') not in ('warming_up','holdout','monitoring','paused')
                    for key,state in policy.items()):
                    raise ValueError('malformed or unavailable strategy policy')
                strategy_states = list(policy.values())
                policy_ok = True
                ledger.log_decision('policy','conservative strategy policy evaluated from settled outcomes only',clock(),
                    payload={'states':strategy_states},run_id=run_id)
            except Exception as exc:
                add_error('strategy policy',exc)
                ledger.log_decision('policy_error','strategy policy unavailable; all new entries blocked',clock(),
                    payload={'error':str(exc)},run_id=run_id)
            try:
                expanded = build_diversified_signals([m for m in markets.values()
                    if m['event_ticker'].split('-')[0] not in SERIES],fetch,clock(),news=news,ledger=ledger)
                signals.extend(expanded['signals'])
                for issue in expanded.get('errors',[]):
                    (abstentions if issue.get('kind')=='eligibility' else errors).append(issue)

                for d in expanded.get('decisions',[]):
                    ledger.log_decision('analysis',str(d.get('reason',d.get('rationale',d.get('decision_reason','strategy eligibility evaluated')))),clock(),
                        ticker=d.get('ticker'),event_ticker=d.get('event_ticker'),strategy=d.get('strategy_id'),payload=d,run_id=run_id)
            except Exception as exc: add_error('diversified strategies',exc)
            if policy_ok and any(
                'category/'+str(signal.get('category')) not in policy or
                str(signal.get('strategy_id'))+'/'+str(signal.get('category')) not in policy
                for signal in signals):
                policy_ok = False
                add_error('strategy policy','incomplete strategy policy; all new entries blocked')
                ledger.log_decision('policy_error','incomplete strategy policy; all new entries blocked',clock(),run_id=run_id)
        candidates, entries, recorded_signals = [], 0, []
        for signal in signals:
            m = markets[signal['ticker']]
            # Input snapshots hold exact completion times; never label collection-start
            # as if a response had already arrived.
            for source in signal.get('sources',[]):
                if source['url'] in fetched:
                    source['observed_at'] = fetched[source['url']].isoformat()
            if signal.get('sources'):
                signal['source_observed_at'] = max(s['observed_at'] for s in signal['sources'])
            if ledger.observe_signal(signal,m,clock(),run_id) is False:
                abstentions.append({'ticker':signal['ticker'],'error':'known final settlement; no hindsight forecast'})
                continue
            recorded_signals.append(signal)
            paused = any(state.get('status',state.get('state'))=='paused' and
                state.get('category')==signal.get('category') and
                state.get('strategy_id',state.get('id')) in ('category',signal.get('strategy_id'))
                for state in strategy_states)
            if not policy_ok or paused:
                reason = ('strategy/category paused by settled-event policy; shadow forecast only' if policy_ok else
                          'strategy policy unavailable or malformed; all new entries blocked; shadow forecast only')
                ledger.log_decision('paused',reason,clock(),
                    ticker=signal['ticker'],event_ticker=signal['event_ticker'],strategy=signal.get('strategy_id'),run_id=run_id)
                continue
            series = m['event_ticker'].split('-')[0]
            try:
                if series not in metadata:
                    metadata[series] = fetch(API+'/series/'+quote(series,safe=''))['series']
                c = evaluate_candidate(m,signal,metadata[series],clock(),m['_fetched_at'])
                if c:
                    candidates.append(c)
                    ledger.log_decision('candidate','execution and estimated-cost criteria passed',clock(),
                        ticker=m['ticker'],event_ticker=m['event_ticker'],strategy=signal.get('strategy_id'),payload=c,run_id=run_id)
                else:
                    ledger.log_decision('rejected','quote age, liquidity, fee, margin or sizing criteria failed',clock(),
                        ticker=m['ticker'],event_ticker=m['event_ticker'],strategy=signal.get('strategy_id'),payload=signal,run_id=run_id)
            except Exception as exc: add_error(series,exc)
        candidates.sort(key=lambda c:c['net_margin'],reverse=True)
        if not dry_run:
            entered_control_categories = set()
            for c in candidates:
                sig = c['signal']
                category = sig.get('category','Other')
                exploratory = sig.get('entry_tier')=='exploratory'
                if exploratory and category in entered_control_categories:
                    continue
                entered = ledger.enter(c,clock())
                entries += int(entered)
                if entered and exploratory: entered_control_categories.add(category)
                if not entered:
                    ledger.log_decision('skipped','duplicate event or portfolio/category exposure limit',clock(),
                        ticker=c['ticker'],event_ticker=c['event_ticker'],strategy=sig.get('strategy_id'),run_id=run_id)
        for a in news['articles']:
            ledger.db.execute('INSERT OR IGNORE INTO articles VALUES (?,?,?,?)',
                              (a['id'],a['observed_at'],a['published_at'],dumps(a)))
        for match in news['matches']:
            ledger.db.execute('INSERT OR IGNORE INTO news_matches VALUES (?,?,?,?)',
                              (run_id,match['ticker'],match['article_id'],dumps(match)))
        errors.extend(news['errors'])
        report = {'observed_at':clock().isoformat(), 'run_id':run_id, 'paper_only':True,
                  'dry_run':dry_run, 'mode':'settlement_refresh' if settlement_only else 'entry_scan',
                  'coverage':coverage, 'errors':errors, 'abstentions':abstentions,
                  'counts':{'markets':len(markets), 'forecasts':len(recorded_signals), 'new_positions':entries,
                            'articles':len(news['articles']), 'news_matches':len(news['matches']),
                            'feeds_ok':news['feeds_ok']},
                  'candidates':candidates, 'signals':recorded_signals, 'news':news,'strategy_states':strategy_states,
                  'markets':list(markets.values())}
        ledger.db.execute('UPDATE runs SET completed_at=?,report_json=? WHERE id=?',
                          (clock().isoformat(),dumps(report),run_id))
        return report
    finally:
        ledger.close()
