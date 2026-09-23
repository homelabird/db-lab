"""Container contracts and startup timing with explicit doubles, not Docker health tests."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from tools import startup, manage

PROJECT = 'db-lab-mvp-test'

def container(service='mariadb'):
    return {'Id': 'a'*64, 'Image': 'sha256:'+'b'*64, 'RestartCount': 0,
            'Config': {'Labels': {'com.docker.compose.project': PROJECT, 'com.docker.compose.service': service},
                       'Env': ['PASSWORD=PRIVATE-SENTINEL']},
            'State': {'Status': 'running', 'Running': True, 'Paused': False, 'OOMKilled': False, 'ExitCode': 0,
                      'Health': {'Status': 'healthy', 'Log': [{'Output':'PRIVATE-SENTINEL'}]}}}

class ContainerTests(unittest.TestCase):
    def test_health_output_env_and_message_omitted(self):
        row = startup.sanitize_container(container(), PROJECT, 'mariadb')
        self.assertTrue(row['ready']); self.assertNotIn('PRIVATE', json.dumps(row))
    def test_wrong_owner_not_inspected_as_ours(self):
        with self.assertRaises(RuntimeError): startup.sanitize_container(container(), 'other', 'mariadb')
    def test_wrong_service_refused(self):
        with self.assertRaises(RuntimeError): startup.sanitize_container(container(), PROJECT, 'redis')
    def test_malformed_identity_refused(self):
        row=container(); row['Id']='not-a-container'
        with self.assertRaises(RuntimeError): startup.sanitize_container(row,PROJECT,'mariadb')
    def test_running_but_starting_is_not_ready(self):
        row=container(); row['State']['Health']['Status']='starting'
        self.assertFalse(startup.sanitize_container(row, PROJECT, 'mariadb')['ready'])
    def test_running_but_unhealthy_is_not_ready(self):
        row=container(); row['State']['Health']['Status']='unhealthy'
        self.assertFalse(startup.sanitize_container(row, PROJECT, 'mariadb')['ready'])
    def test_paused_is_not_ready(self):
        row=container(); row['State']['Paused']=True
        self.assertFalse(startup.sanitize_container(row, PROJECT, 'mariadb')['ready'])
    def test_restarting_is_not_ready(self):
        row=container(); row['State']['Restarting']=True
        self.assertFalse(startup.sanitize_container(row, PROJECT, 'mariadb')['ready'])
    def test_terminated_oom_is_distinct(self):
        row=container(); row['State'].update(Status='exited',Running=False,OOMKilled=True,ExitCode=137)
        result=startup.sanitize_container(row,PROJECT,'mariadb')
        self.assertFalse(result['ready']);self.assertEqual(result['exit_code'],137);self.assertTrue(result['oom_killed'])
    def test_missing_db_health_is_not_silently_ready(self):
        row=container();row['State'].pop('Health')
        self.assertFalse(startup.sanitize_container(row,PROJECT,'mariadb')['ready'])
    def test_worker_without_healthcheck_is_not_ready(self):
        row=container('worker');row['State'].pop('Health')
        result=startup.sanitize_container(row,PROJECT,'worker')
        self.assertFalse(result['ready']);self.assertEqual(result['health'],'not_configured')
    def test_unknown_state_and_fields_do_not_export_arbitrary_text(self):
        row=container();row['State']['Status']='PRIVATE-SENTINEL';row['RestartCount']='PRIVATE-SENTINEL'
        result=startup.sanitize_container(row,PROJECT,'mariadb')
        self.assertFalse(result['ready']);self.assertNotIn('PRIVATE',json.dumps(result))
    def test_log_classification_never_returns_original_line(self):
        result=startup.log_signals('PASSWORD=PRIVATE-SENTINEL Access denied; No space left on device')
        self.assertEqual(result,['authentication_rejected','storage_full']);self.assertNotIn('PRIVATE',str(result))
    def test_unknown_log_is_not_published(self):
        self.assertEqual(startup.log_signals('secret: PRIVATE-SENTINEL'),[])

class CollectTests(unittest.TestCase):
    def compose(self):
        c=Mock();c.config={'MVP_PROJECT':PROJECT};c.engine=['docker'];c.inherited={}
        c.guard_engine.return_value={'local_docker':True};c.run.return_value=types.SimpleNamespace(stdout='a'*64)
        return c
    def test_missing_containers_reported_without_inspecting_any_id(self):
        c=self.compose();c.run.return_value.stdout=''
        with patch.object(startup.subprocess,'run') as process: result=startup.collect(c)
        process.assert_not_called();self.assertFalse(result['ready'])
        self.assertTrue(all(r['state']=='missing' for r in result['containers']))
    def test_multiple_containers_are_not_arbitrarily_selected(self):
        c=self.compose();c.run.return_value.stdout='a'*64+'\n'+'b'*64
        result=startup.collect(c);self.assertTrue(all(r['state']=='ambiguous' for r in result['containers']))
    def test_timeout_is_unknown_not_unhealthy(self):
        c=self.compose();c.run.side_effect=subprocess.TimeoutExpired(['fake'],1)
        result=startup.collect(c);self.assertTrue(all(r['state']=='observation_timeout' for r in result['containers']))
    def test_wrong_engine_refused_before_container_queries(self):
        c=self.compose();c.engine=['podman']
        with self.assertRaises(RuntimeError): startup.collect(c)
        c.run.assert_not_called()
    def test_pin_failure_prevents_queries(self):
        c=self.compose();c.guard_engine.side_effect=RuntimeError('changed')
        with self.assertRaises(RuntimeError): startup.collect(c)
        c.run.assert_not_called()
    def test_bad_budget_refused(self):
        for budget in (0,61,float('nan')):
            with self.subTest(budget=budget),self.assertRaises(ValueError): startup.collect(self.compose(),budget=budget)
    def test_stderr_log_signal_is_kept_without_raw_output(self):
        c=self.compose()
        def run(argv,**kwargs):
            if argv[1]=='inspect':
                row=container();row['State']['Health']['Status']='unhealthy'
                return types.SimpleNamespace(stdout=json.dumps([row]),stderr='')
            return types.SimpleNamespace(stdout='',stderr='private-password: Permission denied')
        with patch.object(startup,'BASE_SERVICES',('mariadb',)),patch.object(startup.subprocess,'run',side_effect=run):
            result=startup.collect(c)
        self.assertEqual(result['containers'][0]['signals'],['permission_denied'])
        self.assertNotIn('private-password',json.dumps(result))

    def test_requested_healthy_service_logs_are_classified(self):
        c=self.compose()
        def run(argv,**kwargs):
            if argv[1]=='inspect': return types.SimpleNamespace(stdout=json.dumps([container('api')]),stderr='')
            return types.SimpleNamespace(stdout='private-token: Access denied',stderr='')
        with patch.object(startup,'BASE_SERVICES',('api',)),patch.object(startup.subprocess,'run',side_effect=run):
            result=startup.collect(c,include_logs=True,log_services=('api',))
        self.assertTrue(result['containers'][0]['ready'])
        self.assertEqual(result['containers'][0]['signals'],['authentication_rejected'])
        self.assertNotIn('private-token',json.dumps(result))

class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        p=patch.object(manage,'ROOT',self.root);p.start();self.addCleanup(p.stop)
        self.clock=0
        def now(): self.clock+=.1;return self.clock
        p=patch.object(manage.time,'monotonic',side_effect=now);p.start();self.addCleanup(p.stop)
        p=patch.object(manage.time,'sleep');p.start();self.addCleanup(p.stop)
        self.compose=Mock();self.compose.engine=['docker']
        self.compose.guard_engine.return_value={'local_docker':True}
        self.compose.run.return_value=types.SimpleNamespace(returncode=0,stdout='PRIVATE-SENTINEL')
    def evidence(self): return json.loads(next((self.root/'reports/startup').glob('*.json')).read_text())
    def test_admin_success_does_not_bypass_health(self):
        with patch.object(startup,'collect',return_value={'ready':False,'containers':[]}),self.assertRaisesRegex(RuntimeError,'deadline'):
            manage.Compose.wait_initialized(self.compose,seconds=2)
        self.assertEqual(self.evidence()['status'],'failed');self.assertTrue(self.evidence()['admin_initialized'])
        self.assertEqual(self.compose.run.call_count,1)
    def test_delayed_readiness_is_waited_without_rerunning_ddl(self):
        with patch.object(startup,'collect',side_effect=[{'ready':False,'containers':[]},{'ready':True,'containers':[]}]),redirect_stdout(io.StringIO()):
            manage.Compose.wait_initialized(self.compose,seconds=4)
        self.assertEqual(self.evidence()['status'],'initialized_and_ready');self.assertEqual(self.compose.run.call_count,1)
    def test_admin_timeout_is_bounded_retry_not_fake_success(self):
        self.compose.run.side_effect=[subprocess.TimeoutExpired(['fake'],1),types.SimpleNamespace(returncode=0)]
        with patch.object(startup,'collect',return_value={'ready':True,'containers':[]}),redirect_stdout(io.StringIO()):
            manage.Compose.wait_initialized(self.compose,seconds=4)
        self.assertEqual(self.compose.run.call_count,2);self.assertEqual(self.evidence()['status'],'initialized_and_ready')

    def test_admin_timeout_classifies_partial_output_without_storing_it(self):
        self.compose.engine=['podman']
        self.compose.run.side_effect=subprocess.TimeoutExpired(['fake'],1,
            output='password=PRIVATE-SENTINEL',stderr='Access denied; connection timed out')
        with self.assertRaisesRegex(RuntimeError,'logs api'):
            manage.Compose.wait_initialized(self.compose,seconds=2)
        evidence=self.evidence()
        self.assertEqual(evidence['admin_signals'],['authentication_rejected','connection_timeout'])
        self.assertNotIn('PRIVATE-SENTINEL',json.dumps(evidence))
    def test_raw_stdout_is_not_in_startup_receipt(self):
        with patch.object(startup,'collect',return_value={'ready':True,'containers':[]}),redirect_stdout(io.StringIO()):
            manage.Compose.wait_initialized(self.compose,seconds=4)
        self.assertNotIn('PRIVATE',json.dumps(self.evidence()))
    def test_budget_input_checked(self):
        with self.assertRaises(ValueError): manage.Compose.wait_initialized(self.compose,seconds=-1)

    def test_remote_docker_legacy_init_is_not_claimed_as_local_readiness(self):
        self.compose.guard_engine.return_value={'local_docker':False}
        with patch.object(startup,'collect') as inspect,redirect_stdout(io.StringIO()):
            manage.Compose.wait_initialized(self.compose,seconds=4)
        inspect.assert_not_called();self.assertEqual(self.evidence()['status'],'initialized')
        self.assertIn('not_runtime_certified',self.evidence()['readiness_scope'])
    def test_podman_legacy_init_is_not_claimed_as_local_readiness(self):
        self.compose.engine=['podman']
        with patch.object(startup,'collect') as inspect,redirect_stdout(io.StringIO()):
            manage.Compose.wait_initialized(self.compose,seconds=4)
        inspect.assert_not_called();self.assertEqual(self.evidence()['status'],'initialized')


class FailedInitializationEvidenceTests(unittest.TestCase):
    setUp = InitializationTests.setUp
    evidence = InitializationTests.evidence
    def test_failed_admin_keeps_read_only_container_evidence(self):
        self.compose.run.return_value=types.SimpleNamespace(returncode=1,stdout='PRIVATE-SENTINEL')
        rows=[{'service':'mariadb','state':'exited','ready':False}]
        with patch.object(startup,'collect',return_value={'ready':False,'containers':rows}) as collect,self.assertRaisesRegex(RuntimeError,'deadline'):
            manage.Compose.wait_initialized(self.compose,seconds=2)
        self.assertGreater(collect.call_count,0)
        self.assertTrue(any(call.kwargs.get('log_services')==('api','worker')
                            for call in collect.call_args_list))
        evidence=self.evidence()
        self.assertEqual(evidence['containers'],rows);self.assertFalse(evidence['admin_initialized'])
        self.assertEqual(evidence['last_error'],'admin_initialization_failed')
        self.assertNotIn('PRIVATE',json.dumps(evidence))

    def test_healthy_containers_do_not_hide_failed_initialization(self):
        self.compose.run.return_value=types.SimpleNamespace(returncode=1)
        with patch.object(startup,'collect',return_value={'ready':True,'containers':[]}),self.assertRaisesRegex(RuntimeError,'deadline'):
            manage.Compose.wait_initialized(self.compose,seconds=2)
        self.assertEqual(self.evidence()['status'],'failed')

    def test_admin_failure_reports_only_sanitized_signals(self):
        self.compose.engine=['podman']
        self.compose.run.return_value=types.SimpleNamespace(returncode=1,
            stdout='password=PRIVATE-SENTINEL',stderr='Access denied; connection refused')
        with self.assertRaisesRegex(RuntimeError,'logs api'):
            manage.Compose.wait_initialized(self.compose,seconds=2)
        evidence=self.evidence()
        self.assertEqual(evidence['admin_signals'],['authentication_rejected','connection_refused'])
        self.assertNotIn('PRIVATE-SENTINEL',json.dumps(evidence))
