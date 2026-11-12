"""Research archive UI: local Node behavior tests; no network or private data."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ResearchUITests(unittest.TestCase):
    def node(self, source):
        if not shutil.which('node'):
            self.skipTest('Node required')
        result = subprocess.run(['node', '-e', 'const assert=require("node:assert/strict"); const app=require(' + json.dumps(str(ROOT / 'web/app.js')) + ');\n' + source], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_and_page_only_models(self):
        html = (ROOT / 'web/index.html').read_text()
        for key in ['heading', 'load', 'stream', 'search', 'action', 'status', 'range', 'list', 'older', 'newer', 'retry', 'csv']:
            self.assertIn('id="research-' + key + '"', html)
        self.assertIn('href="#research-heading"', html)
        self.node("""
const records=[{id:1,action:'enter',ticker:'A',reason:'one'},{id:2,action:'reject',ticker:'B',reason:'two'}];
assert.deepEqual(app.researchFilter(records,{search:'two',action:'rejected'}).map(r=>r.id),[2]);
assert.deepEqual(app.researchFilter(records,{search:'',action:'all'}).map(r=>r.id),[2,1]);
assert.match(app.researchRecordModel({id:1,p_yes:0.7,result:null,paper_position_exists:true},'forecasts').note,/Waiting for official/);
assert.match(app.researchRecordModel({id:1,p_yes:0.7,result:null,paper_position_exists:true},'forecasts').note,/does not mean this forecast/);
assert.match(app.researchRecordModel({id:1,p_yes:0.7},'forecasts').probability,/estimate.*70.0%/);
assert.match(app.researchCSV([{id:1,title:'=BAD',reason:'@BAD'}],'news'),/\\'=BAD/);
assert.equal(records[0].id,1);
""")

    def test_dom_lazy_paging_xss_and_retry(self):
        self.node(r"""
class Node {
 constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.textContent='';this.hidden=false;this.disabled=false;this.value='';this.attrs={};}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=nodes;}
 setAttribute(k,v){this.attrs[k]=v;}
 addEventListener(k,v){this.listeners[k]=v;}
 get childElementCount(){return this.children.length;}
}
const nodes=new Map();
global.document={getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);},createElement:tag=>new Node(tag)};
const streams=Object.fromEntries(['decisions','forecasts','news','matches','scans'].map(s=>[s,{total:0,pages:[]}]));
streams.news={total:3,pages:[{path:'research/news-000001.json',count:2},{path:'research/news-000002.json',count:1}]};
const idx={schema_version:1,generated_at:'2026-11-11T00:00:00Z',page_size:2,ordering:'oldest_first',streams};
const requests=[];let fail=false,grown=false;
global.fetch=async(path,options)=>{
 requests.push(path);assert.equal(options.redirect,'error');assert.equal(options.credentials,'omit');
 if(fail)return {ok:false,status:404};
 const data=path.endsWith('index.json')?idx:{schema_version:1,stream:'news',page:path.includes('000002')?2:1,records:path.includes('000002')?[{id:3,title:'<img src=x onerror=alert(1)>',url:'javascript:alert(1)'}]:[{id:1,title:'old'},{id:2,title:'middle',url:'https://example.test/article'}]};
 if(grown&&!path.endsWith('index.json')&&path.includes('000002'))data.records.push({id:4,title:'new append'});
 return {ok:true,json:async()=>JSON.parse(JSON.stringify(data))};
};
const get=id=>document.getElementById('research-'+id);
const all=n=>[n,...n.children.flatMap(all)];
(async()=>{
 app.initResearch();assert.equal(requests.length,0);
 get('stream').listeners.change({target:{value:'news'}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(requests,['./research/index.json','./research/news-000002.json']);
 assert.match(get('range').textContent,/records 1–1 of 3 total/);
 assert.match(all(get('list')).map(n=>n.textContent).join(' '),/<img src=x/);
 assert.equal(all(get('list')).filter(n=>n.tag==='img'||n.tag==='a').length,0);
 get('older').listeners.click();await new Promise(resolve=>setImmediate(resolve));
 assert.match(get('range').textContent,/records 2–3 of 3 total/);
 assert.equal(get('older').disabled,true);assert.equal(get('newer').disabled,false);
 assert.equal(all(get('list')).filter(n=>n.tag==='a')[0].rel,'noopener noreferrer');
 get('search').listeners.input({target:{value:'absent'}});
 assert.match(get('range').textContent,/0 shown on this page/);
 assert.equal(get('csv').disabled,true);
 // A new snapshot grows the partial last page while this browser stays open.
 grown=true;streams.news.total=4;streams.news.pages[1].count=2;
 get('search').listeners.input({target:{value:''}});
 get('newer').listeners.click();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(get('retry').hidden,true,'fresh index must recover append-only updates');
 assert.match(get('range').textContent,/records 1–2 of 4 total/);
 assert.ok(requests.filter(p=>p.endsWith('index.json')).length>=3);
 fail=true;get('newer').listeners.click();await new Promise(resolve=>setImmediate(resolve));
 assert.match(get('status').textContent,/unavailable.*404/);assert.equal(get('retry').hidden,false);
 fail=false;get('retry').listeners.click();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(get('retry').hidden,true);
 get('stream').listeners.change({target:{value:'scans'}});await new Promise(resolve=>setImmediate(resolve));
 assert.match(get('range').textContent,/0 records/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_archive_contract_rejects_unsafe_paths_and_malformed_pages(self):
        self.node("""
assert.equal(app.researchPath('research/index.json'),'./research/index.json');
for (const path of ['../private.json','research/decisions-000001.json?x','https://evil.test/research/index.json','./research/index.json','research/scans-1.json','research/news-000001.json/../secret']) assert.throws(()=>app.researchPath(path));
const streams=Object.fromEntries(['decisions','forecasts','news','matches','scans'].map(s=>[s,{total:0,pages:[]}]));
const idx={schema_version:1,generated_at:'2026-11-11T00:00:00Z',page_size:2,ordering:'oldest_first',streams};
assert.equal(app.validateResearchIndex(idx),idx);
assert.throws(()=>app.validateResearchIndex({...idx,ordering:'newest_first'}));
assert.throws(()=>app.validateResearchIndex({...idx,streams:{...streams,news:{total:1,pages:[{path:'research/scans-000001.json',count:1}]}}}));
assert.throws(()=>app.validateResearchPage({schema_version:1,stream:'news',page:1,records:[null]},'news',1));
assert.throws(()=>app.validateResearchPage({schema_version:1,stream:'news',page:2,records:[]},'news',1));
const page={schema_version:1,stream:'news',page:1,records:[{id:1,title:'old'},{id:2,title:'new'}]};
assert.deepEqual(app.validateResearchPage(page,'news',1).records.map(r=>r.id),[1,2]);
assert.throws(()=>app.validateResearchPage({...page,records:[{id:{evil:1}}]},'news',1));
""")
