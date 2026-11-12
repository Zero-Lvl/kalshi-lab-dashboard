"""Allowlisted, read-only research history; probabilities are never recomputed."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import tempfile
import re
from urllib.parse import unquote, urlsplit, parse_qsl

from engine import timestamp
from analytics import _text, _url, _identity, _evidence


def _object(value):
    try:
        value = json.loads(value)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _at(value):
    try:
        return timestamp(value).isoformat() if value is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


_PRIVATE = re.compile(
    r'(?i)(?:https?://\S+|file://\S+|(?:^|\s)(?:/|~/|[a-z]:[\\/]|\\\\)\S*|'
    r'\b(?:password|passwd|secret|token|api[_ -]?key|authorization|bearer|credential|'
    r'account[_ -]?id|user[_ -]?id|session[_ -]?id|config|traceback|ssh[_ -]?key)\b|'
    r'[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:\d{1,3}\.){3}\d{1,3}\b|'
    r'\b\d{3}-\d{2}-\d{4}\b|\b(?:sk-|ghp_|github_pat_)[a-z0-9_-]+|'
    r'\b[a-z0-9_+/=-]{48,}\b)')


def _plain(value):
    if not isinstance(value, str):
        return None
    value = ' '.join(value.split())
    match = _PRIVATE.search(unquote(value))
    if match:
        # Do not keep suffixes: they may include unlabelled secret values.
        return '[operational detail omitted]'
    return _text(value, None, 1200)


def _public_url(value):
    value = _url(value)
    if not value:
        return None
    decoded = unquote(unquote(value))
    parts = urlsplit(decoded)
    if parts.fragment or re.search(r'(?i)/(?:home|users|etc|root|tmp|var|config|\.ssh|\.config)(?:/|$)', parts.path):
        return None
    if any(re.search(r'(?i)token|secret|pass|auth|key|session|email|user|account|credential|signature|code', key)
           or '@' in val or len(val) > 100 for key, val in parse_qsl(parts.query)):
        return None
    if re.search(r'(?i)(?:sk-|ghp_|github_pat_)[a-z0-9_-]+', decoded):
        return None
    return value


def _identifier(value):
    return value if type(value) is int and value >= 0 else None

def _public_id(value, fallback=None):
    if type(value) is int and value>=0:return value
    if isinstance(value,str) and re.fullmatch('[a-fA-F0-9]{64}',value):return value
    clean=_plain(value)
    return clean if clean and clean!='[operational detail omitted]' else fallback


def _number(value, probability=False):
    from decimal import Decimal, InvalidOperation
    try:
        number = Decimal(str(value))
        if not number.is_finite() or (probability and not 0 <= number <= 1):
            return None
        return str(number)
    except (ValueError, TypeError, InvalidOperation):
        return None


def _record(kind, r, db, tables):
    observed = r.get('observed_at', r.get('first_seen_at', r.get('started_at')))
    base = {'id': _public_id(r.get('id',r['_rowid']),r['_rowid']), 'observed_at': _at(observed)}
    if kind == 'decisions':
        payload = _object(r.get('payload_json'))
        signal = payload.get('signal') if isinstance(payload.get('signal'), dict) else payload
        base.update({k: r.get(k) if k == 'run_id' else _plain(r.get(k)) for k in
                     ('run_id', 'ticker', 'event_ticker', 'strategy', 'action', 'reason')})
        base['category'] = _plain(signal.get('category'))
        for key in ('qty', 'entry_price', 'fee', 'cost', 'net_margin', 'potential_net_win', 'potential_net_loss'):
            value = _number(payload.get(key))
            if value is None:
                continue
            from decimal import Decimal
            number = Decimal(value)
            if key == 'qty':
                if number > 0 and number == number.to_integral_value() and number <= 10**9:
                    base[key] = int(number)
            elif key == 'entry_price':
                if 0 < number < 1:
                    base[key] = value
            elif key in ('fee', 'cost'):
                if number >= 0:
                    base[key] = value
            else:
                base[key] = value
    elif kind == 'forecasts':
        signal = _object(r.get('signal_json'))
        category, strategy = _identity(signal, r.get('model_version'))
        base.update(ticker=_plain(r.get('ticker')), event_ticker=_plain(signal.get('event_ticker')),
                    category=_plain(category), strategy=_plain(strategy), model_version=_plain(r.get('model_version')),
                    p_yes=_number(r.get('p_yes'), True), baseline_p_yes=_number(r.get('baseline_p_yes'), True),
                    reason=_plain(signal.get('decision_reason')), evidence=[{'label': _plain(e['label']), 'url': _public_url(e['url'])}
                        for e in _evidence(signal) if _public_url(e['url'])],
                    result=None, settled_at=None, paper_position_exists=False)
        if 'settlements' in tables:
            settlement = db.execute('SELECT result,observed_at FROM settlements WHERE ticker=?', (r.get('ticker'),)).fetchone()
            if settlement and settlement[0] in ('yes', 'no') and _at(settlement[1]):
                base.update(result=settlement[0], settled_at=_at(settlement[1]))
        if 'positions' in tables:
            base['paper_position_exists'] = db.execute('SELECT 1 FROM positions WHERE ticker=? LIMIT 1', (r.get('ticker'),)).fetchone() is not None
    elif kind == 'news':
        article = _object(r.get('article_json'))
        base.update(published_at=_at(r.get('published_at')), title=_plain(article.get('title')),
                    url=_public_url(article.get('url')), source=_plain(article.get('source')))
    elif kind == 'matches':
        article = {}
        if 'articles' in tables:
            row = db.execute('SELECT article_json FROM articles WHERE id=?', (r.get('article_id'),)).fetchone()
            article = _object(row[0]) if row else {}
        if 'runs' in tables:
            row = db.execute('SELECT started_at FROM runs WHERE id=?', (r.get('run_id'),)).fetchone()
            base['observed_at'] = _at(row[0]) if row else None
        base.update(run_id=r.get('run_id'), ticker=_plain(r.get('ticker')), article_id=_public_id(r.get('article_id')),
                    title=_plain(article.get('title')), url=_public_url(article.get('url')),
                    reason='Topical keyword relevance only; not predictive direction or a trade recommendation.')
    else:
        report = _object(r.get('report_json'))
        counts = report.get('counts') if isinstance(report.get('counts'), dict) else {}
        base.update(completed_at=_at(r.get('completed_at')), mode=_plain(report.get('mode')))
        for key in ('markets', 'forecasts', 'new_positions', 'articles', 'news_matches', 'feeds_ok'):
            value = counts.get(key)
            base[key] = value if type(value) is int and value >= 0 else None
        for key in ('abstentions', 'errors'):
            base[key] = len(report[key]) if isinstance(report.get(key), list) else None
    return base

KINDS = ('decisions', 'forecasts', 'news', 'matches', 'scans')

RECORD_FIELDS={
    'decisions':set('id observed_at run_id ticker event_ticker category strategy action reason side qty entry_price fee cost net_margin potential_net_win potential_net_loss p_yes baseline_p_yes model_version net payout'.split()),
    'forecasts':set('id observed_at ticker event_ticker category strategy model_version p_yes baseline_p_yes reason evidence result settled_at paper_position_exists'.split()),
    'news':set('id observed_at published_at title url source'.split()),
    'matches':set('id observed_at run_id ticker article_id title url reason'.split()),
    'scans':set('id observed_at completed_at mode markets forecasts new_positions articles news_matches feeds_ok abstentions errors'.split())}

def validate_research_payloads(payloads):
    """Validate complete archive shape, coverage and privacy before any upload."""
    names={name for name in payloads if name.startswith('research/')}
    if not names:return
    if 'research/index.json' not in names:raise ValueError('research index missing')
    def decode(name):
        def bad(value):raise ValueError('nonfinite research JSON')
        return json.loads(payloads[name],parse_constant=bad)
    index=decode('research/index.json')
    if (not isinstance(index,dict) or set(index)!=set('schema_version generated_at page_size ordering streams'.split())
            or index['schema_version']!=1 or index['ordering']!='oldest_first'
            or type(index['page_size']) is not int or not 1<=index['page_size']<=1000
            or not _at(index['generated_at']) or not isinstance(index['streams'],dict) or set(index['streams'])!=set(KINDS)):
        raise ValueError('invalid research index')
    referenced={'research/index.json'}
    numeric=set('qty entry_price fee cost net_margin potential_net_win potential_net_loss p_yes baseline_p_yes net payout markets forecasts new_positions articles news_matches feeds_ok abstentions errors'.split())
    times={'observed_at','published_at','settled_at','completed_at'}
    integers=set('run_id qty markets forecasts new_positions articles news_matches feeds_ok abstentions errors'.split())
    def valid_time(value):return value is None or (isinstance(value,str) and _at(value) is not None)
    def clean(value,key=None):
        if isinstance(value,dict):
            for k,v in value.items():clean(v,k)
        elif isinstance(value,list):
            for v in value:clean(v,key)
        elif isinstance(value,str):
            if key=='url':
                if _public_url(value)!=value:raise ValueError('private research URL')
            elif key in ('id','article_id') and re.fullmatch('[a-fA-F0-9]{64}',value):pass
            elif _plain(value)!=value:raise ValueError('private or unbounded research text')
        elif value is not None and not isinstance(value,(int,float,bool)):
            raise ValueError('invalid research value')
        if key in numeric and value is not None and not isinstance(value,(dict,list)):
            if isinstance(value,bool) or _number(value,key in ('p_yes','baseline_p_yes')) is None:
                raise ValueError('invalid research number')
    for kind,stream in index['streams'].items():
        if (not isinstance(stream,dict) or set(stream)!={'total','pages'} or type(stream['total']) is not int
                or stream['total']<0 or not isinstance(stream['pages'],list)):
            raise ValueError('invalid research stream')
        total=0;seen=set()
        for number,meta in enumerate(stream['pages'],1):
            path=f'research/{kind}-{number:06d}.json'
            if (not isinstance(meta,dict) or set(meta)!={'path','count','first_at','last_at'} or meta['path']!=path
                    or type(meta['count']) is not int or not 1<=meta['count']<=index['page_size'] or path not in names
                    or not valid_time(meta['first_at']) or not valid_time(meta['last_at'])):
                raise ValueError('invalid or missing research page')
            page=decode(path)
            if (not isinstance(page,dict) or set(page)!={'schema_version','stream','page','records'}
                    or page['schema_version']!=1 or page['stream']!=kind or type(page['page']) is not int
                    or page['page']!=number or not isinstance(page['records'],list) or len(page['records'])!=meta['count']):
                raise ValueError('research page count/identity mismatch')
            for record in page['records']:
                if not isinstance(record,dict) or set(record)-RECORD_FIELDS[kind] or not {'id','observed_at'}<=set(record):
                    raise ValueError('nonallowlisted research record')
                identity=record['id']
                if not ((type(identity) is int and identity>=0) or (isinstance(identity,str) and identity)):
                    raise ValueError('invalid research ID')
                if identity in seen:raise ValueError('duplicate research record')
                seen.add(identity)
                if 'evidence' in record:
                    if not isinstance(record['evidence'],list) or any(not isinstance(e,dict) or set(e)!={'label','url'} for e in record['evidence']):
                        raise ValueError('invalid research evidence')
                    if any((e['label'] is not None and not isinstance(e['label'],str)) or not isinstance(e['url'],str) for e in record['evidence']):
                        raise ValueError('invalid research evidence types')
                for key,value in record.items():
                    if key in ('id','evidence'):continue
                    if key in times:
                        valid=valid_time(value)
                    elif key in integers:
                        valid=value is None or (type(value) is int and value>=0)
                    elif key in numeric:
                        valid=value is None or (not isinstance(value,bool) and isinstance(value,(str,int,float)) and _number(value,key in ('p_yes','baseline_p_yes')) is not None)
                    elif key=='paper_position_exists':valid=type(value) is bool
                    elif key=='result':valid=value in (None,'yes','no')
                    else:valid=value is None or isinstance(value,str)
                    if not valid:raise ValueError('invalid research field type: '+key)
                clean(record)
            total+=meta['count'];referenced.add(path)
        if total!=stream['total']:raise ValueError('research stream total mismatch')
    if referenced!=names:raise ValueError('unreferenced research pages')


def _publish(root, relative, value):
    data = (json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False,
                       separators=(',', ':')) + '\n').encode()
    target = root / relative
    if not target.exists() or target.read_bytes() != data:
        fd, name = tempfile.mkstemp(prefix='.research-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return {'path': relative, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def build_research_archive(ledger, output, now=None, page_size=100):
    """Write site-root-relative pages, then their index; never write the ledger."""
    from site_builder import site_files
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError('research page size must be 1..1000')
    root = Path(output)
    site_files(root)
    (root / 'research').mkdir(parents=True, exist_ok=True)
    index = {'schema_version': 1, 'generated_at': timestamp(now or datetime.now(timezone.utc)).isoformat(),
             'page_size': page_size, 'ordering': 'oldest_first',
             'streams': {kind: {'total': 0, 'pages': []} for kind in KINDS}}
    manifest = []
    db = ledger.db
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for kind, table in zip(KINDS, ('decision_log', 'signals', 'articles', 'news_matches', 'runs')):
        if table not in tables:
            continue
        cursor = db.execute(f'SELECT rowid AS _rowid,* FROM {table} ORDER BY rowid ASC')
        page = 0
        seen = set()
        while True:
            rows = cursor.fetchmany(page_size)
            if not rows:
                break
            records = []
            for row in rows:
                record = _record(kind, dict(row), db, tables)
                identity = record['id']
                if identity not in seen:
                    seen.add(identity)
                    records.append(record)
            if not records:
                continue
            page += 1
            relative = f'research/{kind}-{page:06d}.json'
            manifest.append(_publish(root, relative, {'schema_version': 1, 'stream': kind, 'page': page, 'records': records}))
            index['streams'][kind]['total'] += len(records)
            index['streams'][kind]['pages'].append({'path': relative, 'count': len(records),
                'first_at': records[0]['observed_at'], 'last_at': records[-1]['observed_at']})
    manifest.append(_publish(root, 'research/index.json', index))
    return manifest
