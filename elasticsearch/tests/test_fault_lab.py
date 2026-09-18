"""Offline regression tests. FakeES is NOT Elasticsearch and proves no real failover.
Tests exercise request contracts, expected failure interpretation, rollback and CLI.
"""
import copy
import io
import json
import os
import subprocess
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import fault_lab as f
from lablib import APIError, ESClient, INDICES, LAYOUT


class FakeES:
    def __init__(self):
        self.cluster_name = 'cerebro-shard-lab'
        self.uuid = 'unit-test-cluster-uuid'
        self.alive = {f'es0{i}' for i in range(1, 6)}
        self.master = 'es01'
        self.rotation = 0
        self.settings = {'persistent': {}, 'transient': {}}
        self.indices = {}
        self.calls = []
        self.partial = False
        self.search_timeout = False
        self.fault_status_override = None
        self.fail_setting_key = None
        self.fail_recovery = False
        self.wrong_block_status = None
        self.allow_blocked_write = False
        self.missing_canary = False
        for index in INDICES:
            self.indices[index] = {'settings': {'index.uuid': index + '-uuid',
                'index.number_of_shards': str(LAYOUT[index][0]), 'index.number_of_replicas': str(LAYOUT[index][1])},
                'mappings': {'_meta': {'seed_lab': {'signature': 'offline'}}},
                'docs': {f'doc-{i}': {'event_seq': i, 'value': 'synthetic'} for i in range(5)}}

    def guard(self, expected_uuid=None):
        if self.cluster_name != 'cerebro-shard-lab': raise RuntimeError('Wrong cluster')
        if self.uuid == '_na_': raise RuntimeError('Cluster not bootstrapped')
        if expected_uuid and expected_uuid != self.uuid: raise RuntimeError('Cluster UUID changed')
        return self.info('es01')

    def verify_runtime_node(self, node):
        if node not in self.alive: raise OSError('Runtime node unavailable')
        self.guard()

    def info(self, node):
        return {'cluster_uuid': self.uuid, 'cluster_name': self.cluster_name,
                'name': node, 'version': {'number': '7.17.29'}}

    def nodes(self):
        return {n: {'name': n, 'roles': ['master', 'data', 'ingest'],
                     'attributes': {'zone': {'es01': 'a', 'es02': 'b', 'es03': 'c', 'es04': 'a', 'es05': 'b'}[n]}}
                for n in sorted(self.alive)}

    def exclude(self):
        return f.effective(self.settings, f.EXCLUDE) or ''

    def shards(self, selected=None):
        rows = []
        eligible = [n for n in sorted(self.alive) if n not in self.exclude().split(',')]
        for index, data in self.indices.items():
            if selected and index not in selected: continue
            settings = data['settings']
            replicas = int(settings['index.number_of_replicas'])
            for shard in range(int(settings['index.number_of_shards'])):
                candidates = eligible[:]
                primary = f'es0{(shard + self.rotation) % 5 + 1}'
                if primary in candidates: candidates.remove(primary); candidates.insert(0, primary)
                if f.effective(self.settings, f.AWARENESS) == 'zone':
                    seen = set(); unique = []; rest = []
                    for n in candidates:
                        z = self.nodes()[n]['attributes']['zone']
                        if z in seen: rest.append(n)
                        else: seen.add(z); unique.append(n)
                    candidates = unique + rest
                bad = settings.get('index.routing.allocation.require._name', '').startswith('ghost-')
                none = index.startswith('lab-fault-') and f.effective(self.settings, f.ALLOCATION) == 'none'
                disk = index.startswith('lab-fault-') and f.effective(self.settings, f.DISK_KEYS[0]) is not None
                for copy_no in range(1 + replicas):
                    assigned = not bad and copy_no < len(candidates) and not ((none or disk) and copy_no >= 2)
                    rows.append({'index': index, 'shard': str(shard), 'prirep': 'p' if copy_no == 0 else 'r',
                        'state': 'STARTED' if assigned else 'UNASSIGNED',
                        'node': candidates[copy_no] if assigned else None})
        return rows

    def health(self, selected=None):
        rows = self.shards(selected)
        bad = [s for s in rows if s['state'] == 'UNASSIGNED']
        status = 'red' if any(s['prirep'] == 'p' for s in bad) else ('yellow' if bad else 'green')
        if self.fault_status_override and any(n.startswith('lab-fault-') for n in self.indices):
            status = self.fault_status_override
        return {'status': status, 'timed_out': False, 'number_of_nodes': len(self.alive),
                'number_of_data_nodes': len(self.alive), 'unassigned_shards': len(bad),
                'active_primary_shards': sum(s['prirep'] == 'p' and s['state'] == 'STARTED' for s in rows),
                'relocating_shards': 0, 'initializing_shards': 0}

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        path = path.split('?')[0]
        if path == '/': return self.info('es01')
        if path == '/_nodes': return {'nodes': self.nodes(), '_nodes': {'failed': 0}}
        if path.startswith('/_nodes/stats/fs'):
            return {'nodes': {n: {'fs': {'total': {'available_in_bytes': 30 * 1024**3}}} for n in self.alive}}
        if path.startswith('/_nodes/stats/'): return {'nodes': {}}
        if path == '/_cluster/state/master_node,nodes': return {'master_node': self.master, 'nodes': self.nodes()}
        if path.startswith('/_cluster/health'):
            selected = path.split('/')[3].split(',') if len(path.split('/')) > 3 else None
            return self.health(selected)
        if path == '/_cluster/settings':
            if method == 'PUT':
                if self.fail_setting_key and any(self.fail_setting_key in body.get(s, {}) and body[s][self.fail_setting_key] is not None
                                                  for s in ('persistent', 'transient')):
                    raise APIError(400, method, path, 'Injected setting error')
                for scope, fields in body.items():
                    for key, value in fields.items():
                        if value is None: self.settings[scope].pop(key, None)
                        else: self.settings[scope][key] = str(value).lower() if isinstance(value, bool) else value
            return copy.deepcopy(self.settings)
        if path.startswith('/_cat/shards'):
            parts = path.split('/')
            return self.shards(parts[3].split(',') if len(parts) > 3 else None)
        if path.startswith('/_cat/recovery'): return []
        if path == '/_cluster/pending_tasks': return {'tasks': []}
        if path == '/_cluster/allocation/explain':
            data = self.indices[body['index']]
            if data['settings'].get('index.routing.allocation.require._name', '').startswith('ghost-'): decider = 'filter'
            elif f.effective(self.settings, f.ALLOCATION) == 'none': decider = 'enable'
            elif f.effective(self.settings, f.DISK_KEYS[0]): decider = 'disk_threshold'
            else: decider = 'same_shard'
            return {'index': body['index'], 'shard': body['shard'], 'current_state': 'unassigned',
                    'node_allocation_decisions': [{'deciders': [{'decider': decider, 'decision': 'NO'}]}]}
        parts = path.strip('/').split('/')
        index = parts[0]
        if len(parts) == 1 and method == 'PUT':
            if index in self.indices: raise APIError(400, method, path, 'resource_already_exists_exception')
            settings = {'index.uuid': index + '-uuid'}
            for k, v in body.get('settings', {}).items(): settings[k if k.startswith('index.') else 'index.' + k] = str(v)
            self.indices[index] = {'settings': settings, 'mappings': copy.deepcopy(body['mappings']), 'docs': {}}
            return {'acknowledged': True}
        if index not in self.indices: raise APIError(404, method, path, 'index_not_found_exception')
        data = self.indices[index]
        if method == 'DELETE' and len(parts) == 1:
            del self.indices[index]; return {'acknowledged': True}
        if parts[1] == '_mapping': return {index: {'mappings': copy.deepcopy(data['mappings'])}}
        if parts[1] == '_settings':
            if method == 'PUT':
                if self.fail_recovery and any(v is None for v in body.values()): raise APIError(503, method, path, 'Injected rollback error')
                for k, v in body.items():
                    if v is None: data['settings'].pop(k, None)
                    else: data['settings'][k] = str(v).lower() if isinstance(v, bool) else str(v)
            return {index: {'settings': copy.deepcopy(data['settings'])}}
        if parts[1] == '_count': return {'count': len(data['docs']), '_shards': {'failed': int(self.partial)}}
        if parts[1] == '_search':
            if data['settings'].get('index.routing.allocation.require._name', '').startswith('ghost-'):
                raise APIError(503, method, path, 'search_phase_execution_exception')
            docs = data['docs']
            term = (body or {}).get('query', {}).get('term')
            if term: docs = {k: v for k, v in docs.items() if all(v.get(a) == b for a, b in term.items())}
            hits = [{'_id': k, '_source': v} for k, v in sorted(docs.items())]
            return {'timed_out': self.search_timeout, '_shards': {'failed': int(self.partial)},
                    'hits': {'total': {'value': len(hits), 'relation': 'eq'}, 'hits': hits[:(body or {}).get('size', 10)]}}
        if parts[1] == '_doc':
            key = parts[2]
            if method == 'PUT':
                if data['settings'].get('index.blocks.write') == 'true' and not self.allow_blocked_write:
                    raise APIError(self.wrong_block_status or 403, method, path, 'cluster_block_exception')
                data['docs'][key] = copy.deepcopy(body)
                return {'result': 'created', '_shards': {'failed': 0}}
            found = key in data['docs'] and not self.missing_canary
            return {'found': found, '_source': copy.deepcopy(data['docs'].get(key)) if found else None}
        raise AssertionError((method, path, body))


class FakeRuntime:
    def __init__(self, api):
        self.api, self.actions = api, []
        self.fail_start = False
    def action(self, action, node):
        self.actions.append((action, node))
        if action in ('stop', 'kill'):
            self.api.alive.remove(node)
            if self.api.master == node: self.api.master = sorted(self.api.alive)[0]
        elif action == 'start':
            if self.fail_start: raise OSError('Injected start failure')
            self.api.alive.add(node)
    def request(self, node, method, path, body=None):
        if node not in self.api.alive: raise OSError('Node down')
        if path == '/': return self.api.info(node)
        return self.api.request(method, path, body)


class FaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = FakeES(); self.runtime = FakeRuntime(self.api)
        self.drill = f.Drill(self.api, self.runtime, self.temp.name, timeout=.02, interval=.001)
        self.output = redirect_stdout(io.StringIO()); self.output.__enter__()
    def tearDown(self):
        self.output.__exit__(None, None, None); self.temp.cleanup()
    def test_all_scenarios_recover_and_preserve_seeds(self):
        for scenario in f.SCENARIOS:
            with self.subTest(scenario=scenario):
                baseline = copy.deepcopy({k: self.api.indices[k] for k in INDICES})
                result = self.drill.run(scenario)
                self.assertEqual(result['result'], 'PASS')
                self.assertFalse(self.drill.active.exists())
                self.assertEqual(self.api.indices, baseline)
                self.assertEqual(len(self.api.alive), 5)
    def test_original_persistent_and_transient_restored_not_defaults(self):
        self.api.settings['persistent'][f.REBALANCE] = 'all'
        self.api.settings['transient'][f.REBALANCE] = 'primaries'
        self.api.settings['persistent']['unrelated.setting'] = 'keep'
        original = copy.deepcopy(self.api.settings)
        self.drill.run('rebalance-disabled')
        self.assertEqual(self.api.settings, original)
    def test_original_replica_count_restored(self):
        self.api.indices[INDICES[0]]['settings']['index.number_of_replicas'] = '2'
        self.drill.run('too-many-replicas')
        self.assertEqual(self.api.indices[INDICES[0]]['settings']['index.number_of_replicas'], '2')
    def test_wrong_cluster_no_mutations(self):
        self.api.cluster_name = 'production'
        with self.assertRaisesRegex(RuntimeError, 'Wrong cluster'): self.drill.run('node-stop')
        self.assertFalse(self.runtime.actions)
        self.assertFalse(self.api.calls)
    def test_unbootstrapped_no_injection(self):
        self.api.uuid = '_na_'
        with self.assertRaisesRegex(RuntimeError, 'not bootstrapped'): self.drill.run('node-stop')
        self.assertFalse(self.runtime.actions)
    def test_partial_count_200_is_failure(self):
        self.api.partial = True
        with self.assertRaisesRegex(RuntimeError, 'Partial shard'): self.drill.run('write-block')
        self.assertFalse(self.drill.active.exists())
    def test_search_timed_out_200_is_failure(self):
        self.api.search_timeout = True
        with self.assertRaisesRegex(RuntimeError, 'timed out'): self.drill.run('write-block')
    def test_less_than_five_nodes_rejected(self):
        self.api.alive.remove('es05')
        with self.assertRaisesRegex(RuntimeError, '>=5'): self.drill.run('node-stop')
        self.assertFalse(self.runtime.actions)
    def test_seed_count_change_detected_and_journal_kept(self):
        self.drill.run('write-block', leave=True)
        self.api.indices[INDICES[0]]['docs']['extra'] = {'event_seq': 999}
        with self.assertRaisesRegex(RuntimeError, 'Seed count'): self.drill.check()
        with self.assertRaisesRegex(RuntimeError, 'Recovery incomplete'): self.drill.recover()
        self.assertTrue(self.drill.active.exists())
        del self.api.indices[INDICES[0]]['docs']['extra']
        self.drill.recover()
        self.assertFalse(self.drill.active.exists())
    def test_sample_corruption_same_count_detected(self):
        self.drill.run('write-block', leave=True)
        self.api.indices[INDICES[0]]['docs']['doc-0']['value'] = 'corrupt'
        with self.assertRaisesRegex(RuntimeError, 'Seed count/index UUID/sample'): self.drill.check()
    def test_cluster_uuid_change_prevents_rollback_mutation(self):
        self.drill.run('allocation-disabled', leave=True)
        self.api.uuid = 'another-cluster'
        offset = len(self.api.calls)
        with self.assertRaisesRegex(RuntimeError, 'UUID changed'): self.drill.recover()
        self.assertEqual(len(self.api.calls), offset)
        self.assertTrue(self.drill.active.exists())
    def test_active_journal_prevents_second_fault_and_does_not_recover_first(self):
        self.drill.run('node-stop', leave=True)
        calls = copy.deepcopy(self.runtime.actions)
        other = f.Drill(self.api, self.runtime, self.temp.name, timeout=.02, interval=.001)
        with self.assertRaisesRegex(RuntimeError, 'Active drill'): other.run('write-block')
        self.assertEqual(self.runtime.actions, calls)
        self.assertTrue(other.active.exists())
    def test_new_process_can_recover_journal(self):
        self.drill.run('node-crash', leave=True)
        resumed = f.Drill(self.api, self.runtime, self.temp.name, timeout=.02, interval=.001)
        resumed.recover()
        self.assertFalse(resumed.active.exists())
        self.assertIn(('start', 'es03'), self.runtime.actions)
    def test_promotion_snapshot_refreshed_after_canary_rebalance(self):
        original = self.drill.create_canary
        def create(bad_filter=False):
            original(bad_filter)
            self.api.rotation = 1
        self.drill.create_canary = create
        result = self.drill.run('node-stop')
        ids = [s['shard'] for s in result['old_primaries'] if s['index']==INDICES[0]]
        self.assertIn('1', ids)
        self.assertNotIn('2', ids)
    def test_journal_saved_before_runtime_mutation(self):
        original = self.runtime.action
        def action(verb, node):
            if verb in ('stop', 'kill'):
                saved = json.loads(self.drill.active.read_text())
                self.assertTrue(saved['restart_needed'])
                self.assertEqual(saved['cluster_uuid'], self.api.uuid)
            original(verb, node)
        self.runtime.action = action
        self.drill.run('node-stop')
    def test_setting_error_automatically_restores(self):
        self.api.fail_setting_key = f.ALLOCATION
        with self.assertRaisesRegex(APIError, 'Injected setting'): self.drill.run('allocation-disabled')
        self.assertFalse(self.drill.active.exists())
        self.assertEqual(set(self.api.indices), set(INDICES))
        archived = [json.loads(p.read_text()) for p in Path(self.temp.name).glob('*.json')]
        self.assertEqual(archived[0]['result'], 'FAIL')
    def test_ctrl_c_tries_node_restart(self):
        original = self.runtime.action
        def action(verb, node):
            original(verb, node)
            if verb == 'stop': raise KeyboardInterrupt('test interrupt')
        self.runtime.action = action
        with self.assertRaises(KeyboardInterrupt): self.drill.run('node-stop')
        self.assertIn('es03', self.api.alive)
        self.assertFalse(self.drill.active.exists())
    def test_recovery_failure_keeps_retryable_journal(self):
        self.drill.run('node-stop', leave=True)
        self.runtime.fail_start = True
        with self.assertRaisesRegex(RuntimeError, 'Recovery incomplete'): self.drill.recover()
        self.assertTrue(self.drill.active.exists())
        self.runtime.fail_start = False
        self.drill.recover()
        self.assertFalse(self.drill.active.exists())
    def test_canary_owner_mismatch_not_deleted(self):
        self.drill.run('write-block', leave=True)
        name = self.drill.state['canary']
        self.api.indices[name]['mappings']['_meta']['fault_lab_run_id'] = 'not-our-run'
        with self.assertRaisesRegex(RuntimeError, 'ownership mismatch'): self.drill.recover()
        self.assertIn(name, self.api.indices)
        self.assertTrue(self.drill.active.exists())
    def test_canary_deleted_only_exact_name(self):
        self.drill.run('allocation-filter')
        deleted = [path for method, path, body in self.api.calls if method == 'DELETE']
        self.assertEqual(deleted, ['/' + self.drill.state['canary']])
        self.assertNotIn('*', deleted[0])
    def test_node_stop_does_not_require_yellow(self):
        # Stub reassigns replicas immediately: green after node loss must still pass.
        result = self.drill.run('node-stop')
        self.assertEqual(result['fault_check']['detail']['health']['status'], 'green')
    def test_master_is_detected_not_hardcoded(self):
        self.api.master = 'es04'
        result = self.drill.run('master-failover')
        self.assertIn(('stop', 'es04'), self.runtime.actions)
        self.assertNotEqual(result['fault_check']['detail']['master_after'], 'es04')
    def test_write_block_requires_exact_error_not_arbitrary_failure(self):
        self.api.wrong_block_status = 401
        with self.assertRaisesRegex(RuntimeError, 'Expected 403'): self.drill.run('write-block')
        self.assertFalse(self.drill.active.exists())
    def test_successful_write_during_write_block_is_failure(self):
        self.api.allow_blocked_write = True
        with self.assertRaisesRegex(RuntimeError, 'write succeeded'): self.drill.run('write-block')
    def test_fail_closed_on_unexpected_index(self):
        with self.assertRaisesRegex(RuntimeError, 'INDEX'): self.drill.run('too-many-replicas', index='_all')
        self.assertFalse(self.api.calls)
    def test_fail_closed_on_invalid_node(self):
        for node in ('es01;rm -rf /', '--all', 'es01 es02', 'es00'):
            with self.subTest(node=node), self.assertRaises(ValueError): self.drill.run('node-stop', node=node)
    def test_recovery_project_mismatch_rejected(self):
        self.drill.run('node-stop', leave=True)
        other = f.Drill(self.api, self.runtime, self.temp.name)
        with patch.dict(os.environ, {'COMPOSE_PROJECT_NAME': 'different'}):
            with self.assertRaisesRegex(RuntimeError, 'PROJECT_NAME'): other.load()
    def test_diagnose_is_read_only(self):
        self.drill.diagnose()
        self.assertTrue(all(m == 'GET' for m, _, _ in self.api.calls))
    def test_no_unassigned_explain_is_not_an_error(self):
        self.assertIn('note', self.drill.explain())
    def test_poll_times_out_instead_of_false_success(self):
        with self.assertRaisesRegex(RuntimeError, 'timeout'): self.drill.poll('test', lambda: {'status': 'red'}, lambda _: False)
    def test_disk_units_and_real_disk_guard(self):
        result = f.disk_thresholds({'nodes': {'a': {'fs': {'total': {'available_in_bytes': 100 * 1024**3}}}}})
        low, high, flood = [int(result[k][:-1]) for k in f.DISK_KEYS[:3]]
        self.assertGreater(low, high); self.assertGreater(high, 100 * 1024**3); self.assertEqual(flood, 1)
        with self.assertRaisesRegex(RuntimeError, 'critically low'):
            f.disk_thresholds({'nodes': {'a': {'fs': {'total': {'available_in_bytes': 1024}}}}})
    def test_mutation_requires_yes_before_contacting_server(self):
        with self.assertRaisesRegex(RuntimeError, '--yes'): f.main(['run', 'node-stop'])
    def test_apply_all_not_allowed(self):
        with self.assertRaisesRegex(RuntimeError, 'apply all'): f.main(['apply', 'all', '--yes'])
    def test_lock_rejects_concurrent_command(self):
        with f.exclusive(self.temp.name):
            with self.assertRaisesRegex(RuntimeError, 'Another fault command'):
                with f.exclusive(self.temp.name): pass


class TransportTests(unittest.TestCase):
    def test_es01_connection_failure_falls_back_to_surviving_node(self):
        api = FakeES(); api.alive.remove('es01'); api.master = 'es02'
        runtime = FakeRuntime(api)
        class Down:
            def request(self, *args): raise urllib.error.URLError('connection refused')
        client = f.FaultClient(runtime, Down())
        client.guard(api.uuid)
        self.assertEqual(client.route, 'es02')
        self.assertEqual(client.request('GET', '/_cluster/health')['number_of_nodes'], 4)
    def test_no_replay_of_ambiguous_mutation(self):
        class Down:
            def request(self, *args): raise urllib.error.URLError('lost ACK')
        client = f.FaultClient(FakeRuntime(FakeES()), Down())
        with self.assertRaises(urllib.error.URLError): client.request('PUT', '/x', {})
        self.assertIsNone(client.route)
    def test_wrong_uuid_fallback_refused(self):
        class Down:
            def request(self, *args): raise OSError('down')
        client = f.FaultClient(FakeRuntime(FakeES()), Down()); client.bound_uuid = 'wrong'
        with self.assertRaisesRegex(RuntimeError, 'UUID changed'): client.guard()
    def test_runtime_stop_and_crash_use_different_commands(self):
        runtime = f.ComposeRuntime()
        with patch.object(runtime, 'call') as call:
            runtime.action('stop', 'es03'); runtime.action('kill', 'es03'); runtime.action('start', 'es03')
        self.assertEqual([c.args for c in call.call_args_list],
                         [('stop', '-t', '10', 'es03'), ('kill', '-s', 'SIGKILL', 'es03'), ('start', 'es03')])
    def test_compose_exec_preserves_http_status_and_error_body(self):
        runtime = f.ComposeRuntime()
        with patch.object(runtime, 'call', return_value=b'{"error":"cluster_block_exception"}\n403') as call:
            with self.assertRaises(APIError) as ctx: runtime.request('es02', 'PUT', '/lab-fault-test/_doc/1', {'a': 1})
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn('cluster_block_exception', ctx.exception.detail)
        self.assertIn('@-', call.call_args.args)
        self.assertEqual(call.call_args.kwargs['input_data'], b'{"a":1}')
    def test_subprocess_nonzero_not_ignored(self):
        runtime = f.ComposeRuntime()
        result = subprocess.CompletedProcess([], 1, b'', b'container not running')
        with patch('subprocess.run', return_value=result):
            with self.assertRaisesRegex(OSError, 'Compose exit=1'): runtime.action('start', 'es03')
    def test_runtime_node_identity_not_only_service_name(self):
        api = FakeES(); runtime = FakeRuntime(api)
        client = f.FaultClient(runtime); client.bound_uuid = 'other-cluster'
        with self.assertRaisesRegex(RuntimeError, 'UUID changed'): client.verify_runtime_node('es03')


class HTTPFixture(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()
    def do_PUT(self): self.handle_request()
    def do_DELETE(self): self.handle_request()
    def handle_request(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.loads(raw) if raw else None
        node = self.headers.get('X-Fault-Test-Node')
        if self.path != '/__fixture__/action' and getattr(self.server, 'simulate_entrypoint_loss', False):
            if (node or 'es01') not in self.server.api.alive:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
        try:
            if self.path == '/__fixture__/action':
                self.server.runtime.action(body['action'], body['node'])
                result = {'ok': True}
            elif self.path == '/' and node:
                result = self.server.api.info(node)
            else:
                result = self.server.api.request(self.command, self.path, body)
            status = 200
        except APIError as exc:
            status, result = exc.status, {'error': {'type': exc.detail}}
        data = json.dumps(result).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)


class RealHTTPContractTests(unittest.TestCase):
    def test_full_lifecycle_using_real_http_to_fake_server(self):
        api = FakeES(); server = ThreadingHTTPServer(('127.0.0.1', 0), HTTPFixture); server.api = api
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
                runtime = FakeRuntime(api)
                client = f.FaultClient(runtime, ESClient(f'http://127.0.0.1:{server.server_port}', timeout=3))
                drill = f.Drill(client, runtime, temp, timeout=.3, interval=.01)
                for scenario in ('write-block', 'allocation-filter', 'too-many-replicas', 'allocation-disabled'):
                    with self.subTest(scenario=scenario):
                        self.assertEqual(drill.run(scenario)['result'], 'PASS')
                self.assertEqual(set(api.indices), set(INDICES))
        finally:
            server.shutdown(); server.server_close(); thread.join()



class CLIContractTests(unittest.TestCase):
    """Actual Bash -> Python -> HTTP / fake compose -> real curl command chain.
    Still a mock API: not a live Elasticsearch/container test.
    """
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.api = FakeES(); self.runtime = FakeRuntime(self.api)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), HTTPFixture)
        self.server.api = self.api; self.server.runtime = self.runtime
        self.server.simulate_entrypoint_loss = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        binary = self.folder / 'podman-compose'
        shutil.copyfile(ROOT/'tests/fake_compose_provider.py', binary); binary.chmod(0o755)
        self.env = dict(os.environ, ES_URL=self.url, COMPOSE_PROVIDER='podman-compose',
                        LAB_CLUSTER_NAME='cerebro-shard-lab', COMPOSE_PROJECT_NAME='cerebro-seed-lab',
                        FAULT_TEST_API=self.url, PYTHONDONTWRITEBYTECODE='1',
                        PATH=str(self.folder)+os.pathsep+os.environ['PATH'])
        self.reports = self.folder / 'reports'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()
    def call(self, script, *args):
        return subprocess.run(['bash', str(ROOT/script), *args, '--report-dir', str(self.reports),
                               '--timeout', '5'], cwd=self.folder, env=self.env, text=True,
                              capture_output=True, timeout=90)
    def test_actual_cli_default_suite(self):
        self.api.master = 'es04'
        result = self.call('lab.sh', 'fault', 'run', 'all', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('[suite-PASS]', result.stdout)
        summaries = list(self.reports.glob('suite-*.json'))
        summary = json.loads(summaries[0].read_text())
        self.assertEqual(len(summary['reports']), 8)
        self.assertEqual(set(self.api.indices), set(INDICES))
        self.assertFalse((self.reports/'active.json').exists())
    def test_actual_shell_entrypoint_fallback(self):
        self.api.alive.remove('es01'); self.api.master = 'es02'
        result = subprocess.run(['bash', str(ROOT/'scenarios/04-allocation-explain.sh')],
                                cwd=self.folder, env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('No UNASSIGNED', result.stdout)
    def test_legacy_apply_recover_with_recorded_node(self):
        result = self.call('scenarios/06-node-failure-and-recovery.sh', 'es04', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('es04', self.api.alive)
        result = self.call('scenarios/06-node-failure-and-recovery.sh', '--recover')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('es04', self.api.alive)
    def test_legacy_wrappers_use_managed_verification(self):
        for script in ('02-drain-node.sh', '03-too-many-replicas.sh', '05-impossible-allocation-filter.sh',
                       '07-disable-allocation.sh', '08-zone-awareness.sh', '10-rebalance-control.sh',
                       '11-disk-watermark-simulation.sh'):
            with self.subTest(script=script):
                result = self.call('scenarios/' + script, '--test', '--yes')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('recovery-PASS', result.stdout)
    def test_manual_apply_then_check_then_recover(self):
        for args in (('apply','write-block','--yes'), ('check',), ('recover',)):
            result = self.call('lab.sh', 'fault', *args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.reports/'active.json').exists())
    def test_cli_requires_confirmation_no_http_writes(self):
        result = self.call('lab.sh', 'fault', 'run', 'node-stop')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--yes', result.stderr)
        self.assertEqual(self.api.calls, [])
    def test_wrong_restore_wrapper_does_not_recover_other_fault(self):
        result = self.call('scenarios/03-too-many-replicas.sh', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.call('scenarios/06-node-failure-and-recovery.sh', '--recover')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.reports/'active.json').exists())
        result = self.call('scenarios/03-too-many-replicas.sh', '--restore')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    def test_failed_manual_check_is_not_later_reported_as_pass(self):
        result = self.call('lab.sh', 'fault', 'apply', 'write-block', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.api.allow_blocked_write = True
        result = self.call('lab.sh', 'fault', 'check')
        self.assertNotEqual(result.returncode, 0)
        result = self.call('lab.sh', 'fault', 'recover')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reports = [json.loads(p.read_text()) for p in self.reports.glob('*.json')]
        self.assertEqual(reports[0]['result'], 'FAIL')
        self.assertEqual(reports[0]['recovery']['status'], 'PASS')
    def test_partial_node_response_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Partial node'):
            f.check_response({'_nodes': {'failed': 1}})

if __name__ == '__main__': unittest.main()
