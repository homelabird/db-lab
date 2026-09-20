"""Source gates and Ansible variable contracts; not actual Ansible/DB results."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('db_lab_quality_gate',ROOT/'scripts/quality.py')
quality=importlib.util.module_from_spec(spec);spec.loader.exec_module(quality)


class RootPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'project';self.root.mkdir()
        self.outside=self.base/'outside';self.outside.mkdir()
        shutil.copy2(ROOT/'all.sh',self.root/'all.sh')
        (self.root/'scripts').mkdir();(self.root/'scripts/control.py').write_text('')
        (self.root/'elasticsearch').mkdir()
        (self.root/'elasticsearch/lab.sh').write_text('echo called >> child-was-called\nexit 0\n')
        (self.root/'elasticsearch/.env.example').write_text('X=test\n')
    def execute(self):
        return subprocess.run(['bash',str(self.root/'all.sh'),'init','es'],cwd=self.root,capture_output=True,timeout=10)
    def test_state_link_does_not_write_outside(self):
        (self.root/'.state').symlink_to(self.outside,target_is_directory=True)
        self.assertNotEqual(self.execute().returncode,0);self.assertEqual(list(self.outside.iterdir()),[])
    def test_report_link_does_not_write_outside(self):
        (self.root/'reports').symlink_to(self.outside,target_is_directory=True)
        self.assertNotEqual(self.execute().returncode,0);self.assertEqual(list(self.outside.iterdir()),[])
    def test_lock_link_does_not_touch_existing_file(self):
        (self.root/'.state').mkdir();(self.outside/'lock').write_text('original')
        (self.root/'.state/all.lock').symlink_to(self.outside/'lock')
        self.assertNotEqual(self.execute().returncode,0);self.assertEqual((self.outside/'lock').read_text(),'original')
    def test_normal_directory_still_initializes(self):
        self.assertEqual(self.execute().returncode,0);self.assertTrue((self.root/'elasticsearch/.env').is_file())


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def manifest(self):
        records={p.relative_to(self.root).as_posix():{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'mode':p.stat().st_mode & 0o777}
                 for p in quality.source_files(self.root) if p.name!=quality.MANIFEST}
        (self.root/quality.MANIFEST).write_text(json.dumps({'schema':1,'files':records}))
    def test_duplicate_methods_detected(self):
        tree=ast.parse('class T:\n def test_x(self): pass\n def test_x(self): pass\n')
        self.assertEqual(quality.duplicate_tests(tree)[0]['method'],'test_x')
    def test_same_test_name_in_different_class_allowed(self):
        tree=ast.parse('class A:\n def test_x(self): pass\nclass B:\n def test_x(self): pass\n')
        self.assertEqual(quality.duplicate_tests(tree),[])
    def test_duplicate_yaml_rejected(self):
        with self.assertRaises(ValueError):quality.yaml_documents('x: 1\nx: 2\n')
    def test_yaml_merge_override_allowed(self):
        docs=quality.yaml_documents('base: &base\n  x: 1\nchild:\n  <<: *base\n  x: 2\n')
        self.assertEqual(docs[0]['child']['x'],2)
    def test_multidocument_yaml(self):
        self.assertEqual(len(quality.yaml_documents('x: 1\n---\nx: 2\n')),2)
    def test_dead_local_link_detected(self):
        self.assertEqual(quality.local_links(self.root/'README.md','[bad](absent.md)'),['absent.md'])
    def test_external_link_not_requested(self):
        self.assertEqual(quality.local_links(self.root/'README.md','[web](https://example.com/file.md)'),[])
    def test_broken_python_fails_static(self):
        (self.root/'broken.py').write_text('def :')
        self.assertEqual(quality.static_check(self.root)['status'],'failed')
    def test_overwritten_test_fails_static(self):
        (self.root/'test_a.py').write_text('class T:\n def test_x(self): pass\n def test_x(self): pass\n')
        self.assertEqual(quality.static_check(self.root)['issues'][0]['error'],'overwritten_test_method')
    def test_clean_manifest_passes(self):
        (self.root/'x.py').write_text('x=1\n');self.manifest()
        self.assertEqual(quality.release_check(self.root)['status'],'passed')
    def test_modified_source_detected(self):
        p=self.root/'x.py';p.write_text('x=1\n');self.manifest();p.write_text('x=2\n')
        self.assertEqual(quality.release_check(self.root)['issues'][0]['error'],'sha256_mismatch')
    def test_unlisted_source_detected(self):
        self.manifest();(self.root/'new.py').write_text('x=1\n')
        self.assertEqual(quality.release_check(self.root)['issues'][0]['error'],'unlisted_source_file')
    def test_manifest_traversal_refused(self):
        (self.root/quality.MANIFEST).write_text(json.dumps({'schema':1,'files':{'../x':{}}}))
        with self.assertRaises(ValueError):quality.release_check(self.root)
    def test_manifest_symlink_refused(self):
        p=self.root/'x.py';p.write_text('x=1\n');self.manifest();p.unlink();p.symlink_to(self.root/'other.py')
        with self.assertRaises(ValueError):quality.release_check(self.root)
    def test_runtime_output_does_not_break_manifest(self):
        self.manifest();(self.root/'reports').mkdir();(self.root/'reports/runtime.json').write_text('{}')
        self.assertEqual(quality.release_check(self.root)['status'],'passed')
    def test_missing_tool_is_blocked_not_pass(self):
        r=quality.command_check('tool',['/no-such-db-lab-executable'],self.root,self.root)
        self.assertEqual((r['status'],r['returncode']),('blocked',127))
    def test_empty_test_run_not_pass(self):
        r=quality.command_check('host-zero',[sys.executable,'-c','pass'],self.root,self.root)
        self.assertEqual(r['status'],'failed')
    def test_timeout_never_passes(self):
        r=quality.command_check('tool',[sys.executable,'-c','import time;time.sleep(5)'],self.root,self.root,timeout=.05)
        self.assertEqual((r['status'],r['returncode']),('failed',124))
    def test_blocked_junit_is_error(self):
        rc=quality.write_report(self.root,'tools',[{'name':'ansible','status':'blocked','returncode':127}])
        self.assertEqual(rc,127);self.assertIn('<error', (self.root/'junit.xml').read_text())
    def test_failure_precedes_blocked(self):
        self.assertEqual(quality.overall([{'status':'failed'},{'status':'blocked'}]),('failed',1))


class AnsibleAndCIQualityTests(unittest.TestCase):
    def test_play_does_not_shadow_inventory_defaults(self):
        play=yaml.safe_load((ROOT/'ansible/control.yml').read_text())[0]
        self.assertFalse(play.get('vars'))
        task=play['tasks'][0]['db_lab_control']
        for key in ('target','request','allow_changes','allow_faults','timeout'):
            self.assertIn('default(omit)',task[key])
    def test_collector_destination_can_come_from_inventory(self):
        play=yaml.safe_load((ROOT/'ansible/collect.yml').read_text())[0]
        self.assertFalse(play.get('vars'))
        self.assertIn('default(',play['tasks'][2]['ansible.builtin.fetch']['dest'])
    def test_automatic_ci_never_runs_db_or_remote_faults(self):
        flow=yaml.safe_load((ROOT/'.github/workflows/quality.yml').read_text())
        self.assertIn('push',flow.get('on',flow.get(True)))
        self.assertEqual(flow['permissions'],{'contents':'read'})
        text=(ROOT/'.github/workflows/quality.yml').read_text()
        for forbidden in ('mvp up','verify run','simulate run','ci-mvp.py','secrets.'):
            self.assertNotIn(forbidden,text)
        self.assertIn('bash scripts/test-ansible.sh',text)
    def test_offline_dependency_closure_includes_requests(self):
        text=(ROOT/'scripts/requirements-checks.txt').read_text()
        self.assertIn('requests==',text)
    def test_declared_live_tests_cover_precedence_and_collection(self):
        text=(ROOT/'ansible/tests/test_playbooks_live.py').read_text()
        for name in ('test_inventory_request_is_not_shadowed','test_inventory_consent','test_real_fetch_collects'):
            self.assertIn('def '+name,text)
