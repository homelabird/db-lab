"""Real loopback/filesystem regression tests; DBs and engines are NOT running."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import yaml
from tools import probe, startup, manage

PROJECT = 'db-lab-mvp'
ROOT = Path(manage.__file__).resolve().parents[1]

class ProbeTargetTests(unittest.TestCase):
    def server(self, identity):
        events = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                events.append(('GET', self.path))
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps(identity).encode())
            def do_POST(self):
                events.append(('POST', self.path))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        return f'http://127.0.0.1:{server.server_port}', events

    def test_wrong_project_never_receives_order_write(self):
        url, events = self.server({'application': 'db-lab-mvp', 'study_api': 2, 'project': 'db-lab-mvp-other'})
        with self.assertRaises(Exception): probe.probe(url, 1)
        self.assertFalse(any(method == 'POST' for method, _ in events), events)
        self.assertEqual(events, [('GET', '/api/study/info')])

    def test_missing_identity_never_receives_order_write(self):
        url, events = self.server({})
        with self.assertRaises(Exception): probe.probe(url, 1)
        self.assertFalse(any(method == 'POST' for method, _ in events), events)

    def test_wrong_application_never_receives_order_write(self):
        url, events = self.server({'application': 'other', 'study_api': 2, 'project': PROJECT})
        with self.assertRaises(Exception): probe.probe(url, 1)
        self.assertFalse(any(method == 'POST' for method, _ in events), events)

    def test_identity_must_use_exact_integer_api_version(self):
        url, events = self.server({'application': 'db-lab-mvp', 'study_api': True, 'project': PROJECT})
        with self.assertRaises(Exception): probe.probe(url, 1)
        self.assertFalse(any(method == 'POST' for method, _ in events), events)

    def test_timeout_must_be_a_finite_number_not_boolean(self):
        for value in (True, float('nan'), float('inf'), 0, 301):
            with self.subTest(value=value), patch.object(probe, 'build_opener') as opener:
                with self.assertRaises(ValueError): probe.probe('http://127.0.0.1:18090', value)
                opener.assert_not_called()

    def test_expected_project_checked_before_http(self):
        for value in ('prod', '', '../db-lab-mvp', None):
            with self.subTest(value=value), patch.object(probe, 'build_opener') as opener:
                with self.assertRaises(ValueError): probe.probe('http://127.0.0.1:18090', expected_project=value)
                opener.assert_not_called()

class ReadinessGapTests(unittest.TestCase):
    def test_worker_without_healthcheck_is_not_ready(self):
        row = {'Id': 'a'*64, 'Image': 'sha256:'+'b'*64, 'RestartCount': 0,
               'Config': {'Labels': {'com.docker.compose.project': PROJECT, 'com.docker.compose.service': 'worker'}},
               'State': {'Status':'running', 'Running':True, 'Paused':False, 'ExitCode':0}}
        self.assertFalse(startup.sanitize_container(row, PROJECT, 'worker')['ready'])

    def test_compose_worker_healthchecks_progress_not_just_pid(self):
        worker = yaml.safe_load((ROOT/'compose.yaml').read_text())['services']['worker']
        self.assertEqual(worker.get('healthcheck', {}).get('test'), ['CMD', 'python', '-m', 'mvp_app.worker_health'])

    def test_api_compose_uses_sql_readiness(self):
        api = yaml.safe_load((ROOT/'compose.yaml').read_text())['services']['api']
        self.assertIn('/health/ready', ' '.join(api['healthcheck']['test']))

class WorkerReceiptTests(unittest.TestCase):
    def setUp(self):
        self.health = importlib.import_module('mvp_app.worker_health')
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'worker.json'
        self.clock = Mock(return_value=100.0)
        self.monitor = self.health.WorkerHealth(self.path, clock=self.clock)

    def ready(self):
        return self.health.is_ready(self.path, now=self.clock())

    def test_empty_process_is_not_ready(self):
        self.assertFalse(self.ready())

    def test_one_loop_not_enough(self):
        self.monitor.mark('relay', True)
        self.assertFalse(self.ready())

    def test_both_progressing_loops_ready(self):
        self.monitor.mark('relay', True); self.monitor.mark('consumer', True)
        self.assertTrue(self.ready())

    def test_error_invalidates_readiness_immediately(self):
        self.monitor.mark('relay', True); self.monitor.mark('consumer', True)
        self.monitor.mark('relay', False)
        self.assertFalse(self.ready())

    def test_old_success_does_not_mask_a_hung_loop(self):
        self.monitor.mark('relay', True); self.monitor.mark('consumer', True)
        self.clock.return_value = 131.0
        self.assertFalse(self.ready())

    def test_restarting_process_invalidates_receipt(self):
        self.monitor.mark('relay', True); self.monitor.mark('consumer', True)
        data=json.loads(self.path.read_text()); data['process_start']='not-the-current-process'
        self.path.write_text(json.dumps(data))
        self.assertFalse(self.ready())

    def test_symlink_receipt_refused(self):
        other=self.path.with_name('other.json'); self.path.rename(other); self.path.symlink_to(other)
        self.assertFalse(self.ready())

    def test_nonfinite_or_future_timestamps_refused(self):
        self.monitor.mark('relay', True); self.monitor.mark('consumer', True)
        original=json.loads(self.path.read_text())
        for value in (float('nan'), 101.0, True):
            data=json.loads(json.dumps(original)); data['loops']['relay']['last_success']=value
            self.path.write_text(json.dumps(data))
            self.assertFalse(self.ready())

    def test_receipt_contains_no_credentials_and_is_private(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads(self.path.read_text())), {'schema','pid','process_start','loops'})

if __name__ == '__main__': unittest.main()
