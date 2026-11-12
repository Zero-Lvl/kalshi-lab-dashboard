"""Static web contract checks; Node fixtures here are isolated test data only."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class WebAppTests(unittest.TestCase):
    def text(self, name):
        path = WEB / name
        self.assertTrue(path.exists(), f'missing web/{name}')
        return path.read_text()

    def node(self, source):
        if not shutil.which('node'):
            self.skipTest('Node required for JS behavioral checks')
        js_path = WEB / 'app.js'
        self.assertTrue(js_path.exists(), 'missing web/app.js')
        result = subprocess.run(['node', '-e', 'const app = require(' + json.dumps(str(js_path)) + ');\n' + source], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_shell_is_relative_accessible_and_locked_down(self):
        html = self.text('index.html')
        doc = Document(html)
        ids = {attrs.get('id') for _, attrs in doc.tags}
        self.assertTrue({'load-status', 'dashboard', 'retry', 'as-of', 'observed-at', 'warnings'} <= ids)
        scripts = [attrs for tag, attrs in doc.tags if tag == 'script']
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0]['src'], './app.js')
        self.assertIn('defer', scripts[0])
        self.assertIn('./styles.css', html)
        self.assertIn('Loading', html)
        self.assertIn('Paper only', html)
        csp = next(a['content'] for tag, a in doc.tags if tag == 'meta' and a.get('http-equiv') == 'Content-Security-Policy')
        for directive in ["default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'", "base-uri 'none'", "form-action 'none'"]:
            self.assertIn(directive, csp)
        self.assertNotIn('unsafe-inline', csp)
        for _, attrs in doc.tags:
            self.assertFalse(any(k.startswith('on') for k in attrs))
        css = self.text('styles.css')
        self.assertIn('#101318', css)
        self.assertIn('#191e27', css)
        self.assertIn('18px', css)
        self.assertIn('44px', css)
        self.assertNotIn('@import', css)

    def test_fetch_is_no_store_and_rejects_nonpaper_or_wrong_schema(self):
        js = self.text('app.js')
        self.assertIn("fetch('./dashboard-data.json', { cache: 'no-store' })", js)
        self.assertIn('response.ok', js)
        self.assertIn('textContent', js)
        for unsafe in ['innerHTML', 'insertAdjacentHTML', 'eval(', 'localStorage', 'sendBeacon']:
            self.assertNotIn(unsafe, js)
        self.node("""
const assert = require('node:assert/strict');
const fixture = {schema_version:2,paper_only:true,summary:{},periods:{},performance:{points:[],windows:{}},positions:[],strategies:[]};
assert.equal(app.validateReport(fixture), fixture);
assert.throws(()=>app.validateReport({...fixture,paper_only:false}));
assert.throws(()=>app.validateReport({...fixture,schema_version:1}));
assert.throws(()=>app.validateReport({...fixture,positions:{}}));
assert.equal(app.number(null), null);
assert.equal(app.number(''), null);
assert.equal(app.number('0'), 0);
assert.equal(app.number('4.20'), 4.2);
assert.equal(app.number('Infinity'), null);
assert.equal(app.number(false), null);
""")

    def test_overview_preserves_unknown_marks_and_calendar_report_totals(self):
        html = self.text('index.html')
        for dom_id in ['metric-total', 'metric-realized', 'metric-open', 'metric-cash', 'metric-risk', 'calendar-tallies']:
            self.assertIn(f'id="{dom_id}"', html)
        self.node("""
const assert = require('node:assert/strict');
const data = {summary:{initial_bankroll:'1000', cash:'991.80', open_cost:'8.20', realized_pnl:'0',unrealized_pnl:null,total_pnl:null,unpriced_positions:3,total_positions:3,open_positions:3,wins:0,losses:0,settled:0},periods:{day:{realized_pnl:'0',fees:'0.20',settled:0,wins:0,losses:0},week:{realized_pnl:'0',fees:'0.20',settled:0,wins:0,losses:0},month:{realized_pnl:'0',fees:'0.20',settled:0,wins:0,losses:0}},positions:[]};
let overview = app.overview(data);
assert.equal(overview.total, 'Unavailable');
assert.equal(overview.open, 'Unavailable');
assert.match(overview.unknown, /3.*unpriced/);
assert.equal(overview.realized, '$0.00');
assert.equal(overview.cash, '$991.80');
assert.equal(overview.risk, '$8.20');
assert.equal(overview.periods[0].realized, '$0.00');
assert.equal(overview.periods[0].fees, '$0.20');
assert.equal(overview.periods[0].settled, 0);
assert.equal(overview.periods.length, 3);
assert.equal(app.money(null), 'Unavailable');
assert.equal(app.money('-1.25'), '−$1.25');
data.summary = {...data.summary,unpriced_positions:0,total_pnl:'1.20',unrealized_pnl:'1.20'};
assert.equal(app.overview(data).total, '$1.20');
""")

    def test_chart_uses_real_timestamps_breaks_unknown_segments_and_defaults_realized(self):
        html = self.text('index.html')
        for dom_id in ['performance-chart', 'chart-summary', 'chart-values', 'chart-inception', 'chart-current']:
            self.assertIn(f'id="{dom_id}"', html)
        for window in ['24h', '7d', '1m', '3m']:
            self.assertIn(f'data-window="{window}"', html)
        self.assertIn('data-metric="realized_pnl" aria-pressed="true"', html)
        self.assertIn('createElementNS', self.text('app.js'))
        self.node("""
const assert = require('node:assert/strict');
const times = ['2026-11-11T01:00:00Z','2026-11-11T02:00:00Z','2026-11-11T03:00:00Z','2026-11-11T04:00:00Z','2026-11-11T05:00:00Z'];
const points = times.map((time,i)=>({time,realized_pnl:'0',total_pnl:i===2?null:String(i)}));
const data = {tracking_started_at:times[0],performance:{points:points.slice().reverse(),windows:{'24h':{start:'2026-11-10T05:00:00Z',end:times[4]},'7d':{start:'2026-11-04T05:00:00Z',end:times[4]}}}};
const realized = app.chartModel(data,'24h','realized_pnl');
assert.equal(realized.points.length,5);
assert.deepEqual(realized.points.map(p=>p.value),[0,0,0,0,0]);
assert.equal(realized.start,Date.parse(times[0]));
assert.equal(realized.end,Date.parse(times[4]));
const total = app.chartModel(data,'24h','total_pnl');
assert.deepEqual(total.segments.map(s=>s.map(p=>p.value)),[[0,1],[3,4]]);
assert.equal(total.points[2].value,null);
assert.equal(total.missing,1);
assert.equal(app.chartModel({...data,performance:{...data.performance,points:[]}},'24h','realized_pnl').segments.length,0);
const one = app.chartModel({...data,performance:{...data.performance,points:[points[0]]}},'24h','total_pnl');
assert.equal(one.segments[0].length,1);
assert.equal(one.points.length,1);
""")

    def test_positions_are_complete_searchable_categorized_and_newest_first(self):
        html = self.text('index.html')
        for dom_id in ['positions-list', 'position-search', 'category-filter', 'position-sort', 'visible-count']:
            self.assertIn(f'id="{dom_id}"', html)
        for status in ['all', 'open', 'won', 'lost', 'unresolved']:
            self.assertIn(f'data-status="{status}"', html)
        for category in ['Weather', 'Technology', 'Politics', 'Economics', 'Crypto', 'Other']:
            self.assertIn(f'<option value="{category}">', html)
        self.node("""
const assert = require('node:assert/strict');
const positions = [
 {ticker:'CHI',title:'Chicago daily high above 60°F?',category:'weather',side:'yes',opened_at:'2026-11-11T01:00:00Z',status:'open',cost:'3.20',estimated_net_win:'6.80',estimated_net_loss:'-3.20'},
 {ticker:'AI',title:'Technology launch?',category:'Technology',side:'no',opened_at:'2026-11-11T03:00:00Z',status:'settled',result:'no',payout:'10',realized_pnl:'6.80'},
 {ticker:'POL',title:'Election outcome?',category:'Politics',side:'yes',opened_at:'2026-11-11T02:00:00Z',status:'settled',result:'no',payout:'0',realized_pnl:'-3.20'},
 {ticker:'MISC',title:'Unknown outcome',category:'not a category',opened_at:'2026-11-11T04:00:00Z',status:'unresolved'}
];
assert.deepEqual(app.filterPositions(positions,{}).map(p=>p.ticker),['MISC','AI','POL','CHI']);
assert.equal(app.filterPositions(positions,{search:'chicago',category:'Weather',status:'open'}).length,1);
assert.equal(app.filterPositions(positions,{search:'chi'}).length,1);
assert.equal(app.filterPositions(positions,{status:'won'})[0].ticker,'AI');
assert.equal(app.filterPositions(positions,{status:'lost'})[0].ticker,'POL');
assert.equal(app.filterPositions(positions,{status:'unresolved',category:'Other'}).length,1);
assert.equal(app.filterPositions(positions,{status:'open',search:'not here'}).length,0);
assert.equal(app.filterPositions(positions,{sort:'oldest'})[0].ticker,'CHI');
assert.equal(positions[0].ticker,'CHI'); // never mutate source report
const open = app.positionModel(positions[0]);
assert.equal(open.outcomeLabel,'Projected outcomes · not realized');
assert.match(open.outcomes.map(x=>x[0]).join(' '),/Projected net if win/);
assert.ok(!open.outcomes.some(x=>x[0]==='Actual net P&L'));
const won = app.positionModel(positions[1]);
assert.equal(won.status,'won');
assert.deepEqual(won.outcomes,[['Actual payout','$10.00'],['Actual net P&L','$6.80']]);
assert.equal(app.positionStatus({...positions[1],realized_pnl:'-0.01'}),'won'); // winning contract may lose net after fees
assert.equal(app.safeURL('javascript:alert(1)'),null);
assert.equal(app.safeURL('https://user:secret@example.com/'),null);
assert.equal(app.safeURL('//example.com'),null);
assert.equal(app.safeURL('https://example.com/source'),'https://example.com/source');
assert.equal(app.safeURL('http://example.com/'),'http://example.com/');
assert.equal(app.safeURL('data:text/html,evil'),null);
""")

    def test_strategy_states_are_readonly_and_never_promoted_to_validated_edge(self):
        html = self.text('index.html')
        for dom_id in ['strategies-list', 'strategy-heading', 'adaptation-note', 'coverage-list', 'snapshot-counts']:
            self.assertIn(f'id="{dom_id}"', html)
        self.assertIn('Experimental / unvalidated', html)
        self.assertIn('Counterfactual exploration', html)
        self.assertIn('This page cannot change adaptation states', html)
        self.node("""
const assert = require('node:assert/strict');
const model = app.strategyModel({id:'weather-v1',category:'Weather',state:'paused',settled_events:0,brier:null,baseline_brier:null,realized_pnl:'0',note:'Awaiting outcomes'});
assert.equal(model.state,'paused');
assert.equal(model.brier,'Unavailable');
assert.equal(model.baseline,'Unavailable');
assert.equal(model.settled,'0');
assert.equal(model.pnl,'$0.00');
assert.equal(model.note,'Awaiting outcomes');
assert.equal(app.strategyModel({}).state,'State unavailable');
assert.equal(app.strategyModel({brier:'0.2183'}).brier,'0.2183');
""")

    def test_csv_exports_only_view_with_formula_and_quote_protection(self):
        self.assertIn('id="export-csv"', self.text('index.html'))
        js = self.text('app.js')
        for token in ['new Blob', 'createObjectURL', 'revokeObjectURL', 'text/csv']:
            self.assertIn(token, js)
        self.node(r"""
const assert = require('node:assert/strict');
assert.equal(app.csvCell('=1+1'), '"\'=1+1"');
assert.equal(app.csvCell(' @SUM(A1)'), '"\' @SUM(A1)"');
assert.equal(app.csvCell('\t+cmd'), '"\'\t+cmd"');
assert.equal(app.csvCell('-1.25'), '"\'-1.25"');
assert.equal(app.csvCell('one "two"'), '"one ""two' + '"'.repeat(3));
assert.equal(app.csvCell(null), '""');
const positions = [{ticker:'A',title:'=DANGEROUS',status:'open',category:'Weather'},{ticker:'B',title:'Second',status:'unresolved'}];
const view = app.filterPositions(positions,{status:'open'});
const csv = app.positionsCSV(view);
assert.ok(csv.includes("'=DANGEROUS"));
assert.ok(!csv.includes('Second'));
assert.ok(csv.includes('estimated_net_win'));
assert.ok(csv.includes('realized_pnl'));
assert.equal(csv.split('\r\n').length,2);
""")

    def test_phone_chart_keeps_coordinate_scale_and_labels_readable(self):
        self.node("""
const assert = require('node:assert/strict');
const model = {start:100,end:200,points:[{timestamp:100,value:0},{timestamp:200,value:0}]};
const geometry = app.chartGeometry(model,300);
assert.equal(geometry.width,300);
assert.equal(geometry.height,180);
assert.equal(geometry.x(model.points[0]),75);
assert.equal(geometry.x(model.points[1]),288);
assert.ok(Number.isFinite(geometry.y(0)));
assert.equal(geometry.ticks.length,3);
assert.equal(app.chartGeometry(model,900).height,240);
""")
        css = self.text('styles.css')
        self.assertNotIn('.chart-axis { font-size: 21px; }', css)
        self.assertNotIn('viewBox: \'0 0 1000 260\'', self.text('app.js'))


if __name__ == '__main__':
    unittest.main()
