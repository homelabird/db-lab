"""Acceptance controller regression tests. No Docker/DB is invoked by these tests."""
from contextlib import nullcontext, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
from tools import acceptance as a, manage
from test_runtime_contract import source


def response(value, code=0, timed_out=False):
    return {'returncode':code,'stdout':json.dumps(value),'stderr_bytes':0,'stderr_policy':'redacted',
            'timed_out':timed_out,'interrupted':False,'limit_reason':None,'elapsed_seconds':0.1}


def native(service='api'):
    checks=[{'name':n,'status':'passed'} for n in ('image_source','driver_imports_and_pins','effective_target','mariadb','redis','elasticsearch','kafka')]
    return response({'schema':1,'service':service,'status':'passed','data_writes':False,
                     'evidence_kind':'LIVE-DRIVER-READ-ONLY-PROBE','checks':checks})

class PlanTests(unittest.TestCase):
    def test_core_is_smoke_and_one_baseline(self):
        steps=a.steps_for('core');self.assertEqual([s.name for s in steps],['smoke','simulate-baseline'])

    def test_all_is_29_unique_scenarios_not_30(self):
        value=a.plan('all');self.assertEqual(value['selected_scenarios'],29)
        names=[s['name'] for s in value['steps']];self.assertEqual(len(names),len(set(names)))
        self.assertNotIn('drills-upgrade-restore',names);self.assertTrue(value['excluded'])

    def test_transaction_suite_includes_six_and_baseline(self):
        self.assertEqual(a.plan('transactions')['selected_scenarios'],7)

    def test_candidate_requires_explicit_compatible_reference(self):
        with self.assertRaises(ValueError):a.steps_for('candidate')
        with self.assertRaises(ValueError):a.steps_for('candidate','mariadb:latest')
        steps=a.steps_for('candidate','mariadb:11.4.5');self.assertIn('--candidate-image',steps[-1].command)

    def test_candidate_not_silently_ignored_by_another_suite(self):
        with self.assertRaises(ValueError):a.steps_for('core','mariadb:11.4.5')

    def test_plan_does_not_use_engine_or_read_configuration(self):
        args=manage.parser().parse_args(['verify','plan','all']);m=Mock()
        with redirect_stdout(io.StringIO()) as output:self.assertEqual(a.cli(args,m),0)
        self.assertIs(json.loads(output.getvalue())['runtime_executed'],False);m.Compose.assert_not_called()

    def test_run_requires_consent(self):
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):manage.parser().parse_args(['verify','run','core'])

    def test_invalid_budget_refused_before_creating_report(self):
        args=manage.parser().parse_args(['verify','run','core','--yes','--step-timeout','10'])
        with self.assertRaises(ValueError),patch.object(a,'Gate') as gate:a.cli(args,Mock())
        gate.assert_not_called()

    def test_report_id_traversal_rejected(self):
        args=manage.parser().parse_args(['verify','report','../../secret'])
        with self.assertRaises(ValueError):a.cli(args,Mock())

class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);source(self.root)
        self.m=types.SimpleNamespace(ROOT=self.root,atomic_json=manage.atomic_json,lock=nullcontext,require_no_active_fault=Mock())
        self.gate=a.Gate(self.m,'core')
        self.gate.preflight=Mock(return_value=[])
        self.gate.c=types.SimpleNamespace(inherited=dict(os.environ))
        self.gate.native=Mock(side_effect=lambda service,states:native(service))
        self.gate.check_unchanged=Mock(return_value=[])

    def good_child(self, argv, **_):
        if argv[-1]=='smoke':
            return response({'passed':True,'submitted_payload_checked':True,'full_retry_payload_checked':True})
        directory=self.root/'reports/simulations/sim-0123456789ab';directory.mkdir(parents=True,exist_ok=True)
        value={'run_id':'sim-0123456789ab','scenario':'baseline','status':'passed','fault_restored':True,'evidence_kind':'real-http-and-docker-actions'}
        manage.atomic_json(directory/'summary.json',value)
        return response({**value,'report':str(directory/'report.md')})

    def run_gate(self, side_effect=None):
        with patch.object(a,'execute',side_effect=side_effect or self.good_child),redirect_stdout(io.StringIO()):return self.gate.run()

    def test_success_requires_both_image_probes_and_live_child_evidence(self):
        self.assertEqual(self.run_gate(),0)
        self.assertEqual(self.gate.summary['passed_scenarios'],1)
        self.assertEqual(self.gate.native.call_count,2)
        xml=ET.parse(self.gate.directory/'junit.xml').getroot();self.assertEqual(xml.attrib['failures'],'0')

    def test_missing_docker_records_blocked_zero_tests_not_pass(self):
        self.gate.preflight.side_effect=FileNotFoundError()
        with patch.object(a,'execute') as run,redirect_stdout(io.StringIO()):code=self.gate.run()
        self.assertEqual(code,127);run.assert_not_called();self.gate.native.assert_not_called()
        self.assertEqual(self.gate.summary['status'],'blocked');self.assertEqual(self.gate.summary['passed_scenarios'],0)
        xml=ET.parse(self.gate.directory/'junit.xml').getroot();self.assertEqual(xml.attrib['errors'],'1');self.assertEqual(xml.attrib['skipped'],'4')

    def test_image_failure_cannot_be_skipped(self):
        self.gate.native.return_value=None;self.gate.native.side_effect=lambda *args:response({'status':'failed'},1)
        with patch.object(a,'execute') as run,redirect_stdout(io.StringIO()):self.assertEqual(self.gate.run(),1)
        run.assert_not_called();self.assertEqual(self.gate.summary['steps'][3]['status'],'not_run')

    def test_exit_zero_without_complete_native_checks_is_blocked(self):
        self.gate.native.side_effect=lambda *args:response({'status':'passed','checks':[]})
        with patch.object(a,'execute') as run,redirect_stdout(io.StringIO()):self.assertEqual(self.gate.run(),1)
        run.assert_not_called();self.assertEqual(self.gate.summary['status'],'blocked')

    def test_worker_is_not_assumed_equal_to_api(self):
        self.gate.native.side_effect=[native('api'),native('api')]
        with patch.object(a,'execute') as run,redirect_stdout(io.StringIO()):self.gate.run()
        run.assert_not_called();self.assertNotEqual(self.gate.summary['status'],'passed')

    def test_weak_old_smoke_cannot_pass(self):
        self.assertEqual(self.run_gate(lambda *a,**k:response({'passed':True})),1)
        self.assertEqual(self.gate.summary['steps'][4]['status'],'not_run')

    def test_inconclusive_stops_and_remains_inconclusive(self):
        self.assertEqual(self.run_gate(lambda *a,**k:response({'status':'inconclusive'},2)),2)
        self.assertEqual(self.gate.summary['status'],'inconclusive');self.assertEqual(self.gate.summary['passed_scenarios'],0)

    def test_timeout_does_not_pass_even_if_child_exits_zero(self):
        self.assertEqual(self.run_gate(lambda *a,**k:response({'passed':True},0,True)),1)
        self.assertEqual(self.gate.summary['steps'][3]['status'],'failed')

    def test_interruption_report_is_preserved(self):
        def interrupt(*_a,**_k):raise KeyboardInterrupt()
        self.assertEqual(self.run_gate(interrupt),130)
        self.assertTrue((self.gate.directory/'summary.json').is_file())

    def test_active_recovery_marker_blocks_a_run_without_deleting_it(self):
        (self.root/'.state').mkdir();marker=self.root/'.state/drill-active.json';marker.write_text('{}')
        self.gate.preflight.side_effect=RuntimeError('Pending drill')
        self.assertEqual(self.run_gate(),1);self.assertTrue(marker.exists())
        self.assertEqual(self.gate.summary['recovery_markers'],['drill-active.json'])

    def test_previous_report_cannot_be_reused(self):
        old=self.good_child(['simulate'])
        def child(argv,**kwargs):return self.good_child(argv,**kwargs) if argv[-1]=='smoke' else old
        self.assertEqual(self.run_gate(child),1);self.assertIn('previous report',self.gate.summary['steps'][-1]['reason'])

    def test_source_or_image_change_stops_before_child_mutation(self):
        self.gate.check_unchanged.side_effect=RuntimeError('changed')
        with patch.object(a,'execute') as run,redirect_stdout(io.StringIO()):self.assertEqual(self.gate.run(),1)
        run.assert_not_called()

    def test_raw_stderr_not_put_into_step_result(self):
        result=response({'passed':False},1);result['stderr_bytes']=100
        self.run_gate(lambda *a,**k:result)
        value=json.loads((self.gate.directory/'smoke.json').read_text());self.assertNotIn('stderr',value)

    def test_gate_reports_private_permissions(self):
        self.run_gate();self.assertEqual((self.gate.directory/'summary.json').stat().st_mode&0o777,0o600)
        self.assertEqual((self.gate.directory/'junit.xml').stat().st_mode&0o777,0o600)

    def test_suite_lock_prevents_another_suite(self):
        with a.suite_lock(self.root),self.assertRaises(RuntimeError):
            with a.suite_lock(self.root):pass

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def summary(self,family,extra=None):
        run='run-test';p=self.root/'reports'/family/run;p.mkdir(parents=True,exist_ok=True)
        kind={'simulations':'real-http-and-docker-actions','drills':'LIVE-LOCAL-DOCKER-DRILL','messages':'LIVE-LOCAL-DOCKER-ISOLATED-MESSAGE-DRILL'}[family]
        value={'run_id':run,'kind':'one','status':'passed','evidence_kind':kind,'cleanup':True,**(extra or {})}
        manage.atomic_json(p/'summary.json',value)
        return value,{'run_id':run,'report_directory':str(p),'report':str(p/'report.md')}

    def test_nonlive_evidence_is_not_runtime_pass(self):
        _,value=self.summary('drills',{'evidence_kind':'TEST-DOUBLE'})
        with self.assertRaisesRegex(RuntimeError,'Non-live'):a.validate_child(self.root,a.Step('one',(),'drills','one'),value)

    def test_wrong_scenario_is_not_runtime_pass(self):
        _,value=self.summary('drills')
        with self.assertRaises(RuntimeError):a.validate_child(self.root,a.Step('one',(),'drills','two'),value)

    def test_missing_cleanup_not_runtime_pass(self):
        _,value=self.summary('drills',{'cleanup':False})
        with self.assertRaises(RuntimeError):a.validate_child(self.root,a.Step('one',(),'drills','one'),value)

    def test_outside_report_path_refused(self):
        with self.assertRaises(RuntimeError):a.result_path(self.root,{'report_directory':'/tmp/outside'},'messages')

    def test_restore_false_is_not_runtime_pass(self):
        _,value=self.summary('simulations',{'fault_restored':False})
        with self.assertRaises(RuntimeError):a.validate_child(self.root,a.Step('one',(),'simulate','one'),value)

    def test_message_cleanup_dict_is_checked(self):
        _,value=self.summary('messages',{'cleanup':{'cleaned':False}})
        with self.assertRaises(RuntimeError):a.validate_child(self.root,a.Step('one',(),'messages','one'),value)

class ProcessTests(unittest.TestCase):
    def test_real_subprocess_json_and_private_stderr(self):
        result=a.execute([sys.executable,'-S','-c','import sys;print("{} ");print("private-credential",file=sys.stderr)'],cwd=Path.cwd(),env=dict(os.environ),timeout=10)
        self.assertEqual(result['returncode'],0);self.assertGreater(result['stderr_bytes'],0)
        self.assertNotIn('private-credential',json.dumps(result))

    def test_actual_timeout_remains_failed_after_cleanup_exit(self):
        code='import time,signal,sys; signal.signal(signal.SIGTERM,lambda *args:sys.exit(0)); print("{}",flush=True); time.sleep(10)'
        result=a.execute([sys.executable,'-S','-c',code],cwd=Path.cwd(),env=dict(os.environ),timeout=0.3)
        self.assertTrue(result['timed_out']);self.assertEqual(result['returncode'],0)

class TargetContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);source(self.root)
        self.config={'SQL_PASSWORD':'synthetic-sql','REDIS_PASSWORD':'synthetic-redis','CACHE_TTL':'30','MVP_PROJECT':'db-lab-mvp'}
        self.c=Mock();self.lab=Mock();self.lab.docker.return_value='{"OSType":"linux"}'
        self.lab.binding.return_value={'identity':'test-only'}
        self.m=types.SimpleNamespace(ROOT=self.root,atomic_json=manage.atomic_json,require_no_active_fault=Mock(),
             parse_env=Mock(return_value=self.config),validate=lambda c:c,Compose=Mock(return_value=self.c))
        mounts={'mariadb':'/var/lib/mysql','kafka':'/var/lib/kafka/data','elasticsearch':'/usr/share/elasticsearch/data','redis':'/data'}
        self.states=[{'service':name,'image_id':'image-'+('app' if name in ('api','worker') else name),
             'published_ports':{},'volumes':{mounts[name]:'test-'+name} if name in mounts else {}}
             for name in ('mariadb','kafka','elasticsearch','redis','api','worker')]
        self.states[-2]['published_ports']={'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'18090'}]}
        self.lab.preflight.return_value=self.states;self.gate=a.Gate(self.m,'core')
        self.patch=patch.object(a,'DockerLab',return_value=self.lab);self.patch.start();self.addCleanup(self.patch.stop)
        p=patch.object(a.shutil,'which',return_value='/test/docker');p.start();self.addCleanup(p.stop)

    def test_expected_named_volumes_and_same_app_image_accepted(self):
        self.assertEqual(self.gate.preflight(),self.states)
        self.assertTrue((self.gate.directory/'containers-before.json').exists())
        self.c.run.assert_not_called()

    def test_linux_backend_required(self):
        self.lab.docker.return_value='{"OSType":"windows"}'
        with self.assertRaisesRegex(RuntimeError,'Linux'):self.gate.preflight()

    def test_additional_database_host_port_refused(self):
        self.states[0]['published_ports']={'3306/tcp':[{'HostIp':'127.0.0.1','HostPort':'3306'}]}
        with self.assertRaisesRegex(RuntimeError,'Only the API'):self.gate.preflight()

    def test_missing_persistent_data_mount_refused(self):
        self.states[0]['volumes']={}
        with self.assertRaisesRegex(RuntimeError,'named volume'):self.gate.preflight()

    def test_different_worker_build_refused(self):
        self.states[-1]['image_id']='older-worker-image'
        with self.assertRaisesRegex(RuntimeError,'different application'):self.gate.preflight()

    def test_changed_volume_between_steps_is_not_ignored(self):
        self.gate.preflight()
        # preflight retains the initial dictionaries; simulate a new independent Docker inspect snapshot.
        import copy
        changed=copy.deepcopy(self.states);changed[0]['volumes']['/var/lib/mysql']='replacement-volume'
        self.lab.preflight.return_value=changed
        with self.assertRaisesRegex(RuntimeError,'volume identity'):self.gate.check_unchanged()

    def test_unchanged_targets_allow_recreated_container_ids(self):
        self.gate.preflight()
        self.states[-2]['id']='new-api-id'
        self.assertEqual(self.gate.check_unchanged(),self.states)
