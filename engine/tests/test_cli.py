import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from test_engine import engine, NOW, ROOT


class CLITests(unittest.TestCase):
    def test_cli_settlement_only_exposes_sanitized_site_without_entry_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'paper.db'
            ledger=engine().Ledger(db);ledger.close()
            result=subprocess.run([sys.executable,str(ROOT/'engine.py'),'scan','--settlement-only',
                '--db',str(db),'--reports',str(Path(tmp)/'reports')],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            export=json.loads((Path(tmp)/'site/dashboard-data.json').read_text())
            self.assertEqual(export['schema_version'],2)
            self.assertEqual(export['positions'],[])
            self.assertTrue((Path(tmp)/'site/app.js').exists())
            self.assertNotIn('signal_json',str(export))

    def test_report_cli_produces_real_readonly_ledger_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'paper.db'
            ledger = engine().Ledger(db)
            ledger.close()
            before = db.read_bytes()
            dest = Path(tmp)/'reports'
            r = subprocess.run([sys.executable,str(ROOT/'engine.py'),'report','--db',str(db),
                                '--reports',str(dest)],capture_output=True,text=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertIn('PAPER ONLY',r.stdout)
            self.assertTrue((dest/'latest.json').exists())
            report = json.loads((dest/'latest.json').read_text())
            self.assertEqual(report['summary']['cash'],'1000.00')
            public=json.loads((Path(tmp)/'site/dashboard-data.json').read_text())
            self.assertTrue(any(s['id']=='crypto-btc-terminal' and s['state']=='warming_up' for s in public['strategies']))
            self.assertTrue((dest/'index.html').exists())
            self.assertTrue((dest/'positions.csv').exists())
            self.assertEqual(db.read_bytes(),before)
            full=subprocess.run([sys.executable,str(ROOT/'engine.py'),'report','--db',str(db),
                                 '--reports',str(dest),'--full'],capture_output=True,text=True,timeout=15)
            self.assertEqual(full.returncode,0,full.stderr)
            self.assertIn('Every paper result settled',full.stdout)
            self.assertNotIn('MEDIA:',full.stdout)

    def test_static_dashboard_escapes_external_text_and_rejects_script_urls(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('dashboard'),'dashboard missing')
        from dashboard import build_dashboard
        r = {'summary':{'cash':'1000'}, 'news':{'articles':[{'title':'<script>alert(1)</script>',
              'url':'javascript:alert(1)', 'source':'Bad', 'published_at':'now'}]},
             'counts':{}, 'positions':[], 'watchlist':[], 'errors':[]}
        html = build_dashboard(r)
        self.assertNotIn('<script>',html)
        self.assertNotIn('href="javascript:',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertIn('viewport',html)
        self.assertNotIn('<script src=',html)
