"""Synthetic-only public research archive contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 11, 20, tzinfo=timezone.utc)
KINDS = ('decisions', 'forecasts', 'news', 'matches', 'scans')


def build(ledger, output, **kwargs):
    path = ROOT / 'research.py'
    assert path.exists(), 'research implementation missing'
    spec = importlib.util.spec_from_file_location('research_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_research_archive(ledger, output, now=NOW, **kwargs)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = sqlite3.connect(self.root / 'fixture.db', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.ledger = SimpleNamespace(db=self.db)
        self.output = self.root / 'site'

    def index(self):
        return json.loads((self.output / 'research/index.json').read_text())

    def records(self, kind):
        return [r for page in self.index()['streams'][kind]['pages']
                for r in json.loads((self.output / page['path']).read_text())['records']]

    def populate(self, count=205):
        self.db.executescript('''
            CREATE TABLE decision_log (id INTEGER PRIMARY KEY,run_id,observed_at,ticker,event_ticker,strategy,action,reason,payload_json);
            CREATE TABLE signals (id INTEGER PRIMARY KEY,run_id,ticker,observed_at,p_yes,baseline_p_yes,model_version,signal_json);
            CREATE TABLE articles (id TEXT PRIMARY KEY,first_seen_at,published_at,article_json);
            CREATE TABLE news_matches (run_id,ticker,article_id,match_json,PRIMARY KEY(run_id,ticker,article_id));
            CREATE TABLE runs (id INTEGER PRIMARY KEY,started_at,completed_at,report_json);
        ''')
        self.append(count)

    def append(self, count):
        start = self.db.execute('SELECT count(*) FROM runs').fetchone()[0]
        for i in range(start + 1, start + count + 1):
            at = NOW.isoformat()
            self.db.execute('INSERT INTO decision_log VALUES (?,?,?,?,?,?,?,?,?)',
                (i,i,at,f'T-{i}','EVENT','legacy-model','rejected','Liquidity insufficient',json.dumps({'category':'Economics'})))
            self.db.execute('INSERT INTO signals VALUES (?,?,?,?,?,?,?,?)',
                (i,i,f'T-{i}',at,'.7','.5','weather-gaussian-v1-unvalidated',json.dumps({'event_ticker':'EVENT','decision_reason':'Independent forecast'})))
            self.db.execute('INSERT INTO articles VALUES (?,?,?,?)',
                (f'a{i}',at,at,json.dumps({'title':f'Public headline {i}','source':'News','url':f'https://example.org/{i}','summary':'DO NOT EXPORT FULL TEXT'})))
            self.db.execute('INSERT INTO news_matches VALUES (?,?,?,?)', (i,f'T-{i}',f'a{i}',json.dumps({'matched_terms':['inflation']})))
            self.db.execute('INSERT INTO runs VALUES (?,?,?,?)',
                (i,at,at,json.dumps({'mode':'entry_scan','counts':{'markets':i,'forecasts':1},'errors':['private traceback'],'abstentions':[{}]})))

    def test_full_history_all_streams_stable_append_and_read_only(self):
        self.populate()
        before = (self.root / 'fixture.db').read_bytes()
        self.db.set_authorizer(lambda action,*args: sqlite3.SQLITE_DENY if action in
            (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK)
        manifest = build(self.ledger, self.output)
        self.assertEqual(before, (self.root / 'fixture.db').read_bytes())
        self.assertEqual(len(manifest), 16)
        old = {}
        for kind in KINDS:
            stream = self.index()['streams'][kind]
            self.assertEqual(stream['total'], 205)
            self.assertEqual([p['count'] for p in stream['pages']], [100,100,5])
            self.assertEqual(len({r['id'] for r in self.records(kind)}), 205)
            for page in stream['pages'][:2]:
                p = self.output / page['path']
                old[p] = (p.read_bytes(), p.stat().st_mtime_ns)
            self.assertEqual(stream['pages'][0]['first_at'], NOW.isoformat())
        self.db.set_authorizer(None)
        self.append(1)
        build(self.ledger, self.output)
        for path, value in old.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), value)
        for kind in KINDS:
            self.assertEqual(self.index()['streams'][kind]['total'],206)
        self.assertEqual(self.records('forecasts')[0]['strategy'], 'weather-gaussian-v1-unvalidated')
        self.assertFalse(self.records('forecasts')[0]['paper_position_exists'])
        self.assertIsNone(self.records('forecasts')[0]['result'])
        self.assertIn('not predictive direction', self.records('matches')[0]['reason'])
        self.assertNotIn('DO NOT EXPORT', ''.join(p.read_text() for p in (self.output/'research').glob('*.json')))

    def test_privacy_malformed_finite_and_frozen_outcomes(self):
        self.populate(1)
        dirty = 'Liquidity limited /home/alice/.config/key.json password=SUPERSECRET alice@example.com 192.168.1.7'
        self.db.execute('UPDATE decision_log SET reason=?,strategy=?,payload_json=?',
            (dirty, '/etc/private', json.dumps({'category':dirty,'qty':3,'entry_price':'.42','fee':'NaN','cost':'Infinity'})))
        signal = {'event_ticker':'EVENT','decision_reason':dirty,'category':'Weather',
            'evidence':[{'label':dirty,'url':'https://example.org/public'},
                        {'url':'https://example.org/private?session_id=SECRET'},
                        {'url':'https://example.org/?api_key=SECRET'},
                        {'url':'https://127.0.0.1/x'}, {'url':'file:///etc/passwd'}]}
        self.db.execute('UPDATE signals SET signal_json=?,baseline_p_yes=?', (json.dumps(signal),'NaN'))
        self.db.executescript('CREATE TABLE settlements(ticker PRIMARY KEY,result,observed_at,market_json); CREATE TABLE positions(ticker);')
        self.db.execute('INSERT INTO settlements VALUES (?,?,?,?)',('T-1','yes',NOW.isoformat(),'{bad json'))
        self.db.execute('INSERT INTO positions VALUES (?)',('T-1',))
        self.db.execute('UPDATE articles SET article_json=?', (json.dumps({'title':dirty,'source':dirty,'url':'https://example.org/home/alice/config?email=alice%40example.com'}),))
        self.db.execute('UPDATE runs SET report_json=?',(json.dumps({'mode':dirty,'counts':{'markets':float('inf')},'errors':[dirty]}),))
        build(self.ledger,self.output)
        data = ''.join(p.read_text() for p in (self.output/'research').glob('*.json'))
        for secret in ('SUPERSECRET','alice','/etc/private','192.168.1.7','SECRET','NaN','Infinity'):
            self.assertNotIn(secret,data)
        self.assertIn('[operational detail omitted]',data)
        decision = self.records('decisions')[0]
        self.assertEqual(decision['qty'],3)
        self.assertEqual(decision['entry_price'],'0.42')
        self.assertNotIn('fee',decision)
        forecast = self.records('forecasts')[0]
        self.assertEqual(forecast['p_yes'],'0.7')
        self.assertIsNone(forecast['baseline_p_yes'])
        self.assertEqual(forecast['result'],'yes')
        self.assertTrue(forecast['paper_position_exists'])
        self.assertEqual(len(forecast['evidence']),1)
        self.db.execute('UPDATE signals SET signal_json=?',('{broken',))
        build(self.ledger,self.output)
        self.assertEqual(len(self.records('forecasts')),1)
        self.assertEqual(self.records('forecasts')[0]['evidence'],[])

    def test_unsafe_output_and_invalid_page_sizes_do_not_write(self):
        self.populate(1)
        private=self.root/'private';private.mkdir();(private/'keep').write_text('unchanged')
        for location in ('root','archive','leaf'):
            with self.subTest(location=location),tempfile.TemporaryDirectory() as tmp:
                output=Path(tmp)/'site'
                if location=='root':output.symlink_to(private,target_is_directory=True)
                else:
                    output.mkdir();folder=output/'research'
                    if location=='archive':folder.symlink_to(private,target_is_directory=True)
                    else:
                        folder.mkdir();(folder/'index.json').symlink_to(private/'keep')
                with self.assertRaises(ValueError):build(self.ledger,output)
                self.assertEqual((private/'keep').read_text(),'unchanged')
        for size in (0,-1,True,1001):
            with self.subTest(size=size),self.assertRaises(ValueError):build(self.ledger,self.output,page_size=size)

    def test_public_article_hash_ids_preserve_news_match_links(self):
        self.populate(1);identifier='a'*64
        self.db.execute('UPDATE articles SET id=?',(identifier,))
        self.db.execute('UPDATE news_matches SET article_id=?',(identifier,))
        build(self.ledger,self.output)
        self.assertEqual(self.records('news')[0]['id'],identifier)
        self.assertEqual(self.records('matches')[0]['article_id'],identifier)

    def test_empty_legacy_tables_manifest_contract(self):
        rows = build(self.ledger, self.output)
        self.assertEqual(self.index(), {'schema_version': 1, 'generated_at': NOW.isoformat(),
            'page_size': 100, 'ordering': 'oldest_first',
            'streams': {k: {'total': 0, 'pages': []} for k in KINDS}})
        self.assertEqual(len(rows), 1)
        for row in rows:
            raw = (self.output / row['path']).read_bytes()
            self.assertEqual(set(row), {'path', 'bytes', 'sha256'})
            self.assertEqual(row['bytes'], len(raw))
            self.assertEqual(row['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertRegex(row['path'], r'^research/(?:index|(?:decisions|forecasts|news|matches|scans)-[0-9]{6})\.json$')
