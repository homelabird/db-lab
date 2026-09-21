"""HTTP safety: real loopback transport; engine/controller calls are explicit doubles."""
from contextlib import nullcontext, redirect_stdout, redirect_stderr
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from tools import http_target, manage

PROJECT = 'db-lab-mvp-test'

class EngineTargetTests(unittest.TestCase):
    def setUp(self):
        self.compose = Mock()
        self.compose.config = {'MVP_PROJECT': PROJECT, 'API_PORT': '18090'}
        self.compose.engine = ['docker']; self.compose.inherited = {}
        self.compose.guard_engine.return_value = {'local_docker': True}
        self.compose.run.return_value = SimpleNamespace(stdout='a'*64)
        self.row = {'Config': {'Labels': {'com.docker.compose.project': PROJECT,
                                         'com.docker.compose.service': 'api'}},
                    'State': {'Running': True},
                    'NetworkSettings': {'Ports': {'8080/tcp': [{'HostIp':'127.0.0.1','HostPort':'18090'}]}}}

    def invoke(self, row=None, info=None):
        def result(argv, **kwargs):
            self.assertTrue(kwargs['check']); self.assertEqual(kwargs['timeout'], 15)
            return SimpleNamespace(stdout=json.dumps(info if argv[1] == 'info' else [self.row if row is None else row]))
        with patch.object(http_target.subprocess, 'run', side_effect=result) as called:
            http_target.guard(self.compose)
        return called

    def test_exact_local_docker_target_accepted_read_only(self):
        calls = self.invoke()
        self.compose.guard_target.assert_called_once()
        self.compose.guard_engine.assert_called_once()
        self.compose.run.assert_called_once_with('ps', '-a', '-q', 'api', capture=True, timeout=15)
        self.assertEqual(calls.call_args.args[0], ['docker','inspect','a'*64])

    def test_pin_failure_prevents_all_container_reads(self):
        self.compose.guard_engine.side_effect = RuntimeError('changed')
        with self.assertRaisesRegex(RuntimeError,'changed'), patch.object(http_target.subprocess,'run') as read:
            http_target.guard(self.compose)
        read.assert_not_called(); self.compose.run.assert_not_called()

    def test_remote_docker_refused_before_http_or_container_queries(self):
        self.compose.guard_engine.return_value = {'local_docker':False}
        with self.assertRaisesRegex(RuntimeError,'local engine'): self.invoke()
        self.compose.run.assert_not_called()

    def test_podman_requires_explicit_local_info(self):
        self.compose.engine=['podman']
        for info in ({}, {'host':None}, {'host':[]}, {'host':{'serviceIsRemote':True}}, {'host':{'serviceIsRemote':0}}):
            with self.subTest(info=info),self.assertRaisesRegex(RuntimeError,'local engine'): self.invoke(info=info)
        self.compose.run.assert_not_called()
        calls=self.invoke(info={'host':{'serviceIsRemote':False}})
        self.assertEqual(calls.call_count,2)

    def test_wrong_owner_or_service_refused(self):
        for key,value in [('com.docker.compose.project','db-lab-mvp-other'),('com.docker.compose.service','worker')]:
            row=copy.deepcopy(self.row);row['Config']['Labels'][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(RuntimeError,'ownership'): self.invoke(row)

    def test_non_running_states_refused(self):
        for state in ({'Running':False},{'Running':True,'Paused':True},{'Running':True,'Restarting':True}):
            row=copy.deepcopy(self.row);row['State']=state
            with self.subTest(state=state),self.assertRaisesRegex(RuntimeError,'not running'):self.invoke(row)

    def test_public_extra_or_wrong_port_bindings_refused(self):
        values=[{'HostIp':'0.0.0.0','HostPort':'18090'}, {'HostIp':'127.0.0.1','HostPort':'18091'}]
        for value in values:
            row=copy.deepcopy(self.row);row['NetworkSettings']['Ports']['8080/tcp']=[value]
            with self.subTest(value=value),self.assertRaisesRegex(RuntimeError,'loopback'):self.invoke(row)
        row=copy.deepcopy(self.row);row['NetworkSettings']['Ports']['9000/tcp']=[values[0]]
        with self.assertRaisesRegex(RuntimeError,'loopback'):self.invoke(row)

    def test_missing_or_multiple_container_never_inspected(self):
        for ids in ('', 'a'*64+'\n'+'b'*64, 'not-an-id'):
            self.compose.run.return_value.stdout=ids
            with self.subTest(ids=ids),patch.object(http_target.subprocess,'run') as read,self.assertRaises(RuntimeError):
                http_target.guard(self.compose)
            read.assert_not_called()

    def test_malformed_inspect_shapes_fail_closed_not_attribute_error(self):
        for key in ('Config','State','NetworkSettings'):
            for value in (None, [], 'bad'):
                row=copy.deepcopy(self.row);row[key]=value
                with self.subTest(key=key,value=value),self.assertRaises(RuntimeError):self.invoke(row)
        row=copy.deepcopy(self.row);row['Config']['Labels']=[]
        with self.assertRaises(RuntimeError):self.invoke(row)
        row=copy.deepcopy(self.row);row['NetworkSettings']['Ports']={'8080/tcp':'bad'}
        with self.assertRaises(RuntimeError):self.invoke(row)

class DiagnosticHTTPTests(unittest.TestCase):
    def server(self, identity_project=PROJECT, healthy=True, malformed=False, redirect=False):
        events=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                events.append(self.path)
                if redirect:
                    self.send_response(302);self.send_header('Location','/unexpected');self.end_headers();return
                value=({'application':'db-lab-mvp','study_api':2,'project':identity_project}
                       if self.path=='/api/study/info' else
                       {'dependencies_reachable':healthy,
                        'dependencies':{name:{'reachable':healthy if name=='kafka' else True}
                                        for name in ('mariadb','redis','elasticsearch','kafka')}})
                if malformed and self.path=='/api/diagnostics':value['dependencies_reachable']=True
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(2)))
        return {'MVP_PROJECT':PROJECT,'API_PORT':str(server.server_port)},events

    def test_healthy_diagnostics_never_posts(self):
        config,events=self.server()
        self.assertTrue(http_target.diagnostics(config)['dependencies_reachable'])
        self.assertEqual(events,['/api/study/info','/api/diagnostics'])

    def test_degraded_dependencies_remain_failure_data(self):
        config,_=self.server(healthy=False)
        self.assertFalse(http_target.diagnostics(config)['dependencies_reachable'])

    def test_wrong_project_prevents_dependency_probe(self):
        config,events=self.server(identity_project='db-lab-mvp-other')
        with self.assertRaisesRegex(RuntimeError,'identity'):http_target.diagnostics(config)
        self.assertEqual(events,['/api/study/info'])

    def test_inconsistent_success_flag_refused(self):
        config,_=self.server(healthy=False,malformed=True)
        with self.assertRaisesRegex(RuntimeError,'inconsistent'):http_target.diagnostics(config)

    def test_redirect_is_not_followed(self):
        config,events=self.server(redirect=True)
        with self.assertRaises(Exception):http_target.diagnostics(config)
        self.assertEqual(events,['/api/study/info'])

class ControllerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'.env').write_text('fixture')
        for target,value in [('ROOT',self.root),('parse_env',Mock(return_value={})),
                             ('validate',Mock(return_value={'MVP_PROJECT':PROJECT,'API_PORT':'18090'})),
                             ('Compose',Mock()),('lock',Mock(side_effect=nullcontext)),
                             ('require_no_active_fault',Mock())]:
            p=patch.object(manage,target,value);p.start();self.addCleanup(p.stop)

    def execute(self,action):
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):return manage.main([action])

    def test_diagnose_returns_failure_for_unreachable_dependency(self):
        with patch.object(http_target,'guard'),patch.object(http_target,'diagnostics',return_value={'dependencies_reachable':False}):
            self.assertEqual(self.execute('diagnose'),1)

    def test_diagnose_returns_success_for_reachable_dependencies(self):
        with patch.object(http_target,'guard'),patch.object(http_target,'diagnostics',return_value={'dependencies_reachable':True}):
            self.assertEqual(self.execute('diagnose'),0)

    def test_guard_failure_prevents_smoke_subprocess(self):
        with patch.object(http_target,'guard',side_effect=RuntimeError('mismatch')),patch.object(manage.subprocess,'run') as run:
            self.assertEqual(self.execute('smoke'),1)
        run.assert_not_called()

    def test_smoke_passes_exact_project_and_uses_lock(self):
        with patch.object(http_target,'guard') as guard,patch.object(manage.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.execute('smoke'),0)
        guard.assert_called_once();manage.lock.assert_called_once();manage.require_no_active_fault.assert_called_once()
        self.assertEqual(run.call_args.args[0][-2:],['--project',PROJECT])
        self.assertEqual(run.call_args.kwargs['timeout'],90)

    def test_fault_guard_prevents_http_guard_and_write(self):
        manage.require_no_active_fault.side_effect=RuntimeError('fault active')
        with patch.object(http_target,'guard') as guard,patch.object(manage.subprocess,'run') as run:
            self.assertEqual(self.execute('smoke'),1)
        guard.assert_not_called();run.assert_not_called()
