"""Build artifact-only static website; never copy the engine, raw reports or DB."""
import hashlib
import json
import re
from pathlib import Path

ASSETS=('index.html','app.js','styles.css')
PUBLIC_FILES=(*ASSETS,'dashboard-data.json','.nojekyll')
TOP_FIELDS={'schema_version','generated_at','observed_at','tracking_started_at','paper_only',
            'summary','periods','performance','positions','coverage','counts','strategies','warnings'}
PRIVATE_KEYS={'signal_json','market_json','article_json','raw_snapshots','raw_articles','db_path',
              'api_key','access_token','password','secret','environment','local_path'}

RESEARCH_STREAMS=('decisions','forecasts','news','matches','scans')

def is_public_path(name):
    return isinstance(name,str) and (name in PUBLIC_FILES or re.fullmatch(
        r'research/(?:index|(?:decisions|forecasts|news|matches|scans)-[0-9]{6})\.json',name) is not None)

def site_files(root):
    root=Path(root)
    if root.is_symlink():raise ValueError('symlink website output')
    if not root.exists():return set()
    files=set()
    for item in root.iterdir():
        if item.is_symlink():raise ValueError('symlink public artifact')
        if item.name=='research' and item.is_dir():
            for page in item.iterdir():
                name='research/'+page.name
                if page.is_symlink() or not page.is_file() or not is_public_path(name):
                    raise ValueError('unexpected research path; preserved, not published')
                files.add(name)
        elif item.name in PUBLIC_FILES and item.is_file():files.add(item.name)
        else:raise ValueError('unexpected files in website output; will not sweep or publish them')
    return files


def build_site(data, output, asset_dir=None):
    output=Path(output)
    asset_dir=Path(asset_dir or Path(__file__).resolve().parent/'web')
    if data.get('schema_version')!=2 or set(data)-TOP_FIELDS:
        raise ValueError('not an allowlisted public export')
    def check(value):
        if isinstance(value,dict):
            if PRIVATE_KEYS.intersection(value): raise ValueError('private fields in public export')
            for item in value.values():check(item)
        elif isinstance(value,list):
            for item in value:check(item)
        elif isinstance(value,str) and ('/home/wizzy/' in value or '\\.hermes\\' in value):
            raise ValueError('local path in public export')
    check(data)
    encoded=json.dumps(data,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode()
    payloads={name:(asset_dir/name).read_bytes() for name in ASSETS}
    payloads.update({'dashboard-data.json':encoded,'.nojekyll':b''})
    site_files(output)
    output.mkdir(parents=True,exist_ok=True)
    manifest=[]
    for name,body in payloads.items():
        temporary=output/('.'+name+'.new')
        temporary.write_bytes(body);temporary.replace(output/name)
        manifest.append({'path':name,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()})
    return manifest
