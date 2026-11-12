import json
import tempfile
import unittest
from pathlib import Path


class SiteBuildTests(unittest.TestCase):
    def test_site_build_contains_only_allowlisted_frontend_and_sanitized_export(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('site_builder'),'site build missing')
        from site_builder import build_site
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);assets=root/'web';assets.mkdir()
            for name in ('index.html','app.js','styles.css'):(assets/name).write_text('asset '+name)
            (assets/'private.db').write_text('must not copy')
            data={'schema_version':2,'generated_at':'2026-11-11T20:00:00Z','summary':{},
                  'positions':[],'performance':{'points':[]},'warnings':[]}
            output=root/'site'
            manifest=build_site(data,output,assets)
            self.assertEqual({p.name for p in output.iterdir()},
                             {'index.html','app.js','styles.css','dashboard-data.json','.nojekyll'})
            self.assertEqual(json.loads((output/'dashboard-data.json').read_text()),data)
            self.assertTrue(all(row['sha256'] and row['bytes']>=0 for row in manifest))
            with self.assertRaises(ValueError):build_site(dict(data,raw_snapshots=[]),output,assets)
            with self.assertRaises(ValueError):build_site(dict(data,positions=[{'signal_json':'private'}]),output,assets)
    def test_rebuild_preserves_safe_research_directory_and_five_base_rows(self):
        from site_builder import build_site, PUBLIC_FILES
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);research=root/'research';research.mkdir()
            page=research/'news-000001.json';page.write_text('{}')
            manifest=build_site({'schema_version':2},root)
            self.assertEqual(len(manifest),5)
            self.assertEqual({r['path'] for r in manifest},set(PUBLIC_FILES))
            self.assertEqual(page.read_text(),'{}')
