"""Publication safety using only temporary local Git repositories."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from site_builder import PUBLIC_FILES,build_site
import publisher


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'));self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'reports').mkdir()
        self.data={'schema_version':2,'paper_only':True,'positions':[]}
        self.rebuild()
        self.calls=[]
    def rebuild(self):
        manifest=build_site(self.data,self.root/'site')
        (self.root/'reports/site-manifest.json').write_text(json.dumps(manifest))
    def approve(self,**overrides):
        approval=dict(approved=True,repository=publisher.REPOSITORY,branch='main',path='/')
        approval.update(overrides)
        (self.root/'.release-approval.json').write_text(json.dumps(approval))
    def git(self,*args):
        return subprocess.check_output(['git','-C',str(self.work),*args],text=True,stderr=subprocess.STDOUT).strip()
    def fixture(self):
        self.work=self.root/'.pages-repo';self.work.mkdir()
        self.remote=self.root/'remote.git'
        subprocess.check_output(['git','init','--bare',str(self.remote)],stderr=subprocess.STDOUT)
        self.git('init','-b','main');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid')
        self.git('remote','add','origin',publisher.REMOTE)
        (self.work/'.nojekyll').write_bytes(b'')
        self.commit('Fixture seed')
        self.git('push',str(self.remote),'HEAD:refs/heads/main')
    def commit(self,message):
        self.git('add','--all');self.git('commit','-m',message)
    def runner(self,cmd):
        self.calls.append(cmd)
        if cmd[0]==publisher.GH:
            return subprocess.check_output(['git','--git-dir',str(self.remote),'rev-parse','refs/heads/main'],text=True).strip()
        # Never execute a network transport: only these temporary bare repos.
        if cmd[0]!='git' or cmd[1:3]!=['-C',str(self.work)]:
            raise AssertionError('unexpected command: '+repr(cmd))
        args=cmd[3:]
        if args[0]=='pull':return self.git('pull','--ff-only',str(self.remote),'main')
        if args[0]=='fetch':
            return self.git('fetch','--no-tags',str(self.remote),'+refs/heads/main:refs/remotes/origin/main')
        if args[0]=='push':return self.git('push',str(self.remote),'HEAD:refs/heads/main')
        return self.git(*args)
    def assert_no_push(self):
        self.assertFalse(any(c[3:4]==['push'] for c in self.calls))
    def test_unapproved_publication_makes_no_external_calls(self):
        with self.assertRaises(ValueError):publisher.publish(self.root,run=lambda c:self.calls.append(c))
        self.assertEqual(self.calls,[])
    def test_wrong_approval_destination_fails_before_commands(self):
        for key,value in [('repository','someone/private'),('branch','other'),('path','/engine')]:
            with self.subTest(key=key):
                self.approve(**{key:value})
                with self.assertRaises(ValueError):publisher.publish(self.root,run=lambda c:self.calls.append(c))
                self.assertEqual(self.calls,[])
    def test_tampered_artifacts_fail_before_commands(self):
        self.approve();(self.root/'site/dashboard-data.json').write_text('{}')
        with self.assertRaises(ValueError):publisher.publish(self.root,run=lambda c:self.calls.append(c))
        self.assertEqual(self.calls,[])
    def test_public_artifacts_pushed_without_force(self):
        self.approve();self.fixture()
        result=publisher.publish(self.root,run=self.runner)
        self.assertEqual(result['commit'],self.git('rev-parse','HEAD'))
        self.assertEqual(result['status'],'snapshot_pushed')
        self.assertEqual(set(self.git('ls-files').splitlines()),set(PUBLIC_FILES))
        self.assertEqual((self.work/'dashboard-data.json').read_bytes(),(self.root/'site/dashboard-data.json').read_bytes())
        self.assertTrue(any(c[3:]==['push','origin','HEAD:refs/heads/main'] for c in self.calls))
        self.assertFalse(any('--force' in c for c in self.calls))
    def seed_engine(self,files):
        for name in files:
            path=self.work/'engine'/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('frozen reviewed fixture source\n')
        self.commit('Reviewed engine snapshot')
        self.git('push',str(self.remote),'HEAD:refs/heads/main')
    def test_approved_frozen_engine_snapshot_preserved(self):
        self.approve(source_publication_approved=True);self.fixture()
        files=['publisher.py','README.md','.gitignore','engine.py','tests/test_engine.py',
               'web/index.html','web/app.js','web/styles.css']
        self.seed_engine(files)
        before=self.git('rev-parse','HEAD:engine')
        result=publisher.publish(self.root,run=self.runner)
        self.assertEqual(result['status'],'snapshot_pushed')
        remote_tree=subprocess.check_output(['git','--git-dir',str(self.remote),'rev-parse','main:engine'],text=True).strip()
        self.assertEqual(remote_tree,before)
        self.assertEqual(self.git('rev-parse','HEAD:engine'),before)
        self.assertEqual(set(self.git('diff','HEAD^','HEAD','--name-only').splitlines()),set(PUBLIC_FILES)-{'.nojekyll'})
    def test_engine_requires_exact_true_approval(self):
        self.fixture();self.seed_engine(['engine.py'])
        for flag in [None,False,1,'true']:
            with self.subTest(flag=flag):
                self.approve(source_publication_approved=flag);self.calls.clear()
                with self.assertRaises(ValueError):publisher.publish(self.root,run=self.runner)
                self.assert_no_push()
    def test_engine_private_paths_rejected_even_with_approval(self):
        self.approve(source_publication_approved=True);self.fixture()
        for name in ['private.db','data/raw.json','.env','.hermes/config.yaml','other.py',
                     'tests/helpers.py','tests/sub/test_nested.py','web/secret.json']:
            with self.subTest(name=name):
                path=self.work/'engine'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private fixture')
                self.calls.clear()
                with self.assertRaises(ValueError):publisher.publish(self.root,run=self.runner)
                self.assert_no_push();path.unlink()
                # Remove only empty temporary fixture directories.
                parent=path.parent
                while parent!=self.work and parent.exists() and not any(parent.iterdir()):
                    parent.rmdir();parent=parent.parent
    def test_effective_push_insteadof_rewrite_rejected(self):
        self.approve();self.fixture()
        self.git('config','url.https://elsewhere.invalid/.pushInsteadOf','https://github.com/')
        with self.assertRaises(ValueError):publisher.publish(self.root,run=self.runner)
        self.assert_no_push()
    def test_workspace_symlinks_and_private_files_rejected(self):
        self.approve(source_publication_approved=True);self.fixture()
        target=self.root/'private-target';target.write_text('private fixture')
        for name in ['index.html','engine','engine/engine.py','engine/tests','engine/web/app.js']:
            with self.subTest(name=name):
                path=self.work/name;path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to(target)
                self.calls.clear()
                with self.assertRaises(ValueError):publisher.publish(self.root,run=self.runner)
                self.assert_no_push();self.assertEqual(target.read_text(),'private fixture');path.unlink()
        (self.work/'private.db').write_text('private fixture');self.calls.clear()
        with self.assertRaises(ValueError):publisher.publish(self.root,run=self.runner)
        self.assert_no_push();self.assertTrue((self.work/'private.db').exists())
    def test_remote_commit_readback_mismatch_fails(self):
        self.approve();self.fixture()
        def mismatch(cmd):
            return 'wrong-sha' if cmd[0]==publisher.GH else self.runner(cmd)
        with self.assertRaisesRegex(RuntimeError,'readback'):
            publisher.publish(self.root,run=mismatch)
    def test_clean_unpublished_history_rejected_before_update(self):
        self.approve();self.fixture()
        (self.work/'private.db').write_text('private fixture bytes')
        self.commit('Private historical data')
        (self.work/'private.db').unlink();self.commit('Remove private data')
        head=self.git('rev-parse','HEAD')
        self.assertEqual(self.git('status','--porcelain'),'')
        with self.assertRaisesRegex(ValueError,'unpublished'):
            publisher.publish(self.root,run=self.runner)
        self.assert_no_push()
        self.assertEqual(self.git('rev-parse','HEAD'),head)
        self.assertFalse((self.work/'dashboard-data.json').exists())
    def test_mismatched_or_additional_effective_pushurls_rejected_before_upload(self):
        self.approve();self.fixture()
        for urls in [('https://elsewhere.invalid/private.git',),(publisher.REMOTE,'https://elsewhere.invalid/private.git'),(publisher.REMOTE,publisher.REMOTE)]:
            with self.subTest(urls=urls):
                subprocess.run(['git','-C',str(self.work),'config','--unset-all','remote.origin.pushurl'],check=False)
                for url in urls:self.git('config','--add','remote.origin.pushurl',url)
                self.calls.clear()
                with self.assertRaises(ValueError):publisher.publish(self.root,run=self.runner)
                self.assert_no_push()
