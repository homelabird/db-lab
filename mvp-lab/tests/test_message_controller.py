"""Host orchestration/cleanup guards; subprocesses are mock Docker, never live."""
from dataclasses import asdict
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from tools import manage
from tools.messages import Session, location, cli
from mvp_app.message_safety import Scope
from mvp_app.message_drill import runtime_settings
SCOPE=Scope('db-lab-mvp','msg-'+'a'*24,'b'*32)

class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'.state').mkdir()
        self.m=types.SimpleNamespace(ROOT=self.root,atomic_json=manage.atomic_json)
        self.lab=Mock();self.lab.c.config={'MVP_PROJECT':SCOPE.project};self.lab.c.engine=['docker'];self.lab.c.inherited={}
        self.lab.binding.return_value={'fingerprint':'pinned'}
        self.directory=location(self.m,SCOPE.run_id);self.directory.mkdir(parents=True)
        self.record={'schema':1,'scope':asdict(SCOPE),'engine':{'fingerprint':'pinned'},'api_id':'c'*64,
                     'scenario':'poison-schema','stage':'planned','resources':{'topics':{}}}
        self.session=Session(self.lab,self.m,self.record,self.directory)
    def test_plan_requires_no_runtime(self):
        args=manage.parser().parse_args(['messages','plan','poison-schema'])
        with patch('sys.stdout',new_callable=io.StringIO) as out:self.assertEqual(cli(args,Mock()),0)
        self.assertEqual(json.loads(out.getvalue())['scenario'],'poison-schema')
    def test_run_requires_explicit_yes(self):
        with patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit) as error:manage.parser().parse_args(['messages','run','poison-schema'])
        self.assertEqual(error.exception.code,2)
    def test_cleanup_requires_explicit_yes(self):
        with patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit):manage.parser().parse_args(['messages','cleanup',SCOPE.run_id])
    def test_path_not_run_id(self):
        with self.assertRaises(ValueError):location(self.m,'../../production')
    def test_symlink_record_refused(self):
        (self.directory/'ledger.json').symlink_to(self.root/'foreign.json')
        with self.assertRaises(ValueError):location(self.m,SCOPE.run_id)
    def test_foreign_engine_refused_before_rpc(self):
        self.lab.binding.return_value={'fingerprint':'elsewhere'}
        with patch('tools.messages.subprocess.run') as proc,self.assertRaises(RuntimeError):self.session.rpc('cleanup')
        proc.assert_not_called()
    def test_changed_project_refused_before_rpc(self):
        self.lab.c.config['MVP_PROJECT']='db-lab-mvp-other'
        with patch('tools.messages.subprocess.run') as proc,self.assertRaises(RuntimeError):self.session.rpc('cleanup')
        proc.assert_not_called()
    def test_wrong_container_ownership_refused_before_rpc(self):
        self.lab.inspect.side_effect=RuntimeError('different service')
        with patch('tools.messages.subprocess.run') as proc,self.assertRaises(RuntimeError):self.session.rpc('cleanup')
        proc.assert_not_called()
    def test_rpc_uses_exact_id_and_stdin_not_shell(self):
        result=types.SimpleNamespace(stdout='{"kind":"result","result":{"ok":true}}\n',returncode=0)
        with patch('tools.messages.subprocess.run',return_value=result) as proc:self.assertTrue(self.session.rpc('inspect')['ok'])
        args=proc.call_args.args[0]
        self.assertEqual(args[:4],['docker','exec','-i','c'*64]);self.assertNotIn('shell',proc.call_args.kwargs)
        self.assertIn('scope',json.loads(proc.call_args.kwargs['input']))
    def test_exit_zero_without_result_not_success(self):
        result=types.SimpleNamespace(stdout='unstructured output',returncode=0)
        with patch('tools.messages.subprocess.run',return_value=result),self.assertRaises(RuntimeError):self.session.rpc('run')
    def test_nonzero_with_result_not_success(self):
        result=types.SimpleNamespace(stdout='{"kind":"result","result":{"status":"passed"}}',returncode=1)
        with patch('tools.messages.subprocess.run',return_value=result),self.assertRaises(RuntimeError):self.session.rpc('run')
    def test_creation_intent_saved_before_rpc(self):
        def rpc(action,**fields):
            self.assertTrue(self.session.path.exists())
            if action=='create-topic':return {'name':fields['topic'],'topic_id':fields['topic']+'-uuid'}
            return {'name':SCOPE.index,'index_uuid':'index-uuid'}
        self.session.rpc=rpc;self.session.create()
        self.assertEqual(self.session.record['stage'],'ready');self.assertEqual(len(self.session.record['resources']['topics']),2)
    def test_missing_uuid_stops_creation(self):
        self.session.rpc=Mock(return_value={'name':SCOPE.topic})
        with self.assertRaises(RuntimeError):self.session.create()
        self.assertTrue(self.session.path.exists());self.assertEqual(self.session.record['resources']['topics'],{})
    def test_failed_cleanup_retains_marker(self):
        self.session.save();self.session.rpc=Mock(side_effect=RuntimeError('wrong uuid'))
        with self.assertRaises(RuntimeError):self.session.cleanup()
        self.assertTrue(self.session.path.exists())
    def test_cleaned_result_removes_only_own_marker(self):
        self.session.save();self.session.rpc=Mock(return_value={'cleaned':True})
        self.assertTrue(self.session.cleanup()['cleaned']);self.assertFalse(self.session.path.exists())
        self.assertEqual(json.loads((self.directory/'ledger.json').read_text())['stage'],'cleaned')
    def test_another_marker_never_cleared_or_touched(self):
        self.session.save();self.m.atomic_json(self.session.path,{'scope':{'run_id':'other'}})
        self.session.rpc=Mock(return_value={'cleaned':True})
        with self.assertRaises(RuntimeError):self.session.cleanup()
        self.session.rpc.assert_not_called();self.assertTrue(self.session.path.exists())
    def test_cleanup_does_not_erase_original_event_log(self):
        self.m.atomic_json(self.directory/'events.json',[{'kind':'observation','stage':'before'}])
        session=Session(self.lab,self.m,self.record,self.directory)
        response=types.SimpleNamespace(stdout='{"kind":"result","result":{"cleaned":true}}',returncode=0)
        with patch('tools.messages.subprocess.run',return_value=response):session.rpc('cleanup')
        self.assertEqual(json.loads((self.directory/'events.json').read_text())[0]['stage'],'before')
    def test_pending_message_blocks_normal_lifecycle_guard(self):
        (self.root/'.state/message-active.json').write_text('{}')
        with patch.object(manage,'ROOT',self.root),self.assertRaises(RuntimeError):manage.require_no_active_fault()
    def test_runtime_settings_reject_non_normal_target(self):
        with patch.dict('os.environ',{'STUDY_PROJECT':SCOPE.project,'SQL_HOST':'external-db'}),self.assertRaises(ValueError):runtime_settings(SCOPE)
    def test_runtime_settings_reject_foreign_project(self):
        with patch.dict('os.environ',{'STUDY_PROJECT':'db-lab-mvp-other'}),self.assertRaises(ValueError):runtime_settings(SCOPE)
