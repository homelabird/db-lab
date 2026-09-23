#!/usr/bin/env python3
"""Podman-first MariaDB HA lab controller. Python standard library only on host."""
import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import tempfile
import urllib.request
import urllib.error
import uuid

from seed import PROFILES, SIZES, expected_counts, generate

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT.parent/'lib'))
from db_lab_benchmark import HostPressureSampler, environment as host_environment
LAB_DIR = ROOT / 'labs'
NODES = ('galera1', 'galera2', 'galera3')
PASSWORDS = ('ROOT_PASSWORD', 'LAB_PASSWORD', 'READONLY_PASSWORD', 'SST_PASSWORD')
PORTS = ('NODE1_PORT', 'NODE2_PORT', 'NODE3_PORT', 'NODE4_PORT', 'NODE5_PORT', 'WRITER_PORT', 'READER_PORT',
         'DASHBOARD_PORT', 'HAPROXY_STATS_PORT', 'CLOUDBEAVER_PORT', 'RESTORE_PORT')

class LabError(RuntimeError): pass

def load_env(path):
    result = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'): continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise LabError('Invalid .env line: use simple KEY=value syntax, no shell expansion.')
        if key in result:
            raise LabError(f'Duplicate .env key: {key}')
        result[key] = value.strip()
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,39}', result.get('LAB_PROJECT', '')):
        raise LabError('LAB_PROJECT: 2..40 lowercase letters/digits/hyphens.')
    for key in PASSWORDS:
        if not re.fullmatch(r'[A-Za-z0-9_-]{12,128}', result.get(key, '')) or result[key].startswith('GENERATE_'):
            raise LabError(f'{key}: run ./lab.sh init, or set 12..128 ASCII letters/digits/_/-.')
    result.setdefault('NODE4_PORT', '13304')
    result.setdefault('NODE5_PORT', '13305')
    missing = [key for key in ('NODE1_PORT', 'NODE2_PORT', 'NODE3_PORT', 'WRITER_PORT',
                               'READER_PORT', 'DASHBOARD_PORT', 'HAPROXY_STATS_PORT',
                               'CLOUDBEAVER_PORT', 'RESTORE_PORT', 'BIND_ADDRESS') if key not in result]
    if missing: raise LabError('Missing .env settings: ' + ', '.join(missing))
    values = [int(result[key]) for key in PORTS]
    if len(values) != len(set(values)) or any(not 1024 <= p <= 65535 for p in values):
        raise LabError('Host ports must be unique and between 1024 and 65535.')
    ipaddress.IPv4Address(result['BIND_ADDRESS'])
    for key in ('BUFFER_POOL_SIZE', 'GCACHE_SIZE'):
        if not re.fullmatch(r'[1-9][0-9]*[MG]', result.get(key, '')):
            raise LabError(f'{key}: use a positive size such as 256M.')
    if int(result.get('WAIT_SECONDS', '600')) < 10: raise LabError('WAIT_SECONDS must be >= 10.')
    count = int(result.get('NODE_COUNT', '3'))
    if count < 3 or count > 5: raise LabError('NODE_COUNT must be 3..5.')
    return result

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def lab_files():
    return sorted(LAB_DIR.glob('*.sql'))

def lab_path(name):
    candidate = Path(name).name
    if candidate != name:
        raise LabError('Lab name must be a file name from labs/.')
    if not candidate.endswith('.sql'):
        candidate += '.sql'
    path = LAB_DIR / candidate
    if not path.is_file():
        available = ', '.join(p.stem for p in lab_files())
        raise LabError(f'Unknown lab {name!r}. Available labs: {available}')
    return path

def lab_is_write(path):
    sql = re.sub(r'--[^\n]*', '', path.read_text()).upper()
    return bool(re.search(r'\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|START|COMMIT|ROLLBACK|SET)\b', sql))

def initialize():
    """Fill only new/placeholder secrets, never rotate credentials on an existing lab."""
    path = ROOT / '.env'
    existed = path.exists()
    text = path.read_text() if existed else (ROOT / '.env.example').read_text()
    generated = []
    for key in PASSWORDS:
        matches = list(re.finditer(r'^' + key + r'=(.*)$', text, re.M))
        if len(matches) > 1: raise LabError('Duplicate .env key: ' + key)
        value = matches[0].group(1).strip() if matches else ''
        if not value or value.startswith('GENERATE_'):
            if (ROOT / '.state/identity.json').exists():
                raise LabError('Existing lab identity found: restore the original .env passwords; init will not rotate them.')
            line = key + '=' + secrets.token_hex(18)
            text = text[:matches[0].start()] + line + text[matches[0].end():] if matches else text.rstrip() + '\n' + line + '\n'
            generated.append(key)
    # Validate before replacing anything. mkstemp is private from creation, not after writing.
    fd, name = tempfile.mkstemp(prefix='.env-init-', dir=ROOT)
    candidate = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream: stream.write(text)
        load_env(candidate)
        if not existed or generated:
            os.replace(candidate, path)
        else:
            path.chmod(0o600)
    finally:
        candidate.unlink(missing_ok=True)
    print('Created/repaired .env (mode 600); existing valid passwords preserved.' if generated
          else '.env is valid; existing values unchanged (mode 600).')


def select_safe_node(states):
    """Never infer that node 1 is authoritative. Called only after ALL nodes are stopped."""
    if set(states) != set(NODES):
        raise LabError(f'Bootstrap requires state checks on all {len(NODES)} active nodes.')
    if not any(s['initialized'] for s in states.values()): return 'galera1', 'authorize-new'
    existing = {n:s for n,s in states.items() if s['initialized']}
    if any(not s['init_complete'] for s in existing.values()):
        raise LabError('Partial initialization detected; inspect logs. No automatic bootstrap.')
    if any(s['seqno'] < 0 for s in existing.values()):
        raise LabError('An existing node has an unknown position. Recover all positions before bootstrap.')
    candidates = [n for n,s in existing.items() if s['safe_to_bootstrap'] == 1 and s['seqno'] >= 0]
    if len(candidates) != 1:
        raise LabError('No unique safe-to-bootstrap node. Run ./lab.sh recover (all nodes stopped).')
    selected = candidates[0]
    ids = {s['uuid'] for s in existing.values() if s['uuid'] and s['seqno'] >= 0}
    if len(ids) != 1 or '00000000-0000-0000-0000-000000000000' in ids or existing[selected]['uuid'] not in ids:
        raise LabError('State UUID mismatch. Refusing to combine different cluster histories.')
    if any(s['seqno'] > existing[selected]['seqno'] for s in existing.values()):
        raise LabError('Safe flag conflicts with sequence positions. Run offline recovery.')
    return selected, 'authorize-safe'

def select_recovered_node(states):
    if set(states) != set(NODES) or any(not s.get('initialized') or s['seqno'] < 0 for s in states.values()):
        raise LabError(f'Recovery requires valid positions from all {len(NODES)} existing nodes.')
    uuids = {s['uuid'] for s in states.values()}
    if len(uuids) != 1 or None in uuids or '00000000-0000-0000-0000-000000000000' in uuids:
        raise LabError('Recovered UUIDs differ or are invalid: manual investigation required.')
    return max(NODES, key=lambda n: states[n]['seqno'])

class Lab:
    def __init__(self):
        if not (ROOT / '.env').exists(): raise LabError('First run ./lab.sh init.')
        self.settings = load_env(ROOT / '.env')
        global NODES
        NODES = tuple(f'galera{i}' for i in range(1, int(self.settings.get('NODE_COUNT', '3')) + 1))
        self.env = dict(os.environ, **self.settings)
        self.env['GALERA_NODES'] = ','.join(NODES)
        # Explicitly selected optional services must work across Compose providers.
        self.env['COMPOSE_PROFILES'] = 'tools,restore,ui'
        self.project = self.settings['LAB_PROJECT']
        requested = self.settings.get('CONTAINER_ENGINE', 'auto')
        if requested not in ('auto', 'podman', 'docker'): raise LabError('Invalid CONTAINER_ENGINE.')
        self.engine = next((e for e in (('podman', 'docker') if requested == 'auto' else (requested,)) if shutil.which(e)), None)
        if not self.engine: raise LabError('Podman or Docker is required on the Linux host.')
        if self.engine == 'podman' and shutil.which('podman-compose'):
            self.compose_base = ['podman-compose']
        else:
            self.compose_base = [self.engine, 'compose']
        self.node_image = f'localhost/{self.project}-node:lab'
        self.proxy_image = f'localhost/{self.project}-proxy:lab'
        (ROOT / '.state').mkdir(exist_ok=True)
        identity = ROOT / '.state' / 'identity.json'
        expected = {'project': self.project, 'engine': self.engine}
        if identity.exists() and json.loads(identity.read_text()) != expected:
            raise LabError('Project/engine changed for this directory. Restore .env; use a fresh directory for another lab.')
        if not identity.exists(): identity.write_text(json.dumps(expected))

    def run(self, args, capture=False, check=True, timeout=None, input=None):
        result = subprocess.run(args, cwd=ROOT, env=self.env, input=input, text=True,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None, timeout=timeout)
        if check and result.returncode:
            message = (result.stderr or result.stdout or '').strip()
            for key in PASSWORDS: message = message.replace(self.settings[key], '<redacted>')
            raise LabError(f'Command failed ({result.returncode}): {" ".join(args[:5])}\n{message}')
        return result

    def comp(self, *args, **kwargs):
        args = list(args)
        if args and args[0] == 'run':
            # podman-compose can inherit the service container_name for `run`.
            # Never collide with the stopped DB whose volume we are inspecting.
            if '--name' not in args and not any(a.startswith('--name=') for a in args):
                args[1:1] = ['--name', self.project + '-task-' + secrets.token_hex(8)]
            if '-T' not in args and '--no-TTY' not in args:
                args.insert(1, '-T')
        return self.run(self.compose_base + ['-p', self.project, '-f', str(ROOT / 'compose.yaml')] + args, **kwargs)

    def start_service(self, service):
        status = self.state(service)
        if status == 'paused': raise LabError(service + ' is paused; resume it before starting.')
        if status != 'running':
            # Replace only this stopped container, never the named data/control volumes.
            self.comp('up', '-d', '--no-deps', '--force-recreate', service)


    def name(self, node): return self.project + '-' + node

    def state(self, node):
        p = self.run([self.engine, 'inspect', self.name(node)], capture=True, check=False, timeout=10)
        if p.returncode:
            error = (p.stderr or p.stdout or '').lower()
            if any(msg in error for msg in ('no such container', 'no such object', 'no container with name', 'does not exist')):
                return 'absent'
            raise LabError('Container inspect failed (not an absent node). Check engine/permissions: ' + self.name(node))
        state = json.loads(p.stdout)[0]['State']
        if state.get('Paused') or state.get('Status') == 'paused': return 'paused'
        return 'running' if state.get('Running') else 'stopped'

    def offline(self, node, operation='inspect', *args):
        if self.state(node) in ('running', 'paused'):
            raise LabError(f'{node} is active; offline volume access refused.')
        p = self.comp('run', '--rm', '--no-deps', '--entrypoint', 'python3', node,
                      '/opt/lab/state.py', operation, *args, capture=True, timeout=360)
        # Compose providers may print service setup information before the program's JSON.
        for line in reversed(p.stdout.splitlines()):
            if line.startswith('{'): return json.loads(line)
        raise LabError('Offline helper returned no JSON.\n' + p.stdout + p.stderr)

    def require_all_stopped(self):
        active = [n for n in NODES if self.state(n) in ('running', 'paused')]
        if active: raise LabError('Stop ALL nodes before offline recovery: ' + ', '.join(active))

    def sql_command(self, node, interactive=False, extra=None, program='mariadb'):
        prefix = [self.engine, 'exec', '-it' if interactive else '-i', self.name(node)]
        shell = 'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; exec ' + program + ' --protocol=socket -uroot "$@"'
        return prefix + ['bash', '-c', shell, '--'] + (extra or [])

    def sql(self, node, query, check=True, timeout=30):
        return self.run(self.sql_command(node, extra=['--batch', '--skip-column-names']),
                        input=query, capture=True, check=check, timeout=timeout)

    def health(self, node):
        status = self.state(node)
        if status != 'running': return {'node':node, 'ready':False, 'container':status}
        try:
            p = self.run([self.engine, 'exec', self.name(node), 'python3', '/opt/lab/health.py', '--json'],
                         capture=True, check=False, timeout=8)
            data = json.loads(p.stdout) if p.returncode == 0 else {'ready':False}
            return dict(data, container=status, node=node)
        except (ValueError, subprocess.TimeoutExpired):
            return {'node':node, 'container':status, 'ready':False, 'error':'Probe failed'}

    def healthy_node(self):
        for node in NODES:
            if self.health(node).get('ready'): return node
        raise LabError('No Synced Primary node is ready. Check status/logs; do not force bootstrap.')

    def wait(self, node, size=None):
        deadline = time.monotonic() + int(self.settings['WAIT_SECONDS'])
        print(f'Checking {node}: Primary/Synced' + (f', cluster size {size}' if size else '') + ' ...', flush=True)
        while time.monotonic() < deadline:
            data = self.health(node)
            if data.get('ready') and (size is None or int(data.get('wsrep_cluster_size', 0)) == size): return
            if data.get('container') in ('absent', 'stopped'):
                self.run([self.engine, 'logs', '--tail', '70', self.name(node)], check=False)
                raise LabError(node + ' stopped during startup. Read the logs above.')
            time.sleep(2)
        raise LabError(f'Timeout waiting for {node}. Run ./lab.sh logs {node}. No forced recovery was attempted.')

    def check_galera_transport(self, target='galera1'):
        """Probe the actual container network before waiting for a Galera join."""
        probe = (
            'import socket,sys; '
            's=socket.socket(); s.settimeout(3); '
            's.connect((%r,4567)); s.close()'
        ) % target
        # Docker Compose can spend ~25s creating/attaching this one-shot container
        # under load, so the socket probe alone (3s timeout) is not the bottleneck.
        result = self.comp('run', '--rm', '--no-deps', '--entrypoint', 'python3', 'tools',
                           '-c', probe, capture=True, check=False, timeout=120)
        if result.returncode:
            raise LabError(
                f'Container network cannot reach {target}:4567 from the Compose network. '
                'Check Docker bridge/iptables or rootless networking, '
                'inspect ./lab.sh status and node logs before retrying. '
                'Do not reset/delete DB volumes to troubleshoot a network failure.')

    def build_fingerprint(self, service):
        inputs = ('images/node', 'scripts', 'datasets') if service == 'galera1' else ('images/proxy',)
        digest = hashlib.sha256()
        image_key = 'MARIADB_IMAGE' if service == 'galera1' else 'HAPROXY_IMAGE'
        digest.update(self.settings.get(image_key, '').encode())
        for directory in inputs:
            for path in sorted((ROOT / directory).rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    digest.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes() + b'\0')
        return digest.hexdigest()

    def build(self, force=False):
        stamp = ROOT / '.state/builds.json'
        stamp.parent.mkdir(exist_ok=True)
        try: previous = json.loads(stamp.read_text())
        except (OSError, ValueError): previous = {}
        for service, image in (('galera1', self.node_image), ('proxy', self.proxy_image)):
            fingerprint = self.build_fingerprint(service)
            p = self.run([self.engine, 'image', 'inspect', image], check=False, capture=True)
            image_id = json.loads(p.stdout)[0]['Id'] if p.returncode == 0 else None
            stale = previous.get(service) != {'source':fingerprint, 'image_id':image_id}
            if force or p.returncode or stale:
                if not force and service == 'galera1' and any(self.state(n) in ('running', 'paused') for n in NODES):
                    raise LabError('Node image/source changed or has no build record. Run ./lab.sh down, then ./lab.sh build and ./lab.sh up; volumes are retained.')
                self.comp('build', service)
                built = self.run([self.engine, 'image', 'inspect', image], capture=True)
                previous[service] = {'source':fingerprint, 'image_id':json.loads(built.stdout)[0]['Id']}
                temporary = stamp.with_suffix('.tmp')
                temporary.write_text(json.dumps(previous, indent=2))
                os.replace(temporary, stamp)

    def wait_frontends(self):
        # A running process is not proof of a working SQL route or HTTP listener.
        # Wait for the proxy/dashboard listeners BEFORE probing SQL: HAProxy only
        # routes to a backend after its DNS resolver and health checks converge,
        # which can lag container start by several seconds after --force-recreate.
        host = self.settings['BIND_ADDRESS']
        if host == '0.0.0.0': host = '127.0.0.1'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for service, port, path in (('proxy', 'HAPROXY_STATS_PORT', '/stats'),
                                    ('dashboard', 'DASHBOARD_PORT', '/api/cluster')):
            deadline = time.monotonic() + min(60, int(self.settings['WAIT_SECONDS']))
            while time.monotonic() < deadline:
                if self.state(service) != 'running':
                    raise LabError(service + ' is not running after Compose up; inspect its logs.')
                try:
                    with opener.open('http://' + host + ':' + self.settings[port] + path, timeout=3) as response:
                        if response.status == 200: break
                except (OSError, urllib.error.URLError): pass
                time.sleep(1)
            else: raise LabError(service + ' HTTP endpoint did not become ready.')
        # The SQL route is checked last with a generous retry window so a cold
        # HAProxy backend table cannot fail `up` while the cluster itself is healthy.
        self.comp('run', '--rm', '--no-deps', 'tools', 'check', timeout=360)


    def doctor(self):
        self.run([self.engine, 'info'], capture=True, timeout=30)
        self.run(self.compose_base + ['version'], capture=True, timeout=30)
        self.comp('config', capture=True, timeout=30)
        print('Engine:', self.engine, '| Compose:', ' '.join(self.compose_base))
        print('Project:', self.project, '| Bind:', self.settings['BIND_ADDRESS'])
        print('Port checks (an already-running copy of this lab can own these ports):')
        for key in PORTS:
            s = socket.socket()
            try:
                s.bind((self.settings['BIND_ADDRESS'], int(self.settings[key])))
                print('  ', key, self.settings[key], 'available')
            except OSError: print('  ', key, self.settings[key], 'IN USE: check ss -ltnp')
            finally: s.close()
        free = shutil.disk_usage(ROOT).free / 1024**3
        print(f'Free disk: {free:.1f} GiB. Plan for 3 data copies, binlogs, gcache, backup, restore.')
        print('Learning estimate: 4 vCPU / 6-8 GiB free RAM / 10+ GiB disk; CloudBeaver needs extra RAM.')
        if self.settings['BIND_ADDRESS'] != '127.0.0.1':
            print('WARNING: non-loopback binding. Dashboard/HAProxy stats are unauthenticated. Use a firewall.')

    def up(self):
        self.build()
        active = [n for n in NODES if self.state(n) in ('running', 'paused')]
        healthy = [n for n in active if self.health(n).get('ready')]
        if active and not healthy:
            raise LabError('Nodes exist but none is ready. Check logs; resume paused nodes or do documented offline recovery.')
        if not active:
            states = {n:self.offline(n) for n in NODES}
            selected, authorization = select_safe_node(states)
            print('Bootstrap decision:', selected, authorization)
            self.offline(selected, authorization)
            self.start_service(selected)
            self.wait(selected, 1)
        if self.health('galera1').get('ready'):
            self.check_galera_transport()
        for node in NODES:
            state = self.state(node)
            if state == 'paused': raise LabError(f'{node} paused. Use ./lab.sh resume {node}.')
            if state != 'running':
                self.start_service(node)
                self.wait(node)
        for node in NODES: self.wait(node, len(NODES))
        self.sql(self.healthy_node(), (ROOT / 'datasets/ops-schema.sql').read_text(), timeout=120)
        self.comp('up', '-d', '--no-deps', '--force-recreate', 'proxy', 'dashboard')
        self.wait_frontends()
        self.status()
        print('Ready. Use ./lab.sh seed --size standard, then ./lab.sh verify.')

    def status(self):
        print(f'{"NODE":10} {"CONTAINER":10} {"COMPONENT":12} {"STATE":18} {"SIZE":5} {"READY":5} UUID')
        for node in NODES:
            d = self.health(node)
            print(f'{node:10} {d.get("container","?"):10} {d.get("wsrep_cluster_status","-"):12} '
                  f'{d.get("wsrep_local_state_comment","-"):18} {d.get("wsrep_cluster_size","-"):5} '
                  f'{str(d.get("ready",False)):5} {d.get("wsrep_cluster_state_uuid","-")}')
        print('Dashboard: http://127.0.0.1:' + self.settings['DASHBOARD_PORT'])
        print('HAProxy:   http://127.0.0.1:' + self.settings['HAPROXY_STATS_PORT'] + '/stats')

    def down(self):
        for service in ('proxy', 'dashboard', 'cloudbeaver', 'restore'):
            if self.state(service) == 'running': self.run([self.engine, 'stop', '-t', '30', self.name(service)])
        for node in reversed(NODES):
            if self.state(node) == 'paused': self.run([self.engine, 'unpause', self.name(node)])
            if self.state(node) == 'running': self.run([self.engine, 'stop', '-t', '120', self.name(node)])
        print('Stopped sequentially; volumes retained. ./lab.sh up checks safe_to_bootstrap before restart.')

    def stop_node_for_scale(self, node):
        if self.state(node) == 'paused':
            self.run([self.engine, 'unpause', self.name(node)])
        if self.state(node) == 'running':
            self.run([self.engine, 'stop', '-t', '120', self.name(node)])

    def recover(self, execute=False):
        self.require_all_stopped()
        self.build()
        states = {n:self.offline(n, 'recover') for n in NODES}
        selected = select_recovered_node(states)
        report = {'recovered_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                  'positions':states, 'candidate':selected, 'bootstrap_executed':execute}
        (ROOT / 'reports').mkdir(exist_ok=True)
        (ROOT / 'reports/recovery.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        if not execute:
            print('NO bootstrap performed. Review positions. To apply, run ./lab.sh recover --execute --confirm-recovery.')
            return
        self.require_all_stopped()
        state = states[selected]
        self.offline(selected, 'authorize-recovered', '--uuid', state['uuid'], '--seqno', str(state['seqno']))
        self.start_service(selected)
        self.wait(selected, 1)
        self.up()

    def seed(self, size, replace=False, batch=500, payload=256):
        node = self.healthy_node()
        for n in NODES:
            if not self.health(n).get('ready'): raise LabError(f'Seed requires all {len(NODES)} healthy nodes.')
        existing = self.sql(node, "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='commerce_lab';").stdout.strip()
        if existing != '0' and not replace:
            raise LabError('commerce_lab already exists. To replace ONLY this synthetic schema, use --replace --confirm-replace.')
        # Mark incomplete BEFORE destructive DDL, so a failed replacement cannot look complete.
        self.sql(node, f"REPLACE INTO lab_ops.dataset_manifest VALUES(1,'{size}','loading',NOW(6));")
        self.sql(node, (ROOT/'datasets/commerce-schema.sql').read_text(), timeout=180)
        process = subprocess.Popen(self.sql_command(node, extra=['--batch']), cwd=ROOT, env=self.env,
                                   stdin=subprocess.PIPE, text=True)
        try:
            template = (ROOT/'datasets/commerce-data-template.sql').read_text()
            for stmt in generate(template, size, batch, payload):
                if stmt.startswith('-- Step:'): print(stmt.strip(), flush=True)
                process.stdin.write(stmt)
            process.stdin.close()
            if process.wait() != 0: raise LabError('Seed failed. Marker stays loading; re-run with explicit replacement after diagnosis.')
        except BaseException:
            if process.poll() is None: process.terminate(); process.wait()
            raise
        self.sql(node, "UPDATE lab_ops.dataset_manifest SET state='complete', loaded_at=NOW(6) WHERE id=1;")
        print('Expected row counts:', json.dumps(expected_counts(size)))
        print(self.sql(node, "SELECT table_schema,ROUND(SUM(data_length+index_length)/1024/1024,1) AS MiB FROM information_schema.tables WHERE table_schema IN ('commerce_lab','lab_ops') GROUP BY table_schema;").stdout)
        self.verify()

    def labs(self):
        print('NAME                 TYPE       DESCRIPTION')
        for path in lab_files():
            first = next((line.strip()[2:].strip() for line in path.read_text().splitlines()
                          if line.strip().startswith('--')), '')
            kind = 'write' if lab_is_write(path) else 'read-only'
            print(f'{path.stem:<20} {kind:<10} {first}')

    def run_lab(self, name, node, allow_write=False):
        path = lab_path(name)
        if lab_is_write(path) and not allow_write:
            raise LabError(f'{path.name} can modify lab data. Re-run with --allow-write.')
        self.healthy_node()
        print(f'Running {path.name} on {node} ({("write-enabled" if allow_write else "read-only")})')
        self.run(self.sql_command(node), input=path.read_text())

    def health_all(self, as_json=False):
        states = {node: self.health(node) for node in NODES}
        ready = all(d.get('ready') and str(d.get('wsrep_cluster_size')) == str(len(NODES)) for d in states.values())
        ids = {d.get('wsrep_cluster_state_uuid') for d in states.values()}
        ready = ready and len(ids) == 1 and None not in ids and '' not in ids
        result = {'ready': ready, 'nodes': states, 'scope': 'read-only Primary/Synced membership and UUID'}
        if as_json: print(json.dumps(result, indent=2))
        else: print('READY' if ready else 'NOT READY')
        if not ready: raise LabError('Cluster membership/readiness/UUID check failed.')

    def verify(self):
        data = {n:self.health(n) for n in NODES}
        expected_size = str(len(NODES))
        if any(not d.get('ready') or d.get('wsrep_cluster_size') != expected_size for d in data.values()):
            raise LabError(f'Cluster is not Primary/Synced with {expected_size} members on every node.')
        if len({d.get('wsrep_cluster_state_uuid') for d in data.values()}) != 1:
            raise LabError('Cluster UUID mismatch.')
        node = self.healthy_node()
        token = str(uuid.uuid4())
        self.sql(node, f"INSERT INTO lab_ops.probe VALUES('{token}',@@hostname,NOW(6),'verify');")
        for n in NODES:
            answer = self.sql(n, f"SET SESSION wsrep_sync_wait=1; SELECT COUNT(*) FROM lab_ops.probe WHERE id='{token}';").stdout.strip()
            if answer != '1': raise LabError('Replicated probe missing on ' + n)
        print('PASS: membership, readiness, UUID and causally-synchronized replicated write.')
        manifest = self.sql(node, 'SELECT size_name,state FROM lab_ops.dataset_manifest WHERE id=1;').stdout.strip()
        if manifest:
            size, state = manifest.split('\t')
            if state != 'complete': raise LabError('Dataset loading did not complete.')
            counts = expected_counts(size)
            # History may legitimately grow as the user runs trigger exercises.
            counts.pop('order_status_history')
            for n in NODES:
                query = 'SET SESSION wsrep_sync_wait=1;\n' + '\n'.join(
                    f"SELECT '{t}',COUNT(*) FROM commerce_lab.`{t}`;" for t in counts)
                observed = dict(line.split('\t') for line in self.sql(n, query, timeout=180).stdout.splitlines())
                for t, count in counts.items():
                    actual = int(observed[t])
                    if actual < count:
                        raise LabError(f'{n}.{t}: expected at least {count}, got {actual}. Dataset was modified or incompletely loaded.')
            print('PASS: minimum synthetic dataset row counts on all nodes.')
        balance = self.sql(node, 'SET SESSION wsrep_sync_wait=1; SELECT SUM(balance),COUNT(*) FROM lab_ops.account;').stdout.strip()
        if balance != '100000000\t100': raise LabError('Workload balance invariant failed: ' + balance)
        self.comp('run', '--rm', '--no-deps', 'tools', 'check', capture=False)
        print('PASS: workload sum invariant, writer/reader endpoints and readonly write rejection.')

    def benchmark_account_snapshot(self, node, table, check=True):
        result = self.sql(node, f'SELECT id,balance FROM {table} ORDER BY id;', check=check)
        if result.returncode: return None
        rows = []
        try:
            for line in result.stdout.splitlines():
                account_id, balance = line.split('\t')
                rows.append((int(account_id), int(balance)))
        except (ValueError, TypeError):
            raise LabError('Account snapshot returned malformed rows.')
        if not rows or len({account_id for account_id, _ in rows}) != len(rows):
            raise LabError('Account snapshot is empty or has duplicate IDs.')
        canonical = ''.join(f'{account_id}\t{balance}\n' for account_id, balance in rows)
        return {'account_rows': len(rows), 'balance_total': sum(balance for _, balance in rows),
                'account_fingerprint': hashlib.sha256(canonical.encode()).hexdigest()}

    def create_benchmark_dataset(self, token, node='galera1'):
        if not re.fullmatch(r'[a-f0-9]{16}', token): raise LabError('Invalid benchmark dataset token.')
        accounts, transfers = f'lab_bench_{token}_accounts', f'lab_bench_{token}_transfers'
        source = self.benchmark_account_snapshot(node, 'lab_ops.account')
        if source['account_rows'] < 2 or source['balance_total'] <= 0:
            raise LabError('Source synthetic account dataset is empty or invalid.')
        self.sql(node, f'CREATE TABLE `{accounts}` LIKE lab_ops.account;')
        try:
            self.sql(node, f'INSERT INTO `{accounts}` SELECT * FROM lab_ops.account;')
            self.sql(node, f'CREATE TABLE `{transfers}` LIKE lab_ops.transfer;')
            if self.benchmark_account_snapshot(node, f'`{accounts}`') != source:
                raise LabError('Run-scoped account copy differs from the source account fingerprint.')
        except BaseException as exc:
            if not self.cleanup_benchmark_dataset(accounts, transfers, node):
                raise LabError(f'Benchmark setup failed and run-scoped tables may remain: {accounts}, {transfers}.') from exc
            raise
        return accounts, transfers, source

    def cleanup_benchmark_dataset(self, accounts, transfers, node='galera1'):
        try:
            transfer_drop = self.sql(node, f'DROP TABLE IF EXISTS `{transfers}`;', check=False)
            account_drop = self.sql(node, f'DROP TABLE IF EXISTS `{accounts}`;', check=False)
            remaining = self.sql(node,
                'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() '
                f"AND table_name IN ('{accounts}','{transfers}');", check=False)
            return (transfer_drop.returncode == 0 and account_drop.returncode == 0
                    and remaining.returncode == 0 and remaining.stdout.strip() == '0')
        except (LabError, OSError, subprocess.SubprocessError, ValueError):
            return False

    def rebuild(self, node):
        for other in NODES:
            if other != node and not self.health(other).get('ready'):
                raise LabError('Both surviving nodes must be healthy before rebuilding one node.')
        if self.state(node) == 'paused': self.run([self.engine, 'unpause', self.name(node)])
        if self.state(node) == 'running': self.run([self.engine, 'stop', '-t', '120', self.name(node)])
        for other in NODES:
            if other != node and not self.health(other).get('ready'):
                raise LabError('A surviving node became unready; wipe cancelled.')
        self.offline(node, 'wipe')
        self.start_service(node)
        self.wait(node, len(NODES))
        print('Node rebuilt. Inspect logs for SST and donor selection; no production volume was touched.')

    def backup(self):
        node = self.healthy_node()
        stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
        (ROOT / 'backups').mkdir(exist_ok=True)
        path = ROOT / 'backups' / ('backup-' + stamp + '.sql.gz')
        exists = self.sql(node, "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='commerce_lab';").stdout.strip()
        schemas = ['lab_ops'] + (['commerce_lab'] if exists == '1' else [])
        options = ['--single-transaction', '--quick', '--skip-lock-tables', '--routines', '--triggers', '--events', '--hex-blob', '--databases'] + schemas
        print('Logical snapshot from', node, '. Stop DDL and workload during the backup exercise.')
        p = subprocess.Popen(self.sql_command(node, extra=options, program='mariadb-dump'),
                             cwd=ROOT, env=self.env, stdout=subprocess.PIPE)
        try:
            with gzip.open(path, 'wb') as f: shutil.copyfileobj(p.stdout, f, length=1024*1024)
            p.stdout.close()
            if p.wait(): raise LabError('mariadb-dump failed.')
        except BaseException:
            if p.poll() is None: p.terminate(); p.wait()
            path.unlink(missing_ok=True); raise
        path.chmod(0o600)
        digest = sha256_file(path)
        meta = {'type':'logical-single-transaction', 'source_node':node, 'schemas':schemas,
                'sha256':digest, 'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'server_version':self.sql(node, 'SELECT VERSION();').stdout.strip()}
        path.with_suffix(path.suffix + '.json').write_text(json.dumps(meta, indent=2))
        print('Backup:', path.relative_to(ROOT), '| bytes:', path.stat().st_size)

    def restore(self, filename=None):
        candidates = sorted((ROOT/'backups').glob('backup-*.sql.gz'))
        if filename: path = Path(filename).expanduser().resolve()
        elif candidates: path = candidates[-1]
        else: raise LabError('No backup exists. Run ./lab.sh backup first.')
        if not path.is_file() or path.parent != (ROOT/'backups').resolve():
            raise LabError('Restore accepts only a file inside this project backups/ directory.')
        meta = json.loads(path.with_suffix(path.suffix + '.json').read_text())
        if sha256_file(path) != meta['sha256']:
            raise LabError('Backup checksum mismatch.')
        self.build()
        if self.state('restore') == 'running': self.run([self.engine, 'stop', '-t', '120', self.name('restore')])
        self.offline('restore', 'wipe')
        self.start_service('restore')
        self.wait('restore')
        # Feed the complete dump through docker exec's stdin in one managed
        # subprocess. Keeping the pipe open while waiting can make the client
        # lose its socket mid-statement when the exec stream closes early.
        with gzip.open(path, 'rb') as f:
            dump = f.read()
        result = subprocess.run(self.sql_command('restore', extra=['--batch']),
                                cwd=ROOT, env=self.env, input=dump,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, timeout=600)
        if result.returncode:
            detail = (result.stderr or result.stdout or b'').decode(errors='replace').strip()
            raise LabError('Restore failed; original Galera nodes are unchanged.'
                           + ('\n' + detail if detail else ''))
        isolated = self.sql('restore', 'SELECT @@wsrep_on;').stdout.strip()
        if isolated != '0': raise LabError('Restored instance unexpectedly has wsrep enabled.')
        balance = self.sql('restore', 'SELECT SUM(balance),COUNT(*) FROM lab_ops.account;').stdout.strip()
        if balance != '100000000\t100':
            raise LabError('Restored balance/count invariant failed: ' + balance)
        manifest = self.sql('restore', 'SELECT size_name,state FROM lab_ops.dataset_manifest WHERE id=1;').stdout.strip()
        if manifest:
            size, state = manifest.split('\t')
            if state != 'complete': raise LabError('Backup contains an incomplete synthetic dataset.')
            counts = expected_counts(size); counts.pop('order_status_history')
            query = '\n'.join(f"SELECT '{t}',COUNT(*) FROM commerce_lab.`{t}`;" for t in counts)
            actual = dict(line.split('\t') for line in self.sql('restore', query, timeout=180).stdout.splitlines())
            for table, count in counts.items():
                if int(actual[table]) != count:
                    raise LabError('Restored synthetic row count differs: ' + table)
        print('PASS: standalone isolation, balance invariant and available synthetic row counts.')
        print(self.sql('restore', 'SELECT @@hostname,@@wsrep_on; SHOW DATABASES;').stdout)
        print('Restored ONLY into isolated standalone restore node on 127.0.0.1:' + self.settings['RESTORE_PORT'])


def parser():
    p = argparse.ArgumentParser(description='MariaDB Galera learning lab; see README.md for the guided sequence.')
    sub = p.add_subparsers(dest='command', required=True)
    for command in ('init','doctor','build','up','status','down','verify','backup','ui','routes','labs'):
        sub.add_parser(command)
    q = sub.add_parser('health'); q.add_argument('--json', action='store_true')
    q = sub.add_parser('run-lab', help='Run one SQL lab by name')
    q.add_argument('name')
    q.add_argument('node', choices=NODES, nargs='?', default='galera1')
    q.add_argument('--allow-write', action='store_true')
    q = sub.add_parser('scale', help='Scale the lab between 3 and 5 Galera nodes')
    q.add_argument('count', type=int, choices=(3, 4, 5))
    q.add_argument('--confirm-scale', action='store_true')
    q = sub.add_parser('seed'); q.add_argument('--size', choices=SIZES, default='standard')
    q.add_argument('--profile', choices=PROFILES)
    q.add_argument('--batch', type=int, default=500); q.add_argument('--payload-bytes', type=int, default=256)
    q.add_argument('--replace', action='store_true'); q.add_argument('--confirm-replace', action='store_true')
    q = sub.add_parser('sql'); q.add_argument('node', choices=NODES+('restore',), nargs='?', default='galera1')
    q.add_argument('query', nargs='?')
    q = sub.add_parser('logs'); q.add_argument('node', choices=NODES+('proxy','dashboard','restore'), nargs='?', default='galera1')
    q.add_argument('--tail', type=int, default=120); q.add_argument('-f', '--follow', action='store_true')
    for command in ('stop','start','kill','pause','resume'):
        q = sub.add_parser(command); q.add_argument('node', choices=NODES)
    q = sub.add_parser('rebuild'); q.add_argument('node', choices=NODES); q.add_argument('--confirm-rebuild', action='store_true')
    q = sub.add_parser('recover'); q.add_argument('--execute', action='store_true'); q.add_argument('--confirm-recovery', action='store_true')
    q = sub.add_parser('quorum-demo'); q.add_argument('--confirm-pause', action='store_true')
    q = sub.add_parser('load'); q.add_argument('--seconds', type=int, default=60); q.add_argument('--workers', type=int, default=4)
    q.add_argument('--target', choices=['writer','multi'], default='writer')
    q = sub.add_parser('simulate'); q.add_argument('--seconds', type=int, default=300)
    q.add_argument('--rate', type=int, default=10)
    q.add_argument('--seed', type=int, default=20260918)
    q.add_argument('--mode', choices=('api', 'orders', 'mixed'), default='mixed')
    sub.add_parser('conflict')
    q = sub.add_parser('restore'); q.add_argument('file', nargs='?'); q.add_argument('--confirm-restore', action='store_true')
    q = sub.add_parser('reset'); q.add_argument('--confirm-delete-lab-data', action='store_true')
    from scenarios import register
    register(sub)
    return p

def main():
    args = parser().parse_args()
    if args.command == 'init': initialize(); return
    if args.command == 'scenario':
        from scenarios import no_runtime
        os.environ['NODE_COUNT'] = os.environ.get('NODE_COUNT', '3')
        if no_runtime(args): return
    lab = Lab()
    lock = None
    if args.command in ('build','up','down','recover','rebuild','seed','restore','reset','backup','load','simulate','run-lab','scale','conflict','quorum-demo','stop','start','kill','pause','resume') or (args.command == 'scenario' and args.scenario != 'inspect'):
        lock = (ROOT/'.state/operation.lock').open('w')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise LabError('Another lifecycle/seed/restore operation is active in this lab directory.')
    command = args.command
    if command == 'scenario':
        from scenarios import Runner, ScenarioError
        import scenarios
        scenarios.NODES = NODES
        try: Runner(lab).execute(args)
        except ScenarioError as exc: raise LabError(str(exc)) from exc
    elif command in ('doctor','up','status','down','verify','backup','labs'): getattr(lab, command)()
    elif command == 'health': lab.health_all(args.json)
    elif command == 'run-lab': lab.run_lab(args.name, args.node, args.allow_write)
    elif command == 'scale':
        if not args.confirm_scale: raise LabError('--scale requires --confirm-scale; nodes are started/stopped and cluster membership changes.')
        current = len(NODES)
        if args.count < current:
            active = [n for n in NODES if lab.state(n) in ('running', 'paused')]
            if len(active) != current: raise LabError('Scale-down requires all current nodes to be present.')
            if args.count < 3: raise LabError('Scale-down below 3 would lose Galera quorum.')
            for node in reversed(NODES[args.count:]):
                lab.stop_node_for_scale(node)
        lines = (ROOT / '.env').read_text().splitlines()
        text = '\n'.join(line if not line.startswith('NODE_COUNT=') else f'NODE_COUNT={args.count}' for line in lines) + '\n'
        if not any(line.startswith('NODE_COUNT=') for line in lines): text += f'NODE_COUNT={args.count}\n'
        (ROOT / '.env').write_text(text); (ROOT / '.env').chmod(0o600)
        print(f'NODE_COUNT set to {args.count}. Run ./lab.sh up to reconcile membership.')
    elif command == 'build': lab.build(force=True)
    elif command == 'seed':
        if args.replace and not args.confirm_replace: raise LabError('--replace requires --confirm-replace.')
        if not 1 <= args.batch <= 5000 or not 0 <= args.payload_bytes <= 8192: raise LabError('Invalid batch/payload bounds.')
        if args.profile:
            profile = PROFILES[args.profile]
            args.size, args.batch, args.payload_bytes = profile['size'], profile['batch'], profile['payload_bytes']
        lab.seed(args.size, args.replace, args.batch, args.payload_bytes)
    elif command == 'sql':
        if args.query is not None:
            print(lab.sql(args.node, args.query, timeout=None).stdout, end='')
        elif sys.stdin.isatty(): lab.run(lab.sql_command(args.node, interactive=True))
        else: lab.run(lab.sql_command(args.node), input=sys.stdin.read())
    elif command == 'logs':
        flags = ['-f'] if args.follow else []
        lab.run([lab.engine, 'logs', '--tail', str(args.tail)] + flags + [lab.name(args.node)])
    elif command == 'ui': lab.comp('up', '-d', '--no-deps', 'cloudbeaver')
    elif command in ('stop','kill','pause','resume','start'):
        if command == 'start':
            lab.healthy_node()  # existing Primary component required, never bootstrap one random node
            lab.start_service(args.node); lab.wait(args.node)
        else:
            verb = {'resume':'unpause'}.get(command, command)
            flags = ['-t','120'] if command == 'stop' else []
            lab.run([lab.engine,verb]+flags+[lab.name(args.node)])
    elif command == 'rebuild':
        if not args.confirm_rebuild: raise LabError('Requires --confirm-rebuild; erases only this lab node and copies a surviving node via SST.')
        lab.rebuild(args.node)
    elif command == 'recover':
        if args.execute and not args.confirm_recovery: raise LabError('--execute requires --confirm-recovery.')
        lab.recover(args.execute)
    elif command == 'quorum-demo':
        if not args.confirm_pause: raise LabError('Requires --confirm-pause; pauses nodes 2 and 3 to simulate simultaneous unreachability.')
        lab.verify()
        lab.run([lab.engine,'pause',lab.name('galera2')])
        lab.run([lab.engine,'pause',lab.name('galera3')])
        print('Nodes 2/3 paused. Wait for failure detection, then inspect galera1 readiness. NO bootstrap.')
        print('Restore: ./lab.sh resume galera2; ./lab.sh resume galera3; ./lab.sh status')
    elif command in ('load','simulate','conflict','routes'):
        lab.healthy_node()
        toolcmd = ['route'] if command == 'routes' else [command]
        if command == 'load': toolcmd += ['--seconds',str(args.seconds),'--workers',str(args.workers),'--target',args.target]
        if command == 'simulate':
            lab.comp('run','--rm','--no-deps','--entrypoint','python3','tools',
                     '/opt/lab/simulator.py', '--seconds',str(args.seconds),
                     '--rate',str(args.rate),
                     '--seed',str(args.seed),'--mode',args.mode)
        elif command == 'load':
            started = dt.datetime.now(dt.timezone.utc)
            dataset_token = uuid.uuid4().hex[:16]
            benchmark_node = lab.healthy_node()
            account_table, transfer_table, source_dataset = lab.create_benchmark_dataset(dataset_token, benchmark_node)
            pressure = HostPressureSampler()
            dataset_invariant_passed = False
            try:
                pressure.start()
                result = lab.comp('run','--rm','--no-deps','tools',*toolcmd,
                                  '--account-table',account_table,'--transfer-table',transfer_table,
                                  capture=True,check=False)
                after = lab.benchmark_account_snapshot(benchmark_node, f'`{account_table}`', check=False)
                dataset_invariant_passed = result.returncode == 0 and after == source_dataset
            finally:
                runtime_observation = pressure.stop()
                tables_removed = lab.cleanup_benchmark_dataset(account_table, transfer_table, benchmark_node)
                dataset_state_verified = dataset_invariant_passed and tables_removed
            print(result.stdout, end='')
            stderr = result.stderr or ''
            for key in PASSWORDS: stderr = stderr.replace(lab.settings[key], '<redacted>')
            if stderr: print(stderr, file=sys.stderr, end='')
            marker = next((line[len('BENCHMARK_JSON='):] for line in result.stdout.splitlines()
                           if line.startswith('BENCHMARK_JSON=')), None)
            if marker is None: raise LabError('Workload finished without its machine-readable benchmark summary.')
            native = json.loads(marker)
            version_result = lab.sql(benchmark_node,'SELECT VERSION()',check=False)
            version = version_result.stdout.strip() or None
            runtime = lab.run([lab.engine,'version','--format','{{.Client.Version}}'],capture=True,check=False).stdout.strip()
            finished = dt.datetime.now(dt.timezone.utc)
            run_id = finished.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
            elapsed = float(native.get('elapsed_seconds') or args.seconds)
            counts = native.get('counts', {})
            report = {
                'schema_version': 1, 'run_id': run_id,
                'status': 'FAIL' if result.returncode or not dataset_state_verified else ('PASS' if version else 'UNVERIFIED'),
                'evidence_kind': 'live_database', 'started_utc': started.isoformat(),
                'finished_utc': finished.isoformat(), 'duration_seconds': elapsed,
                'database': {'product': 'MariaDB Galera', 'version': version},
                'workload': {'name': 'synthetic-transfer', 'parameters':
                             {'seconds': args.seconds, 'workers': args.workers, 'target': args.target,
                              'dataset_fingerprint': source_dataset['account_fingerprint']}},
                'environment': {**host_environment(lab.engine,[lab.name(node) for node in ('galera1','galera2','galera3')]),
                                'runtime_observation': runtime_observation,
                                'revision': lab.run(['git','rev-parse','HEAD'],capture=True,check=False).stdout.strip() or None,
                                'source_dirty': bool(lab.run(['git','status','--porcelain'],capture=True,check=False).stdout.strip()),
                                'container_engine': lab.engine, 'container_engine_version': runtime or None},
                'metrics': {'committed_transactions': counts.get('committed', 0),
                            'unresolved_requests': counts.get('unresolved_requests', 0),
                            'retry_attempts': counts.get('retries', 0),
                            'committed_transactions_per_second': round(counts.get('committed', 0)/elapsed, 3) if elapsed else None,
                            'latency_p50_ms': native.get('latency_ms_p50'),
                            'latency_p95_ms': native.get('latency_ms_p95'),
                            'successful_backend': native.get('successful_backend', {}),
                            'errors_by_code': native.get('errors_by_code', {})},
                'verification': {'balance_invariant_expected': source_dataset['balance_total'],
                                 'workload_and_invariant_passed': result.returncode == 0,
                                 'dataset_state_verified': dataset_state_verified,
                                 'run_dataset_invariant_passed': dataset_invariant_passed,
                                 'dataset_state_note': ('Run-scoped account/transfer tables were removed and verified absent.'
                                                        if dataset_state_verified else 'Run-scoped benchmark tables remain or cleanup could not be verified.'),
                                 'source_dataset': source_dataset,
                                 'version_observed': bool(version)},
                'note': 'Lab workload measurement; same workload and controlled host/container state are required for comparison. Not production capacity evidence.'}
            out = ROOT/'reports'/'benchmarks'; out.mkdir(parents=True, exist_ok=True)
            report_path = out/(run_id+'.json')
            fd, temp_name = tempfile.mkstemp(prefix='.benchmark-', dir=out)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
                os.replace(temp_name, report_path)
            finally:
                Path(temp_name).unlink(missing_ok=True)
            print(f'Benchmark report: {report_path.relative_to(ROOT)}')
            if result.returncode or not dataset_state_verified:
                raise LabError(f'Load benchmark acceptance failed (workload rc={result.returncode}, dataset verified={dataset_state_verified}); report retained.')
        else:
            lab.comp('run','--rm','--no-deps','tools',*toolcmd)
    elif command == 'restore':
        if not args.confirm_restore: raise LabError('Requires --confirm-restore: replaces ONLY the separate restore volume.')
        lab.restore(args.file)
    elif command == 'reset':
        if not args.confirm_delete_lab_data: raise LabError('Requires --confirm-delete-lab-data. Deletes this project volumes including restore/UI, not backups or standalone-original.')
        lab.down()
        lab.comp('down','-v','--remove-orphans')
        print('This lab project volumes deleted. Original standalone project and backup files retained.')

if __name__ == '__main__':
    try: main()
    except (LabError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print('ERROR:', exc, file=sys.stderr); sys.exit(1)
    except KeyboardInterrupt:
        print('\nInterrupted. Check ./lab.sh status before continuing.', file=sys.stderr); sys.exit(130)
