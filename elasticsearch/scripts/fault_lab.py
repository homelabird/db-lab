#!/usr/bin/env python3
"""Journalled fault drills for this isolated 7.17 lab. Standard library only.

Not a chaos daemon: explicit commands, one active drill, scoped canary writes,
cluster UUID checks, snapshots, bounded polling, and recoverable rollback.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from lablib import APIError, ESClient, INDICES, ROOT, compact, write_json

ALLOCATION = 'cluster.routing.allocation.enable'
EXCLUDE = 'cluster.routing.allocation.exclude._name'
AWARENESS = 'cluster.routing.allocation.awareness.attributes'
REBALANCE = 'cluster.routing.rebalance.enable'
DISK_PREFIX = 'cluster.routing.allocation.disk.'
DISK_KEYS = [DISK_PREFIX + 'watermark.' + s for s in ('low', 'high', 'flood_stage')]
DISK_KEYS += [DISK_PREFIX + 'threshold_enabled', 'cluster.info.update.interval']
NODE_SCENARIOS = {'node-stop', 'node-crash', 'master-failover'}
SCENARIOS = {
    'node-stop': '한 노드 정상 종료 → replica 승격 / 읽기·쓰기 → 노드 재합류',
    'node-crash': '한 노드 SIGKILL → replica 승격 / 읽기·쓰기 → 재기동',
    'master-failover': '현재 elected master 종료 → 다른 master 선출 → 복구',
    'too-many-replicas': '시드 인덱스 replica 과다 → yellow / same_shard → 원래 replica 복원',
    'allocation-disabled': 'allocation=none + canary replica 추가 → yellow / enable → 복원',
    'allocation-filter': 'canary에 존재하지 않는 노드 요구 → red / filter → 복원',
    'write-block': 'canary index.blocks.write=true → green이지만 HTTP 403 → 쓰기 재개',
    'drain-node': '노드 allocation 제외 → 실제 샤드 배출 확인 → 원래 필터 복원',
    'disk-watermark': '현재 여유 공간 기반 watermark 변경 → disk_threshold → 복원 (전체 랩 영향)',
    'zone-awareness': 'zone awareness 활성화 → shard copy zone 분리 확인 → 원래 설정 복원',
    'rebalance-disabled': '자동 rebalance만 중지 → 읽기·쓰기 유지 → 원래 설정 복원',
}
DEFAULT_SUITE = ('node-stop', 'node-crash', 'master-failover', 'too-many-replicas',
                 'allocation-disabled', 'allocation-filter', 'write-block', 'drain-node')
TRANSIENT_ERRORS = (APIError, urllib.error.URLError, OSError, TimeoutError)


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def validate_node(node):
    if not re.fullmatch(r'es0[1-6]', node):
        raise ValueError('NODE must be exactly es01 .. es06')
    return node


def check_response(result):
    require(isinstance(result, dict), 'Invalid JSON response')
    require(not result.get('timed_out', False), 'Request timed out (even if HTTP 200)')
    require(result.get('_shards', {}).get('failed', 0) == 0, 'Partial shard failure (even if HTTP 200)')
    require(result.get('_nodes', {}).get('failed', 0) == 0, 'Partial node response (even if HTTP 200)')
    return result


def effective(settings, key):
    return settings.get('transient', {}).get(key, settings.get('persistent', {}).get(key))


def disk_thresholds(stats):
    free = [int(n['fs']['total']['available_in_bytes']) for n in stats['nodes'].values()]
    require(free and min(free) > 64 * 1024 * 1024, 'Real disk space is already critically low; do not inject a fault')
    # All byte values mean FREE space: low >= high >= flood_stage. No disk filler.
    high = max(free) + 1024 ** 3
    return {DISK_KEYS[0]: f'{high + 1024 ** 3}b', DISK_KEYS[1]: f'{high}b',
            DISK_KEYS[2]: '1b', DISK_KEYS[3]: True, DISK_KEYS[4]: '1s'}


class ComposeRuntime:
    def call(self, *args, input_data=None, timeout=45):
        cmd = [str(ROOT / 'scripts/compose.sh'), *args]
        result = subprocess.run(cmd, cwd=ROOT, input=input_data if input_data is not None else b'', stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
        if result.returncode:
            raise OSError(f'Compose exit={result.returncode}: {result.stderr.decode(errors="replace")[-1500:]}')
        return result.stdout

    def request(self, node, method, path, body=None):
        validate_node(node)
        require(path.startswith('/') and '\n' not in path, 'Invalid API path')
        args = ['exec', '-T', node, 'curl', '--globoff', '-sS', '--connect-timeout', '2',
                '--max-time', '8', '-X', method, '-H', 'Content-Type: application/json',
                '-w', '\n%{http_code}']
        raw = compact(body) if body is not None else None
        if raw is not None:
            args += ['--data-binary', '@-']
        args += ['http://127.0.0.1:9200' + path]
        output = self.call(*args, input_data=raw, timeout=15)
        payload, status = output.rsplit(b'\n', 1)
        status = int(status)
        if status >= 400:
            raise APIError(status, method, path, payload.decode(errors='replace')[:8000])
        return json.loads(payload) if payload else None

    def action(self, action, node):
        validate_node(node)
        if action == 'kill':
            self.call('kill', '-s', 'SIGKILL', node)
        elif action == 'stop':
            self.call('stop', '-t', '10', node)
        elif action == 'start':
            # start, not up/down: keep the existing container and volume.
            self.call('start', node)
        else:
            raise ValueError('Unsupported runtime action')


class FaultClient:
    """Prefer host HTTP. Read fallback via compose exec survives loss of es01.

    Mutating HTTP requests are NOT silently replayed on transport errors. A later
    guard/read selects a surviving endpoint; deterministic recovery may be retried.
    """
    def __init__(self, runtime=None, direct=None):
        self.direct = direct or ESClient(timeout=10)
        self.runtime = runtime or ComposeRuntime()
        self.route = None
        self.bound_uuid = None

    def _validate_identity(self, info):
        require(info.get('cluster_name') == os.getenv('LAB_CLUSTER_NAME', 'cerebro-shard-lab'),
                'Wrong cluster name. No mutation permitted.')
        require(info.get('cluster_uuid') not in (None, '_na_'), 'Cluster not bootstrapped: cluster_uuid=_na_')
        if self.bound_uuid:
            require(info['cluster_uuid'] == self.bound_uuid, 'Cluster UUID changed; refusing mutation/recovery')
        return info

    def request(self, method, path, body=None):
        try:
            if self.route:
                return self.runtime.request(self.route, method, path, body)
            return self.direct.request(method, path, body)
        except (urllib.error.URLError, OSError, TimeoutError) as first:
            if method not in ('GET', 'HEAD'):
                raise
            # Only the supplied project's nodes; do not expose more host ports.
            for node in ('es02', 'es04', 'es05', 'es03', 'es01'):
                if node == self.route:
                    continue
                try:
                    info = self.runtime.request(node, 'GET', '/')
                    self._validate_identity(info)
                    result = self.runtime.request(node, method, path, body)
                    self.route = node
                    return result
                except (urllib.error.URLError, OSError, TimeoutError, APIError):
                    continue
            raise OSError(f'Host API and surviving-node exec endpoints unavailable: {first}') from first

    def guard(self, expected_uuid=None):
        if expected_uuid:
            self.bound_uuid = expected_uuid
        info = self._validate_identity(self.request('GET', '/'))
        self.bound_uuid = info['cluster_uuid']
        return info

    def verify_runtime_node(self, node):
        info = self.runtime.request(node, 'GET', '/')
        self._validate_identity(info)
        require(info.get('name') == node, 'Compose service does not match Elasticsearch node.name')


class Drill:
    def __init__(self, client=None, runtime=None, directory=None, timeout=180, interval=2):
        self.runtime = runtime or ComposeRuntime()
        self.client = client or FaultClient(self.runtime)
        self.directory = Path(directory or ROOT / 'reports/faults')
        self.active = self.directory / 'active.json'
        self.timeout, self.interval = timeout, interval
        self.state = None

    def save(self):
        write_json(self.active, self.state)

    def event(self, name, **fields):
        self.state.setdefault('events', []).append({'at': now(), 'event': name, **fields})
        self.save()
        print(f'[{name}] ' + json.dumps(fields, ensure_ascii=False), flush=True)

    def load(self):
        require(self.active.exists(), 'No active drill journal. Use diagnose; do not blindly reset settings.')
        self.state = json.loads(self.active.read_text(encoding='utf-8'))
        require(self.state.get('format') == 1, 'Unsupported/corrupt recovery journal')
        require(re.fullmatch(r'lab-fault-[0-9a-f]{12}', self.state.get('canary', '')), 'Invalid canary journal')
        require(self.state.get('project') == os.getenv('COMPOSE_PROJECT_NAME', 'cerebro-seed-lab'),
                'COMPOSE_PROJECT_NAME differs from the saved drill; restore the original project config')
        self.client.guard(self.state['cluster_uuid'])
        return self.state

    def poll(self, label, operation, predicate):
        deadline, last = time.monotonic() + self.timeout, 'No sample'
        while True:
            try:
                result = operation()
                if predicate(result):
                    return result
                last = json.dumps(result, ensure_ascii=False)[:1200]
            except TRANSIENT_ERRORS as exc:
                last = str(exc)
            if time.monotonic() >= deadline:
                raise RuntimeError(f'{label}: timeout after {self.timeout}s: {last}')
            time.sleep(min(self.interval, max(0, deadline - time.monotonic())))

    def nodes(self):
        result = check_response(self.client.request('GET', '/_nodes'))
        return {n['name']: n for n in result['nodes'].values()}

    def health(self, index=''):
        path = '/_cluster/health' + ('/' + index if index else '')
        return self.client.request('GET', path + '?master_timeout=3s&timeout=3s')

    def stable(self, expected_nodes=None):
        def sample():
            return {'health': self.health(), 'nodes': sorted(self.nodes())}
        def ready(s):
            h = s['health']
            return (not h.get('timed_out') and h.get('status') == 'green'
                    and all(h.get(k, -1) == 0 for k in ('unassigned_shards', 'initializing_shards', 'relocating_shards'))
                    and (expected_nodes is None or s['nodes'] == sorted(expected_nodes)))
        return self.poll('stable green + original node membership', sample, ready)

    def read_seeds(self):
        """Counts + index UUID + deterministic five-document sample hash, not a full checksum."""
        result = {}
        for index in INDICES:
            count = check_response(self.client.request('GET', f'/{index}/_count'))['count']
            require(count > 0, f'{index}: no seed data. Run 04-seed-data.sh first.')
            detail = self.client.request('GET', f'/{index}/_settings?flat_settings=true')[index]['settings']
            search = check_response(self.client.request('POST', f'/{index}/_search?allow_partial_search_results=false',
                        {'size': 5, 'sort': [{'event_seq': 'asc'}], 'query': {'match_all': {}}}))
            sample = [{'id': d['_id'], 'source': d['_source']} for d in search['hits']['hits']]
            digest = hashlib.sha256(json.dumps(sample, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            result[index] = {'count': count, 'index_uuid': detail['index.uuid'], 'sample_sha256': digest}
        return result

    def probe(self, write=True):
        name = self.state['canary']
        body = {'run_id': self.state['run_id'], 'message': 'fault drill probe', 'value': 1}
        if write:
            result = check_response(self.client.request('PUT', f'/{name}/_doc/probe?refresh=true&timeout=5s', body))
            require(result.get('result') in ('created', 'updated'), 'Probe write not acknowledged')
        found = self.client.request('GET', f'/{name}/_doc/probe')
        require(found.get('found') and found.get('_source') == body, 'Probe data missing or changed')
        search = check_response(self.client.request('POST', f'/{name}/_search?allow_partial_search_results=false',
                                   {'query': {'term': {'run_id': self.state['run_id']}}}))
        require(search['hits']['total']['value'] == 1, 'Canary search must return exactly one document')
        return {'write': 'PASS' if write else 'NOT_REQUESTED', 'get': 'PASS', 'search': 'PASS'}

    def cluster_patch(self, values):
        self.client.guard(self.state['cluster_uuid'])
        # Remove transient overrides for these keys only; preserve both scopes in the journal.
        self.client.request('PUT', '/_cluster/settings',
                            {'persistent': values, 'transient': {k: None for k in values}})

    def create_canary(self, bad_filter=False):
        settings = {'number_of_shards': 1, 'number_of_replicas': 1}
        if bad_filter:
            settings['index.routing.allocation.require._name'] = 'ghost-' + self.state['run_id']
        mapping = {'_meta': {'fault_lab_run_id': self.state['run_id']}, 'properties': {
            'run_id': {'type': 'keyword'}, 'message': {'type': 'text'}, 'value': {'type': 'integer'}}}
        # Journal already exists before creation; even a lost ACK can be recovered by owner metadata.
        self.client.request('PUT', f'/{self.state["canary"]}?wait_for_active_shards=0&timeout=5s',
                            {'settings': settings, 'mappings': mapping})
        if not bad_filter:
            self.stable(self.state['nodes'])
            self.probe()

    def begin(self, scenario, node='es03', index=INDICES[0]):
        require(not self.active.exists(), 'An active drill exists. Run check/recover before another drill.')
        require(scenario in SCENARIOS, 'Unknown scenario')
        validate_node(node)
        require(index in INDICES, 'INDEX must be exactly one of the three seed indices')
        info = self.client.guard()
        nodes = self.nodes()
        require(set(nodes).issubset({f'es0{i}' for i in range(1, 7)}), 'Unexpected node names: not this lab topology')
        data_count = sum(any(r == 'data' or r.startswith('data_') for r in n.get('roles', [])) for n in nodes.values())
        require(data_count >= 5, 'Need >=5 data nodes before a fault drill')
        require(node in nodes, 'Requested node is not a current member')
        self.stable(nodes)
        baseline = self.read_seeds()
        cluster = self.client.request('GET', '/_cluster/settings?flat_settings=true')
        state = self.client.request('GET', '/_cluster/state/master_node,nodes')
        master_id = state['master_node']
        master_name = state['nodes'][master_id]['name']
        if scenario == 'master-failover':
            node = master_name
        if scenario in NODE_SCENARIOS or scenario == 'drain-node':
            require(node != 'es06', 'Node drills target es01..es05; remove scale-out es06 separately')
        if scenario in NODE_SCENARIOS:
            eligible = sum('master' in n.get('roles', []) for n in nodes.values())
            require(eligible >= 3, 'Not enough master-eligible members for a one-node drill')
            self.client.verify_runtime_node(node)
        shards = self.client.request('GET', '/_cat/shards/' + ','.join(INDICES) + '?format=json')
        old_primaries = [{'index': s['index'], 'shard': s['shard']} for s in shards
                         if s.get('node') == node and s['prirep'] == 'p' and s['state'] == 'STARTED']
        if scenario in NODE_SCENARIOS:
            require(old_primaries, 'Target owns no STARTED seed primary; choose another node to test promotion')
        if scenario == 'drain-node':
            require(any(s.get('node') == node for s in shards), 'Target already empty; no drain to test')
        keys = []
        if scenario == 'allocation-disabled': keys = [ALLOCATION]
        if scenario == 'drain-node': keys = [EXCLUDE]
        if scenario == 'zone-awareness':
            require(all(n.get('attributes', {}).get('zone') for n in nodes.values()), 'Missing node.attr.zone')
            keys = [AWARENESS]
        if scenario == 'rebalance-disabled': keys = [REBALANCE]
        thresholds = None
        if scenario == 'disk-watermark':
            thresholds = disk_thresholds(self.client.request('GET', '/_nodes/stats/fs'))
            keys = DISK_KEYS
        index_original = {}
        if scenario == 'too-many-replicas':
            original = self.client.request('GET', f'/{index}/_settings?flat_settings=true')[index]['settings']
            require(original.get('index.auto_expand_replicas', 'false') == 'false', 'Disable auto_expand_replicas first')
            index_original[index] = {'index.number_of_replicas': original.get('index.number_of_replicas')}
        run_id = uuid.uuid4().hex[:12]
        self.state = {'format': 1, 'run_id': run_id, 'scenario': scenario, 'created_at': now(),
                      'cluster_uuid': info['cluster_uuid'], 'cluster_name': info['cluster_name'],
                      'project': os.getenv('COMPOSE_PROJECT_NAME', 'cerebro-seed-lab'),
                      'node': node, 'nodes': sorted(nodes), 'master_before': master_name,
                      'old_primaries': old_primaries, 'index': index, 'baseline': baseline,
                      'canary': 'lab-fault-' + run_id, 'status': 'PREPARING', 'events': [],
                      'cluster_original': {scope: {k: cluster.get(scope, {}).get(k) for k in keys}
                                           for scope in ('persistent', 'transient')},
                      'index_original': index_original, 'restart_needed': False}
        self.event('journal-saved', scenario=scenario, node=node)
        self.create_canary(scenario == 'allocation-filter')
        if scenario in NODE_SCENARIOS:
            # Canary allocation itself may rebalance seed shards. Snapshot again immediately
            # before stopping the node; do not label an earlier relocation as a promotion.
            current = self.client.request('GET', '/_cat/shards/' + ','.join(INDICES) + '?format=json')
            self.state['old_primaries'] = [{'index': s['index'], 'shard': s['shard']} for s in current
                if s.get('node') == node and s['prirep'] == 'p' and s['state'] == 'STARTED']
            require(self.state['old_primaries'], 'Target no longer owns a primary; retry after rebalance')
            if scenario == 'master-failover':
                latest = self.client.request('GET', '/_cluster/state/master_node,nodes')
                require(latest['nodes'][latest['master_node']]['name'] == node,
                        'Master changed while preparing the drill; retry rather than test the wrong node')
            self.client.verify_runtime_node(node)
            self.state['restart_needed'] = True
            self.event('node-action-requested', action='kill' if scenario == 'node-crash' else 'stop')
            self.runtime.action('kill' if scenario == 'node-crash' else 'stop', node)
        elif scenario == 'too-many-replicas':
            self.client.request('PUT', f'/{index}/_settings', {'index.number_of_replicas': data_count})
        elif scenario == 'allocation-disabled':
            self.cluster_patch({ALLOCATION: 'none'})
            self.client.request('PUT', f'/{self.state["canary"]}/_settings', {'index.number_of_replicas': 2})
        elif scenario == 'write-block':
            self.client.request('PUT', f'/{self.state["canary"]}/_settings', {'index.blocks.write': True})
        elif scenario == 'drain-node':
            before = effective(cluster, EXCLUDE) or ''
            value = ','.join(dict.fromkeys([x for x in before.split(',') if x] + [node]))
            self.cluster_patch({EXCLUDE: value})
        elif scenario == 'disk-watermark':
            self.cluster_patch(thresholds)
            self.client.request('PUT', f'/{self.state["canary"]}/_settings', {'index.number_of_replicas': 2})
        elif scenario == 'zone-awareness':
            self.cluster_patch({AWARENESS: 'zone'})
        elif scenario == 'rebalance-disabled':
            self.cluster_patch({REBALANCE: 'none'})
        self.state['status'] = 'INJECTED'
        self.event('injected', scenario=scenario)
        return self.check()

    def explain(self, index=None):
        rows = self.client.request('GET', '/_cat/shards' + ('/' + index if index else '') + '?format=json')
        shard = next((s for s in rows if s['state'] == 'UNASSIGNED'), None)
        if shard is None:
            return {'note': 'No UNASSIGNED shard at this sampling instant'}
        return self.client.request('GET', '/_cluster/allocation/explain?include_yes_decisions=true&include_disk_info=true',
                     {'index': shard['index'], 'shard': int(shard['shard']), 'primary': shard['prirep'] == 'p'})

    def check(self):
        if self.state is None: self.load()
        self.client.guard(self.state['cluster_uuid'])
        name, node = self.state['scenario'], self.state['node']
        detail = {}
        if name in NODE_SCENARIOS:
            membership = self.poll('target node departure', self.nodes,
                                  lambda ns: node not in ns and set(ns) == set(self.state['nodes']) - {node})
            health = self.poll('primary promotion', self.health,
                               lambda h: not h.get('timed_out') and h.get('status') in ('yellow', 'green'))
            rows = self.client.request('GET', '/_cat/shards/' + ','.join(INDICES) + '?format=json')
            for old in self.state['old_primaries']:
                require(any(s['index'] == old['index'] and s['shard'] == old['shard'] and
                            s['prirep'] == 'p' and s['state'] in ('STARTED', 'RELOCATING') and s.get('node') != node
                            for s in rows), 'An original primary was not promoted to a surviving node')
            detail = {'nodes': sorted(membership), 'health': health, 'promoted_seed_primaries': len(self.state['old_primaries'])}
            if name == 'master-failover':
                master = self.client.request('GET', '/_cluster/state/master_node,nodes')
                elected = master['nodes'][master['master_node']]['name']
                require(elected != self.state['master_before'], 'No master change observed')
                detail['master_after'] = elected
        elif name in ('too-many-replicas', 'allocation-disabled', 'allocation-filter', 'disk-watermark'):
            target = self.state['index'] if name == 'too-many-replicas' else self.state['canary']
            expected = 'red' if name == 'allocation-filter' else 'yellow'
            health = self.poll('expected ' + expected, lambda: self.health(target),
                               lambda h: not h.get('timed_out') and h.get('status') == expected and h.get('unassigned_shards', 0) > 0)
            decider = {'too-many-replicas': 'same_shard', 'allocation-disabled': 'enable',
                       'allocation-filter': 'filter', 'disk-watermark': 'disk_threshold'}[name]
            def explains_no(e):
                return any(d.get('decider') == decider and d.get('decision') == 'NO'
                           for n in e.get('node_allocation_decisions', []) for d in n.get('deciders', []))
            explanation = self.poll('allocation decider ' + decider, lambda: self.explain(target), explains_no)
            detail = {'health': health, 'expected_decider': decider, 'allocation_explain': explanation}
        elif name == 'write-block':
            self.stable(self.state['nodes'])
            try:
                self.client.request('PUT', f'/{self.state["canary"]}/_doc/blocked?timeout=3s', {'value': 2})
            except APIError as exc:
                require(exc.status == 403 and 'cluster_block_exception' in str(exc.detail),
                        f'Expected 403 cluster_block_exception, not {exc}')
                detail = {'health': self.health(), 'expected_write_status': 403, 'error': str(exc)}
            else:
                raise RuntimeError('Expected write rejection, but write succeeded')
        elif name == 'drain-node':
            rows = self.poll('drain completion', lambda: self.client.request('GET', '/_cat/shards?format=json'),
                             lambda ss: not any(s.get('node') == node for s in ss))
            self.stable(self.state['nodes'])
            rows = self.client.request('GET', '/_cat/shards?format=json')
            require(not any(s.get('node') == node for s in rows), 'Drain incomplete after convergence')
            detail = {'drained_node': node, 'remaining_shards_on_node': 0, 'total_copies': len(rows)}
        else:
            self.stable(self.state['nodes'])
            if name == 'zone-awareness':
                nodes = self.nodes()
                rows = self.client.request('GET', '/_cat/shards/' + ','.join(INDICES) + '?format=json')
                grouped = {}
                for s in rows:
                    grouped.setdefault((s['index'], s['shard']), []).append(nodes[s['node']]['attributes']['zone'])
                require(all(len(zones) == len(set(zones)) for zones in grouped.values()), 'Copies share a zone')
            key = AWARENESS if name == 'zone-awareness' else REBALANCE
            value = 'zone' if name == 'zone-awareness' else 'none'
            require(effective(self.client.request('GET', '/_cluster/settings?flat_settings=true'), key) == value,
                    'Setting was not actually applied')
            detail = {'applied_setting': key, 'value': value}
        require(self.read_seeds() == self.state['baseline'], 'Seed count/index UUID/sample changed during fault')
        if name == 'allocation-filter':
            # Actual unavailable primary, not merely the color. Never accept partial search success.
            try:
                self.client.request('POST', f'/{self.state["canary"]}/_search?allow_partial_search_results=false',
                                    {'query': {'match_all': {}}})
            except APIError as exc:
                require(exc.status == 503 and any(t in str(exc.detail) for t in
                        ('search_phase_execution_exception', 'no_shard_available_action_exception', 'unavailable_shards_exception')),
                        f'Unexpected error for missing primary: {exc}')
                detail['canary_search_status'] = 503
            else:
                raise RuntimeError('Unavailable canary primary did not reject strict search')
        else:
            detail['probe'] = self.probe(write=name != 'write-block')
        self.state['fault_check'] = {'status': 'PASS', 'at': now(), 'detail': detail}
        self.state['status'] = 'OBSERVED'
        self.event('fault-check-PASS', scenario=name)
        return detail

    def owned_canary(self):
        name = self.state['canary']
        try:
            mapping = self.client.request('GET', f'/{name}/_mapping')
        except APIError as exc:
            if exc.status == 404: return False
            raise
        owner = mapping[name]['mappings'].get('_meta', {}).get('fault_lab_run_id')
        require(owner == self.state['run_id'], 'Canary ownership mismatch: will NOT modify/delete this index')
        return True

    def recover(self):
        if self.state is None: self.load()
        self.client.guard(self.state['cluster_uuid'])
        self.state['status'] = 'RECOVERING'
        self.event('recovery-started')
        errors = []
        if self.state['restart_needed']:
            try: self.runtime.action('start', self.state['node'])
            except (OSError, subprocess.SubprocessError) as exc: errors.append(str(exc))
        original = self.state['cluster_original']
        if any(original.values()):
            try: self.client.request('PUT', '/_cluster/settings', original)
            except TRANSIENT_ERRORS as exc: errors.append(str(exc))
        for index, settings in self.state['index_original'].items():
            require(index in INDICES, 'Invalid seed index in journal')
            try: self.client.request('PUT', f'/{index}/_settings', settings)
            except TRANSIENT_ERRORS as exc: errors.append(str(exc))
        try:
            if self.owned_canary():
                self.client.request('PUT', f'/{self.state["canary"]}/_settings', {
                    'index.routing.allocation.require._name': None, 'index.blocks.write': None,
                    'index.number_of_replicas': 1})
        except (RuntimeError, OSError) as exc: errors.append(str(exc))
        try:
            self.stable(self.state['nodes'])
            require(self.read_seeds() == self.state['baseline'], 'Seed count/index UUID/sample changed after recovery')
            restored = self.client.request('GET', '/_cluster/settings?flat_settings=true')
            for scope, fields in original.items():
                require(all(restored.get(scope, {}).get(k) == v for k, v in fields.items()),
                        f'{scope} settings were not restored to their original values')
            for index, fields in self.state['index_original'].items():
                settings = self.client.request('GET', f'/{index}/_settings?flat_settings=true')[index]['settings']
                require(all(settings.get(k) == v for k, v in fields.items()), 'Original index settings not restored')
            if self.owned_canary():
                self.probe()
                self.client.request('DELETE', '/' + self.state['canary'])
            self.stable(self.state['nodes'])
        except (RuntimeError, OSError) as exc: errors.append(str(exc))
        if errors:
            self.state['recovery'] = {'status': 'FAIL', 'errors': errors}
            self.state['status'] = 'RECOVERY_FAILED'
            self.event('recovery-FAIL', errors=errors)
            raise RuntimeError('Recovery incomplete; journal preserved. Fix the cause, rerun recover: ' + '; '.join(errors))
        self.state['recovery'] = {'status': 'PASS', 'at': now()}
        self.state['status'] = 'RECOVERED'
        self.state['result'] = ('PASS' if self.state.get('fault_check', {}).get('status') == 'PASS'
                                and not self.state.get('error') else 'FAIL')
        self.event('recovery-PASS', result=self.state['result'])
        archive = self.directory / (self.state['run_id'] + '.json')
        write_json(archive, self.state)
        self.active.unlink()
        print(f'[report] {archive}', flush=True)
        return self.state

    def run(self, scenario, node='es03', index=INDICES[0], hold=0, leave=False):
        # Refuse overlapping runs without accidentally rolling back somebody else's active drill.
        require(not self.active.exists(), 'Active drill exists; recover it first')
        problem = None
        try:
            self.begin(scenario, node, index)
            if hold:
                print(f'[observe] fault remains active for --hold={hold}s; Ctrl-C triggers recovery', flush=True)
                time.sleep(hold)
        except BaseException as exc:
            problem = exc
            if self.state:
                self.state['error'] = str(exc) or type(exc).__name__
                self.event('test-FAIL', error=self.state['error'])
        finally:
            if self.state and (not leave or problem is not None):
                try:
                    self.recover()
                except BaseException as exc:
                    if problem:
                        raise RuntimeError(f'Test failed: {problem}; rollback also failed: {exc}') from exc
                    raise
        if problem: raise problem
        return self.state

    def diagnose(self):
        info = self.client.guard()
        report = {'at': now(), 'identity': info, 'note': 'Read-only snapshot. green is not a write/search SLA.'}
        for name, path in {
            'health': '/_cluster/health?master_timeout=3s&timeout=3s',
            'nodes': '/_nodes', 'shards': '/_cat/shards?format=json',
            'settings': '/_cluster/settings?flat_settings=true',
            'disk': '/_nodes/stats/fs', 'jvm': '/_nodes/stats/jvm',
            'thread_pool': '/_nodes/stats/thread_pool',
            'pending_tasks': '/_cluster/pending_tasks',
            'recovery': '/_cat/recovery?active_only=true&format=json',
        }.items():
            try: report[name] = self.client.request('GET', path)
            except (RuntimeError, OSError) as exc: report[name] = {'error': str(exc)}
        try: report['allocation_explain'] = self.explain()
        except (RuntimeError, OSError) as exc: report['allocation_explain'] = {'error': str(exc)}
        path = self.directory / ('diagnose-' + uuid.uuid4().hex[:12] + '.json')
        write_json(path, report)
        print(json.dumps({'health': report.get('health'), 'report': str(path)}, ensure_ascii=False, indent=2))
        return report


@contextmanager
def exclusive(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise RuntimeError('Another fault command is running') from exc
        try: yield
        finally: fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['list', 'run', 'apply', 'check', 'recover', 'status', 'diagnose'])
    parser.add_argument('scenario', nargs='?', choices=[*SCENARIOS, 'all'])
    parser.add_argument('--node', default=os.getenv('NODE', 'es03'))
    parser.add_argument('--index', default=os.getenv('INDEX', INDICES[0]))
    parser.add_argument('--yes', action='store_true', help='Explicit consent: disrupt this isolated lab')
    parser.add_argument('--timeout', type=float, default=float(os.getenv('FAULT_TIMEOUT', '180')))
    parser.add_argument('--hold', type=float, default=0, help='Observe before automatic recovery (run only, 0..600s)')
    parser.add_argument('--report-dir', type=Path, default=ROOT / 'reports/faults')
    args = parser.parse_args(argv)
    require(math.isfinite(args.timeout) and args.timeout > 0, '--timeout must be finite and positive')
    require(math.isfinite(args.hold) and 0 <= args.hold <= 600, '--hold must be finite and 0..600')
    validate_node(args.node)
    require(args.index in INDICES, 'INDEX must be one of the three seed indices')
    if args.command == 'list':
        for key, value in SCENARIOS.items(): print(f'{key:24} {value}')
        print('all = ' + ', '.join(DEFAULT_SUITE) + '\nDisk/zone/rebalance drills are separate, not in all.')
        return 0
    if args.command in ('run', 'apply'):
        require(args.yes, 'Fault injection needs --yes. Read docs/FAULT-DRILLS.md; use only this disposable lab.')
        require(args.scenario is not None, 'Choose a scenario (list)')
        require(args.command == 'run' or args.scenario != 'all', 'apply all is not allowed')
    else:
        require(args.scenario is None, 'Do not pass a scenario to check/recover/status/diagnose')
    require(args.command == 'run' or args.hold == 0, '--hold is only valid for run')
    with exclusive(args.report_dir):
        drill = Drill(directory=args.report_dir, timeout=args.timeout)
        if args.command == 'status':
            print(drill.active.read_text() if drill.active.exists() else 'No active drill. Use diagnose for live state.')
        elif args.command == 'diagnose': drill.diagnose()
        elif args.command == 'check':
            try: drill.check()
            except (RuntimeError, OSError, KeyboardInterrupt) as exc:
                if drill.state:
                    drill.state['error'] = str(exc) or type(exc).__name__
                    drill.state['fault_check'] = {'status': 'FAIL', 'at': now(), 'error': drill.state['error']}
                    drill.event('manual-check-FAIL', error=drill.state['error'])
                raise
        elif args.command == 'recover': drill.recover()
        else:
            scenarios = DEFAULT_SUITE if args.scenario == 'all' else [args.scenario]
            reports = []
            for scenario in scenarios:
                drill = Drill(directory=args.report_dir, timeout=args.timeout)
                reports.append(drill.run(scenario, args.node, args.index, args.hold, args.command == 'apply'))
            if args.scenario == 'all':
                summary = {'status': 'PASS', 'at': now(), 'scope': 'Actual live API/runtime execution by user',
                           'reports': [{'run_id': r['run_id'], 'scenario': r['scenario'], 'result': r['result']} for r in reports]}
                path = args.report_dir / ('suite-' + uuid.uuid4().hex[:12] + '.json')
                write_json(path, summary)
                print(f'[suite-PASS] {path}')
    return 0


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    try: sys.exit(main())
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        print(f'[FAIL] {exc}', file=sys.stderr)
        sys.exit(130 if isinstance(exc, KeyboardInterrupt) else 1)
