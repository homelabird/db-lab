#!/usr/bin/env python3
"""Journalled, recoverable fault drills for the Elasticsearch 9 lab."""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent / 'lib' / 'es-lab'))
from lablib_core import APIError, ESClient  # noqa: E402

NODES = tuple(f'es0{i}' for i in range(1, 6))
QUORUM_TARGETS = ('es02', 'es03', 'es04')
DEFAULT_TIMEOUT = 180


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeError(f'Refusing symlinked drill state path: {path}')
    fd, temporary = tempfile.mkstemp(prefix='.drill-', suffix='.tmp', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class Runtime:
    def __init__(self):
        self.engine = os.getenv('RUNTIME', '')
        require(self.engine in ('docker', 'podman'), 'No supported local container runtime resolved; run ./lab.sh doctor.')

    def command(self, *args, timeout=60):
        binary = self.engine
        result = subprocess.run([binary, *args], cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
        if result.returncode:
            detail = result.stderr.decode('utf-8', errors='replace')[-1200:]
            raise RuntimeError(f'{binary} {args[0]} failed (exit {result.returncode}): {detail}')
        return result.stdout

    def container(self, node):
        require(node in NODES, 'Invalid ES9 node name')
        name = os.getenv('LAB_CONTAINER_PREFIX', 'es9-lab-') + node
        try:
            result = json.loads(self.command('inspect', name))
        except (RuntimeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f'Cannot inspect the expected ES9 container {name!r}.') from exc
        require(isinstance(result, list) and len(result) == 1, f'Unexpected container identity for {name!r}')
        item = result[0]
        require(item.get('Name', '').lstrip('/') == name, f'Container name mismatch for {node}')
        labels = item.get('Config', {}).get('Labels') or {}
        actual_project = labels.get('com.docker.compose.project') or labels.get('io.podman.compose.project')
        actual_service = labels.get('com.docker.compose.service') or labels.get('io.podman.compose.service')
        expected_project = os.getenv('COMPOSE_PROJECT_NAME', 'es9-lab')
        require(actual_project == expected_project,
                f'{node} belongs to Compose project {actual_project!r}, expected {expected_project!r}.')
        require(actual_service == node, f'{node} container has unexpected Compose service label {actual_service!r}.')
        identity = item.get('Id')
        require(isinstance(identity, str) and identity, f'Container ID missing for {node}')
        return {'name': name, 'id': identity, 'running': bool(item.get('State', {}).get('Running'))}

    def compose(self, *args):
        result = subprocess.run(['bash', str(ROOT / 'lab.sh'), 'compose', *args], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=False)
        if result.returncode:
            detail = result.stderr.decode('utf-8', errors='replace')[-1200:]
            raise RuntimeError(f'Compose {args[0]} failed (exit {result.returncode}): {detail}')
        return result.stdout.decode('utf-8', errors='replace')

    def stop(self, node):
        self.compose('stop', '-t', '10', node)

    def start(self, node):
        self.compose('start', node)


class Drill:
    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.client = ESClient(timeout=8)
        self.runtime = Runtime()
        self.directory = ROOT / 'reports' / 'drills'
        self.active = self.directory / 'active.json'
        self.state = None

    def save(self):
        write_private_json(self.active, self.state)

    def request(self, method, path, body=None, timeout=None):
        return self.client.request(method, path, body, timeout=timeout)

    def identity(self, expected_uuid=None):
        info = self.client.assert_lab()
        if expected_uuid:
            require(info.get('cluster_uuid') == expected_uuid, 'Cluster UUID changed; refusing drill recovery.')
        require(info.get('version', {}).get('number', '').startswith('9.'), 'Connected cluster is not Elasticsearch 9.')
        return info

    def nodes(self):
        body = self.request('GET', '/_nodes')
        nodes = {entry['name']: entry for entry in body.get('nodes', {}).values()}
        return nodes

    def health(self, index=None, timeout=8):
        path = '/_cluster/health' + (('/' + index) if index else '')
        return self.request('GET', path + '?master_timeout=3s&timeout=5s', timeout=timeout)

    def wait_stable(self, expected_nodes, expected_uuid):
        deadline = time.monotonic() + self.timeout
        last = 'no response'
        while time.monotonic() < deadline:
            try:
                info = self.identity(expected_uuid)
                nodes = sorted(self.nodes())
                health = self.health()
                if (nodes == sorted(expected_nodes) and not health.get('timed_out')
                        and health.get('status') == 'green'
                        and health.get('unassigned_shards') == 0
                        and health.get('initializing_shards') == 0
                        and health.get('relocating_shards') == 0):
                    return {'cluster_uuid': info['cluster_uuid'], 'nodes': nodes, 'health': health}
                last = f'nodes={nodes}, health={health}'
            except (APIError, urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
                last = str(exc)
            time.sleep(1)
        raise RuntimeError(f'Cluster did not return to stable green within {self.timeout}s: {last[:1200]}')

    def canary_index(self, run_id):
        return f'lab-es9-drill-{run_id}'

    def create_canary(self):
        index = self.state['canary']
        self.request('PUT', f'/{index}?wait_for_active_shards=all&timeout=30s', {
            'settings': {'number_of_shards': 1, 'number_of_replicas': 1},
            'mappings': {'_meta': {'es9_drill_run_id': self.state['run_id']},
                         'properties': {'run_id': {'type': 'keyword'}, 'message': {'type': 'keyword'}}},
        }, timeout=40)
        self.state['canary_created'] = True
        self.save()
        source = {'run_id': self.state['run_id'], 'message': 'ES9 node failover canary'}
        self.request('PUT', f'/{index}/_doc/probe?refresh=true&wait_for_active_shards=all', source)
        self.state['canary_source'] = source
        self.save()
        self.check_canary()

    def check_canary(self):
        index = self.state['canary']
        expected = self.state['canary_source']
        doc = self.request('GET', f'/{index}/_doc/probe')
        require(doc.get('found') and doc.get('_source') == expected, 'Drill canary document is missing or changed.')
        result = self.request('POST', f'/{index}/_search?allow_partial_search_results=false',
                              {'query': {'term': {'run_id': self.state['run_id']}}, 'size': 2})
        require(not result.get('timed_out') and result.get('_shards', {}).get('failed', 0) == 0,
                'Drill canary search timed out or had failed shards.')
        require(result.get('hits', {}).get('total', {}).get('value') == 1,
                'Drill canary search did not find exactly one document.')
        return {'get': 'PASS', 'strict_search': 'PASS'}

    def owns_canary(self):
        try:
            mapping = self.request('GET', f'/{self.state["canary"]}/_mapping')
        except APIError as exc:
            if exc.status == 404:
                return False
            raise
        owner = mapping.get(self.state['canary'], {}).get('mappings', {}).get('_meta', {}).get('es9_drill_run_id')
        require(owner == self.state['run_id'], 'Canary owner does not match journal; refusing to delete it.')
        return True

    def begin(self, scenario, requested_node):
        require(not self.active.exists(), 'An active ES9 drill exists. Run ./lab.sh drills recover first.')
        info = self.identity()
        nodes = sorted(self.nodes())
        require(nodes == list(NODES), f'Expected exactly ES9 nodes {NODES}; found {nodes}.')
        health = self.health()
        require(health.get('status') == 'green' and not health.get('timed_out'),
                'Baseline must be green before a fault drill.')
        run_id = uuid.uuid4().hex[:12]
        if scenario == 'quorum-loss':
            targets = list(QUORUM_TARGETS)
        else:
            targets = []
            if requested_node != 'auto':
                require(requested_node in NODES[1:], 'node-outage target must be es02..es05; es01 publishes the host API.')
                targets = [requested_node]
        self.state = {
            'format': 1, 'run_id': run_id, 'scenario': scenario, 'status': 'PREPARING',
            'created_at': now(), 'cluster_name': info['cluster_name'], 'cluster_uuid': info['cluster_uuid'],
            'project': os.getenv('COMPOSE_PROJECT_NAME', 'es9-lab'),
            'container_prefix': os.getenv('LAB_CONTAINER_PREFIX', 'es9-lab-'),
            'nodes_before': nodes, 'targets': targets, 'container_ids': {},
            'canary': self.canary_index(run_id), 'canary_created': False,
            'canary_source': {'run_id': run_id, 'message': 'ES9 node failover canary'},
            'observations': [], 'recovery_needed': [],
        }
        self.save()
        try:
            self.create_canary()
            if scenario == 'node-outage' and requested_node == 'auto':
                shards = self.request('GET', f'/_cat/shards/{self.state["canary"]}?format=json')
                candidates = sorted({row.get('node') for row in shards
                                     if row.get('node') in NODES[1:] and row.get('state') == 'STARTED'})
                require(candidates, 'Canary shard copy is not on an eligible node es02..es05.')
                self.state['targets'] = [candidates[0]]
            if scenario == 'node-outage':
                shards = self.request('GET', f'/_cat/shards/{self.state["canary"]}?format=json')
                holders = {row.get('node') for row in shards if row.get('state') == 'STARTED'}
                require(self.state['targets'][0] in holders,
                        f'{self.state["targets"][0]} does not hold a canary shard copy; choose --node auto or another holder.')
            self.state['container_ids'] = {node: self.runtime.container(node)['id'] for node in self.state['targets']}
            for node in self.state['targets']:
                require(self.runtime.container(node)['running'], f'{node} is not running before the drill.')
            self.state['status'] = 'READY_TO_INJECT'
            self.save()
        except BaseException:
            raise

    def inject(self):
        self.state['status'] = 'INJECTING'
        # Mark all targets before the first stop so recover remains safe if the
        # controller is interrupted between stopping nodes.
        self.state['recovery_needed'] = list(self.state['targets'])
        self.save()
        for node in self.state['targets']:
            self.runtime.stop(node)
            self.state['observations'].append({'at': now(), 'event': 'container-stopped', 'node': node})
            self.save()
        if self.state['scenario'] == 'node-outage':
            self.observe_node_outage()
        else:
            self.observe_quorum_loss()
        self.state['status'] = 'OBSERVED'
        self.state['fault_observation'] = {'status': 'PASS', 'at': now()}
        self.save()

    def observe_node_outage(self):
        target = self.state['targets'][0]
        deadline = time.monotonic() + self.timeout
        last = None
        while time.monotonic() < deadline:
            nodes = sorted(self.nodes())
            health = self.health(self.state['canary'])
            last = {'nodes': nodes, 'health': health}
            if (target not in nodes and len(nodes) == 4 and health.get('status') != 'red'
                    and not health.get('timed_out') and health.get('active_primary_shards', 0) >= 1):
                self.state['observations'].append({'at': now(), 'event': 'node-outage-observed', **last})
                self.save()
                self.state['canary_fault_check'] = self.check_canary()
                return
            time.sleep(1)
        raise RuntimeError(f'Node outage was not observed within {self.timeout}s: {last}')

    def observe_quorum_loss(self):
        require(sorted(QUORUM_TARGETS) == sorted(self.state['targets']), 'Invalid quorum-loss target set.')
        running = {node: self.runtime.container(node)['running'] for node in self.state['targets']}
        require(not any(running.values()), f'Quorum targets did not stop: {running}')
        try:
            health = self.health(timeout=8)
        except (APIError, urllib.error.URLError, TimeoutError, OSError) as exc:
            if isinstance(exc, APIError) and exc.status not in (429, 502, 503, 504):
                raise RuntimeError(f'Unexpected API error during quorum loss: HTTP {exc.status}') from exc
            result = {'http_unavailable': True, 'error_type': type(exc).__name__}
        else:
            require(health.get('number_of_nodes', 5) < 3
                    and (health.get('timed_out') or health.get('status') != 'green'),
                    f'Quorum loss did not degrade the cluster as expected: {health}')
            result = {'http_unavailable': False, 'health': health}
        self.state['observations'].append({'at': now(), 'event': 'master-quorum-loss-observed', **result})
        self.save()

    def restore_containers(self):
        for node in self.state.get('recovery_needed', []):
            expected = self.state['container_ids'].get(node)
            current = self.runtime.container(node)
            require(current['id'] == expected, f'{node} container identity changed; refusing automatic start.')
            if not current['running']:
                self.runtime.start(node)

    def recover(self, outcome='PASS'):
        require(self.active.exists(), 'No active ES9 drill journal to recover.')
        self.state = json.loads(self.active.read_text(encoding='utf-8'))
        require(self.state.get('format') == 1 and self.state.get('scenario') in ('node-outage', 'quorum-loss'),
                'Unsupported or invalid ES9 drill journal.')
        require(self.state.get('project') == os.getenv('COMPOSE_PROJECT_NAME', 'es9-lab'),
                'Compose project differs from the saved drill; restore the original .env first.')
        require(self.state.get('container_prefix') == os.getenv('LAB_CONTAINER_PREFIX', 'es9-lab-'),
                'Container prefix differs from the saved drill; restore the original .env first.')
        self.identity(self.state['cluster_uuid'])
        self.state['status'] = 'RECOVERING'
        self.save()
        self.restore_containers()
        stable = self.wait_stable(self.state['nodes_before'], self.state['cluster_uuid'])
        canary_result = None
        if self.owns_canary():
            canary_result = self.check_canary()
            self.request('DELETE', '/' + self.state['canary'])
        self.state['recovery'] = {'status': 'PASS', 'at': now(), 'cluster': stable,
                                  'canary': canary_result, 'restored_nodes': self.state['targets']}
        self.state['status'] = 'RECOVERED'
        self.state['result'] = outcome
        report = self.directory / (self.state['run_id'] + '.json')
        write_private_json(report, self.state)
        self.active.unlink()
        print(f'[report] {report}', flush=True)
        print(json.dumps({'status': outcome, 'scenario': self.state['scenario'],
                          'targets': self.state['targets'], 'recovered': True,
                          'cluster_status': stable['health']['status']}, ensure_ascii=False))
        return self.state

    def run(self, scenario, requested_node='auto', hold=5):
        problem = None
        try:
            self.begin(scenario, requested_node)
            self.inject()
            if hold:
                time.sleep(hold)
        except BaseException as exc:
            problem = exc
            if self.state is not None and self.active.exists():
                self.state['error'] = str(exc) or type(exc).__name__
                self.state['status'] = 'FAULT_FAILED'
                self.save()
        finally:
            if self.state is not None and self.active.exists():
                try:
                    self.recover('FAIL' if problem else 'PASS')
                except BaseException as recovery_error:
                    if problem:
                        raise RuntimeError(f'Drill failed: {problem}; recovery also failed: {recovery_error}') from recovery_error
                    raise
        if problem:
            raise problem


def exclusive(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError('Refusing symlinked drill report directory.')
    lock_path = directory / '.lock'
    if lock_path.is_symlink():
        raise RuntimeError('Refusing symlinked drill lock.')
    lock = lock_path.open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise RuntimeError('Another ES9 drill command is active.') from exc
    return lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('list', 'plan', 'run', 'recover', 'status'))
    parser.add_argument('scenario', nargs='?', choices=('node-outage', 'quorum-loss'))
    parser.add_argument('--node', default='auto', help='node-outage target es02..es05, or auto')
    parser.add_argument('--hold', type=float, default=5, help='fault observation hold, 0..30 seconds')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument('--yes', action='store_true', help='required consent for actual node stop/start')
    args = parser.parse_args(argv)
    require(0 <= args.hold <= 30, '--hold must be between 0 and 30 seconds')
    require(1 <= args.timeout <= 600, '--timeout must be between 1 and 600 seconds')
    if args.command == 'list':
        print('node-outage  Stop one data/master node; check surviving shard copy and canary; restore and verify green.')
        print('quorum-loss  Stop es02/es03/es04 (3 of 5 masters); observe unavailable/degraded quorum; restore all.')
        return 0
    if args.command == 'plan':
        require(args.scenario is not None, 'Choose a scenario: node-outage or quorum-loss')
        targets = [args.node] if args.scenario == 'node-outage' else list(QUORUM_TARGETS)
        if args.scenario == 'node-outage':
            require(args.node == 'auto' or args.node in NODES[1:], '--node must be auto or es02..es05')
        print(json.dumps({'scenario': args.scenario, 'planned_targets': targets,
                          'mutates_cluster': True, 'requires_yes': True,
                          'temporary_index_prefix': 'lab-es9-drill-',
                          'automatic_recovery': True}, indent=2))
        return 0
    if args.command in ('run', 'recover'):
        require(args.yes, 'ES9 node disruption requires --yes; use ./lab.sh drills plan first.')
    if args.command == 'run':
        require(args.scenario is not None, 'Choose a scenario: node-outage or quorum-loss')
        require(args.node == 'auto' or args.node in NODES[1:], '--node must be auto or es02..es05')
    elif args.command in ('recover', 'status'):
        require(args.scenario is None, f'{args.command} does not take a scenario')
    instance = Drill(args.timeout)
    lock = exclusive(instance.directory)
    try:
        if args.command == 'status':
            print(instance.active.read_text(encoding='utf-8') if instance.active.exists() else 'No active ES9 drill.')
        elif args.command == 'recover':
            instance.recover('FAIL')
        else:
            instance.run(args.scenario, args.node, args.hold)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        print(f'[FAIL] {exc}', file=sys.stderr)
        sys.exit(130 if isinstance(exc, KeyboardInterrupt) else 1)
