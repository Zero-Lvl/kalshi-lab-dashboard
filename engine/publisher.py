"""Approved public artifacts with an optional frozen, reviewed engine snapshot."""
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

from site_builder import PUBLIC_FILES,build_site,is_public_path,site_files

REPOSITORY='Zero-Lvl/kalshi-lab-dashboard'
REMOTE='https://github.com/'+REPOSITORY+'.git'
GH=shutil.which('gh') or str(Path.home()/'.local/bin/gh')
# Explicit frozen snapshot surface; never copy these from the running backend.
ENGINE_ROOT_FILES=frozenset({
    'analytics.py','cli.py','dashboard.py','engine.py','news.py','public_http.py',
    'publisher.py','reporting.py','scanner.py','signals.py','site_builder.py',
    'strategies.py','research.py','README.md','.gitignore'})
ENGINE_WEB_FILES=frozenset({'web/index.html','web/app.js','web/styles.css'})


def _engine_source_path(name):
    if not name.startswith('engine/'):return False
    relative=name[len('engine/'):]
    return (relative in ENGINE_ROOT_FILES or relative in ENGINE_WEB_FILES
            or re.fullmatch(r'tests/test_[A-Za-z0-9_]+\.py',relative) is not None)


def _run(command):
    result=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,text=True,timeout=75,check=True)
    return result.stdout.strip()


def publish(root,run=_run):
    root=Path(root).resolve()
    approval_path=root/'.release-approval.json'
    if not approval_path.is_file():raise ValueError('public publication approval is absent')
    approval=json.loads(approval_path.read_text())
    if approval.get('approved') is not True or any(approval.get(k)!=v for k,v in
            {'repository':REPOSITORY,'branch':'main','path':'/'}.items()):
        raise ValueError('publication approval does not match exact destination')
    source=root/'site';work=root/'.pages-repo'
    if source.is_symlink() or work.is_symlink():raise ValueError('symlink artifact workspace rejected')
    if not source.is_dir():raise ValueError('website payload missing')
    actual=site_files(source)
    manifest=json.loads((root/'reports/site-manifest.json').read_text())
    if (not isinstance(manifest,list) or any(not isinstance(r,dict) or not is_public_path(r.get('path')) for r in manifest)
            or len({r['path'] for r in manifest})!=len(manifest)
            or {r['path'] for r in manifest}!=actual or not set(PUBLIC_FILES)<=actual):
        raise ValueError('invalid artifact manifest')
    payloads={}
    for row in manifest:
        p=source/row['path']
        if p.is_symlink() or not p.is_file() or p.stat().st_size>10_000_000:
            raise ValueError('unsafe or oversized artifact')
        data=p.read_bytes()
        if len(data)!=row['bytes'] or hashlib.sha256(data).hexdigest()!=row['sha256']:
            raise ValueError('artifact changed after manifest; rebuild before publishing')
        payloads[row['path']]=data
    from research import validate_research_payloads
    validate_research_payloads(payloads)
    # Reapply the existing privacy/finite-JSON builder gate to exact public bytes.
    scratch=Path(os.environ.get('TMPDIR',Path.home()/'.hermes/cache/scratch'))
    scratch.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='kalshi-public-check-',dir=scratch) as tmp:
        build_site(json.loads(payloads['dashboard-data.json']),tmp)
        if any((Path(tmp)/name).read_bytes()!=payloads[name] for name in PUBLIC_FILES):
            raise ValueError('site bytes differ from current verified builder/assets')
    (root/'data').mkdir(exist_ok=True)
    with (root/'data/publish.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        def git(*args):return run(['git','-C',str(work),*args])
        def validate_workspace():
            if not (work/'.git').is_dir() or (work/'.git').is_symlink():
                raise ValueError('isolated artifact git workspace missing')
            source_approved=approval.get('source_publication_approved') is True
            allowed_top=set(PUBLIC_FILES)|{'.git','research'}|({'engine'} if source_approved else set())
            if set(p.name for p in work.iterdir())-allowed_top:
                raise ValueError('unexpected file in artifact workspace; preserved, not published')
            engine=work/'engine'
            if engine.exists() or engine.is_symlink():
                if not source_approved or engine.is_symlink() or not engine.is_dir():
                    raise ValueError('unapproved or unsafe engine snapshot')
                for p in engine.rglob('*'):
                    name=p.relative_to(work).as_posix()
                    if p.is_symlink() or (p.is_dir() and name not in {'engine/tests','engine/web'}) or (not p.is_dir() and (not p.is_file() or not _engine_source_path(name))):
                        raise ValueError('non-public file or symlink in engine snapshot')
            if any((work/name).is_symlink() for name in PUBLIC_FILES):
                raise ValueError('symlink in artifact workspace rejected')
            archive=work/'research'
            if archive.exists() or archive.is_symlink():
                if archive.is_symlink() or not archive.is_dir():raise ValueError('unsafe research workspace')
                if any(p.is_symlink() or not p.is_file() or not is_public_path('research/'+p.name) for p in archive.iterdir()):
                    raise ValueError('unapproved research workspace file')
            if (git('remote','get-url','--all','origin').splitlines()!=[REMOTE]
                    or git('remote','get-url','--push','--all','origin').splitlines()!=[REMOTE]
                    or git('branch','--show-current')!='main'):
                raise ValueError('artifact remote/branch does not match approval')
            if any(not is_public_path(name) and not (source_approved and _engine_source_path(name))
                   for name in git('ls-files','-z').split('\0') if name):
                raise ValueError('non-public tracked files in artifact workspace')
        validate_workspace()
        if git('status','--porcelain'):raise ValueError('artifact workspace has pending edits; inspect before retrying')
        git('fetch','--no-tags',REMOTE,'+refs/heads/main:refs/remotes/origin/main')
        if git('rev-parse','HEAD')!=git('rev-parse','refs/remotes/origin/main'):
            raise ValueError('unpublished or stale local history; inspect and bootstrap separately')
        validate_workspace()
        for name,data in payloads.items():
            (work/name).parent.mkdir(parents=True,exist_ok=True)
            (work/name).write_bytes(data)
        names=sorted(payloads)
        for start in range(0,len(names),100):git('add','--',*names[start:start+100])
        changed=bool(git('diff','--cached','--name-only'))
        if changed:
            git('-c','user.name=Zero-Lvl','-c','user.email=114039403+Zero-Lvl@users.noreply.github.com',
                'commit','-m','Update paper-research dashboard snapshot')
        sha=git('rev-parse','HEAD')
        git('push','origin','HEAD:refs/heads/main')
        observed=run([GH,'api','repos/'+REPOSITORY+'/commits/main','--jq','.sha'])
        if observed!=sha:raise RuntimeError('remote commit readback does not match pushed snapshot')
        return {'repository':REPOSITORY,'commit':sha,'changed':changed,
                'status':'snapshot_pushed','pages_url':'https://zero-lvl.github.io/kalshi-lab-dashboard/'}


if __name__=='__main__':
    try:
        print(json.dumps(publish(Path(__file__).resolve().parent),sort_keys=True))
    except Exception as exc:
        raise SystemExit('Dashboard publication failed: '+str(exc))
