"""Regression tests, using explicit Redis/Podman mocks except real temporary shell I/O.
These tests do not establish Redis replication, network or container functionality.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'client'), str(ROOT/'scripts'), str(ROOT/'tests')]
import core
import manage
import live_validate
import test_entrypoint
from test_project import load_client_with_stub
CLIENT = load_client_with_stub()


def result(text=''):
    return subprocess.CompletedProcess([], 0, text, '')


class VerificationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(CLIENT, 'RESULTS', self.out).start()
        self.connection = MagicMock()
        patch.object(CLIENT, 'master_client', return_value=self.connection).start()
        patch('sys.stdout', new=io.StringIO()).start()
    def verify(self, rows, value=None):
        records = [{'event':'start','run_id':'r'}] + [dict(event='request', **r) for r in rows] + [{'event':'finish','run_id':'r'}]
        (self.out/'r.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
        self.connection.__enter__.return_value.get.return_value = value
        return CLIENT.verify('r')
    def report(self): return json.loads((self.out/'r-verification.json').read_text())
    def test_all_not_sent_is_not_a_successful_verification(self):
        with self.assertRaisesRegex(RuntimeError, 'No write was acknowledged'):
            self.verify([{'key':'k','value':'v','write_ack':False,'outcome':'not-sent'}])
        self.assertFalse(self.report()['passed']); self.assertEqual(self.report()['acknowledged_requests'],0)
    def test_all_rejected_is_not_a_successful_verification(self):
        with self.assertRaises(RuntimeError):
            self.verify([{'key':'k','value':'v','write_ack':False,'outcome':'rejected'}])
        self.assertFalse(self.report()['passed'])
    def test_empty_finished_log_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError,'No requests'): self.verify([])
        self.assertEqual(self.report()['checked_requests'],0)
    def test_uncertain_only_does_not_prove_service_success(self):
        with self.assertRaises(RuntimeError):
            self.verify([{'key':'k','value':'v','write_ack':False,'outcome':'uncertain'}], 'v')
        self.assertFalse(self.report()['passed'])
    def test_acknowledged_present_is_success(self):
        self.verify([{'key':'k','value':'v','write_ack':True,'outcome':'acknowledged'}], 'v')
        self.assertTrue(self.report()['passed'])
    def test_missing_acknowledged_data_fails(self):
        with self.assertRaises(RuntimeError):
            self.verify([{'key':'k','value':'v','write_ack':True,'outcome':'acknowledged'}])
        self.assertFalse(self.report()['passed'])
    def test_rejected_existing_different_value_is_not_absent(self):
        self.assertEqual(core.verification_bucket({'value':'v','write_ack':False,'outcome':'rejected'}, 'changed'),'rejected_but_present')
    def test_not_sent_existing_empty_value_is_not_absent(self):
        self.assertEqual(core.verification_bucket({'value':'v','write_ack':False,'outcome':'not-sent'}, ''),'not_sent_but_present')
    def test_incomplete_log_clears_old_verification_pass(self):
        (self.out/'r.jsonl').write_text('{"event":"start"}\n')
        (self.out/'r-verification.json').write_text('{"passed":true}')
        with self.assertRaises(ValueError): CLIENT.verify('r')
        self.assertFalse(self.report()['passed']); self.assertEqual(self.report()['status'],'FAIL')
    def test_redis_error_clears_old_verification_pass(self):
        (self.out/'r-verification.json').write_text('{"passed":true}')
        self.connection.__enter__.return_value.get.side_effect=RuntimeError('connection lost')
        with self.assertRaisesRegex(RuntimeError,'connection lost'):
            self.verify([{'key':'k','value':'v','write_ack':True,'outcome':'acknowledged'}])
        self.assertFalse(self.report()['passed']); self.assertEqual(self.report()['status'],'FAIL')
    def test_incomplete_log_refused(self):
        (self.out/'r.jsonl').write_text('{"event":"start"}\n')
        with self.assertRaisesRegex(ValueError,'not finished'): CLIENT.verify('r')


class WorkloadControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(CLIENT,'RESULTS',self.out).start()
        patch.object(CLIENT.signal,'signal').start()
        patch.object(CLIENT.time,'sleep').start()
        patch('sys.stdout',new=io.StringIO()).start()
        self.args=Namespace(seconds=10,rate=2,size=64,mode='sentinel',fixed_node='redis-1',wait_replicas=0,run_id='controlled')
    def test_unsafe_explicit_run_id_rejected(self):
        self.args.run_id='../outside'
        with self.assertRaises(ValueError): CLIENT.workload(self.args)
        self.assertFalse((self.out/'latest-run.txt').exists())
    def test_existing_run_refused(self):
        (self.out/'controlled.jsonl').write_text('preserve')
        with self.assertRaises(ValueError): CLIENT.workload(self.args)
        self.assertEqual((self.out/'controlled.jsonl').read_text(),'preserve')
    def test_stop_file_finishes_journal_cleanly(self):
        connection=MagicMock(); values={}
        connection.info.return_value={'run_id':'r','tcp_port':6379}
        connection.config_get.return_value={'replica-announce-ip':'10.89.77.11'}
        def write(key,value):
            values[key]=value
            (self.out/'controlled.stop').touch()
            return True
        connection.set.side_effect=write
        connection.get.side_effect=lambda key: values.get(key)
        manager=MagicMock()
        manager.master_for.return_value.client.return_value.__enter__.return_value=connection
        with patch.object(CLIENT,'sentinel',return_value=manager): CLIENT.workload(self.args)
        records=[json.loads(x) for x in (self.out/'controlled.jsonl').read_text().splitlines()]
        self.assertEqual(records[-1]['event'],'finish')
        self.assertEqual(records[-1]['counts'],{'acknowledged':1})
        self.assertFalse((self.out/'controlled.stop').exists())
    def test_parser_passes_explicit_run_id(self):
        args=manage.parser().parse_args(['workload','--run-id','my-run'])
        self.assertEqual(args.run_id,'my-run')


class FailoverReportRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'output').mkdir()
        self.addCleanup(patch.stopall)
        patch.object(manage,'ROOT',self.root).start()
        patch.object(manage.secrets,'token_hex',return_value='VALUE').start()
        patch('sys.stdout',new=io.StringIO()).start()
        self.lab=manage.Lab(manage.parse_env(ROOT/'.env.example'))
        self.lab.state=Mock(return_value={}); self.lab.client=Mock()
        self.lab.master=Mock(side_effect=['redis-1','redis-2','redis-2'])
        self.lab.execute=Mock(return_value=result('OK\n2\n')); self.lab.cli=Mock(return_value=result('VALUE\n'))
        self.lab.fault=Mock(); self.lab.recover=Mock()
    def report(self): return json.loads((self.root/'output/failover-test.json').read_text())
    def test_readiness_failure_replaces_stale_pass_report(self):
        (self.root/'output/failover-test.json').write_text('{"passed":true}')
        self.lab.client.side_effect=RuntimeError('readiness failed')
        with self.assertRaisesRegex(RuntimeError,'readiness failed'): self.lab.test_failover()
        self.assertFalse(self.report()['passed']); self.assertEqual(self.report()['status'],'FAIL')
        self.lab.fault.assert_not_called(); self.lab.recover.assert_not_called()
    def test_baseline_failure_does_not_inject_fault(self):
        self.lab.execute.return_value=result('OK\n1\n')
        with self.assertRaisesRegex(RuntimeError,'Baseline'): self.lab.test_failover()
        self.lab.fault.assert_not_called(); self.lab.recover.assert_not_called()
        self.assertFalse(self.report()['passed'])
    def test_fault_and_recovery_errors_both_preserved(self):
        self.lab.fault.side_effect=RuntimeError('primary failure')
        self.lab.recover.side_effect=RuntimeError('cleanup failure')
        with self.assertRaisesRegex(RuntimeError,'primary failure.*cleanup failure'): self.lab.test_failover()
        self.assertEqual(self.report()['error'],'primary failure')
        self.assertEqual(self.report()['recovery_error'],'cleanup failure')
    def test_cleanup_failure_never_reports_pass(self):
        self.lab.recover.side_effect=RuntimeError('cleanup failure')
        with self.assertRaises(RuntimeError): self.lab.test_failover()
        self.assertFalse(self.report()['passed']); self.assertFalse(self.report()['recovery_passed'])
    def test_success_requires_recovery(self):
        self.lab.test_failover()
        self.assertTrue(self.report()['passed']); self.assertTrue(self.report()['recovery_passed'])
        self.assertEqual(self.report()['after'],'redis-2')
        self.lab.recover.assert_called_once_with('redis-1',wait=True)
    def test_master_change_during_recovery_fails(self):
        self.lab.master.side_effect=['redis-1','redis-2','redis-3']
        with self.assertRaisesRegex(RuntimeError,'Master changed'): self.lab.test_failover()
        self.assertFalse(self.report()['passed'])
    def test_timeout_budget_respects_configuration(self):
        self.lab.env.update(DOWN_AFTER_MS='120000',FAILOVER_TIMEOUT_MS='600000')
        self.assertGreaterEqual(self.lab.failover_budget(),1320)
        self.assertGreaterEqual(self.lab.readiness_budget(),self.lab.failover_budget())
    def test_interruption_is_reported_as_failure(self):
        self.lab.fault.side_effect=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.lab.test_failover()
        self.assertEqual(self.report()['error'],'KeyboardInterrupt')
        self.assertFalse(self.report()['passed']); self.lab.recover.assert_called_once()


class RestoreAndPersistenceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.source=Path(self.temp.name)/'input.rdb'; self.source.write_bytes(b'REDIS-not-a-valid-rdb')
        self.lab=manage.Lab(manage.parse_env(ROOT/'.env.example'))
        self.lab.sandbox_up=Mock(); self.lab.volume_owned=Mock(); self.lab.inspect=Mock()
        self.lab.run=Mock(return_value=result()); self.lab.client=Mock()
        self.lab.execute=Mock(return_value=result())
        self.lab.cli=Mock(return_value=result('PONG\nrdb_bgsave_in_progress:0\nrdb_last_bgsave_status:ok\naof_rewrite_in_progress:0\naof_rewrite_scheduled:0\naof_last_bgrewrite_status:ok\n'))
    def test_rdb_validation_failure_precedes_stop_or_erase(self):
        self.lab.execute.side_effect=[RuntimeError('RDB validation failed'),result()]
        with self.assertRaisesRegex(RuntimeError,'RDB validation failed'): self.lab.restore(str(self.source),True)
        self.assertEqual(self.lab.execute.call_args_list[0].args[1][0],'redis-check-rdb')
        self.assertFalse(any('stop' in c.args[0] for c in self.lab.run.call_args_list))
        self.assertFalse(any('run' in c.args[0] for c in self.lab.run.call_args_list))
    def test_missing_metadata_fields_refused_before_container_changes(self):
        import hashlib
        self.source.with_suffix('.json').write_text(json.dumps({'sha256':hashlib.sha256(self.source.read_bytes()).hexdigest()}))
        with self.assertRaisesRegex(ValueError,'marker_key'): self.lab.restore(str(self.source),True)
        self.lab.run.assert_not_called(); self.lab.sandbox_up.assert_not_called()
    def test_non_object_metadata_refused(self):
        self.source.with_suffix('.json').write_text('["bad"]')
        with self.assertRaisesRegex(ValueError,'JSON object'): self.lab.restore(str(self.source),True)
        self.lab.run.assert_not_called()
    def test_persistence_recovery_marker_kept_when_write_test_fails(self):
        self.lab.cli.side_effect=[result('rdb_bgsave_in_progress:0\nrdb_last_bgsave_status:ok\n'),RuntimeError('write still fails')]
        with self.assertRaisesRegex(RuntimeError,'write still fails'): self.lab.sandbox_recover()
        self.assertEqual(self.lab.execute.call_count,1)
        self.assertNotIn('rm /data/state/persistence-fault-active',self.lab.execute.call_args.args[1][-1])
    def test_persistence_marker_removed_only_after_successful_write(self):
        with redirect_stdout(io.StringIO()): self.lab.sandbox_recover()
        self.assertEqual(self.lab.execute.call_args.args[1],['rm','-f','/data/state/persistence-fault-active'])
    def test_fault_marker_written_before_first_filesystem_mutation(self):
        self.lab.cli.side_effect=[result('rdb_bgsave_in_progress:0\nrdb_last_bgsave_status:err\n'),result('MISCONF')]
        with redirect_stdout(io.StringIO()): self.lab.sandbox_persistence_fault()
        script=self.lab.execute.call_args.args[1][-1]
        self.assertLess(script.index('touch /data/state/persistence-fault-active'),script.index('mv /data/db/dump.rdb'))


class EntrypointInterruptedInitializationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_entrypoint.EntrypointTests('test_primary_has_no_replicaof')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root
    def test_runtime_config_without_fingerprint_refused(self):
        self.assertEqual(self.fixture.run_entry().returncode,0)
        (self.root/'data/state/env.sha256').unlink()
        original=(self.root/'data/state/redis.conf').read_text()
        outcome=self.fixture.run_entry()
        self.assertEqual(outcome.returncode,78)
        self.assertEqual((self.root/'data/state/redis.conf').read_text(),original)
    def test_invalid_initial_role_refused(self):
        self.fixture.env['INITIAL_ROLE']='typo'
        self.assertEqual(self.fixture.run_entry().returncode,64)
    def test_interruption_before_config_rename_can_be_retried(self):
        binpath=self.root/'bin'; binpath.mkdir()
        move=binpath/'mv'
        move.write_text('#!/bin/sh\ncase "$1" in */redis.conf.tmp) exit 99;; esac\nexec /usr/bin/mv "$@"\n')
        move.chmod(0o755)
        original_path=self.fixture.env['PATH']; self.fixture.env['PATH']=str(binpath)+':'+original_path
        self.assertEqual(self.fixture.run_entry().returncode,99)
        self.assertTrue((self.root/'data/state/env.sha256').exists())
        self.assertFalse((self.root/'data/state/redis.conf').exists())
        self.fixture.env['PATH']=original_path
        self.assertEqual(self.fixture.run_entry().returncode,0)
        self.assertIn('replicaof',(self.root/'data/state/redis.conf').read_text())


class LiveSuiteReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(manage,'ROOT',self.root).start()
        self.lab=manage.Lab(manage.parse_env(ROOT/'.env.example'))
        patch('sys.stdout',new=io.StringIO()).start(); patch('sys.stderr',new=io.StringIO()).start()
    def report(self): return json.loads((self.root/'output/live-validation.json').read_text())
    def test_destructive_suite_requires_confirmation(self):
        with self.assertRaises(ValueError): live_validate.validate(self.lab)
        self.assertFalse((self.root/'output').exists())
    def test_no_runtime_is_blocked_not_pass(self):
        with patch.object(live_validate.shutil,'which',return_value=None):
            self.assertEqual(live_validate.validate(self.lab,confirmed=True),2)
        self.assertEqual(self.report()['status'],'BLOCKED'); self.assertFalse(self.report()['passed'])
    def test_stale_success_replaced_when_blocked(self):
        (self.root/'output').mkdir(); (self.root/'output/live-validation.json').write_text('{"passed":true}')
        with patch.object(live_validate.shutil,'which',return_value=None): live_validate.validate(self.lab,confirmed=True)
        self.assertFalse(self.report()['passed'])
    def test_preflight_error_is_blocked(self):
        suite=live_validate.Suite(self.lab); suite.command=Mock(side_effect=RuntimeError('engine unavailable'))
        with patch.object(live_validate.shutil,'which',return_value='/bin/dummy'):
            self.assertEqual(suite.execute(),2)
        self.assertEqual(self.report()['steps'][0]['status'],'BLOCKED')
    def test_build_failure_is_fail_not_pass(self):
        suite=live_validate.Suite(self.lab)
        def command(*args,**kw):
            if args[0]=='up': raise RuntimeError('build failed')
            return ''
        suite.command=Mock(side_effect=command)
        with patch.object(live_validate.shutil,'which',return_value='/bin/dummy'):
            self.assertEqual(suite.execute(),1)
        self.assertEqual(self.report()['status'],'FAIL')
        self.assertFalse(self.report()['passed'])
    def test_workload_log_parser_ignores_partial_json(self):
        path=self.root/'workload.log';path.write_text('RUN_ID=x\n{"write_ack":true,"node_ip":"x"}\n{"write_')
        self.assertEqual(len(live_validate.Suite.workload_rows(path)),1)

if __name__=='__main__': unittest.main(verbosity=2)
