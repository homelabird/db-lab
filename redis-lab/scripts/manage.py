#!/usr/bin/env python3
"""Host-side orchestrator: Python standard library + Podman + podman-compose."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'io.redis-sentinel-lab.id'
REDIS = ['redis-1', 'redis-2', 'redis-3']
SENTINELS = ['sentinel-1', 'sentinel-2', 'sentinel-3']
BASE = REDIS + SENTINELS + ['lab-client']
ALL = BASE + ['redis-sandbox']
VOLUMES = ['redis-1-data', 'redis-2-data', 'redis-3-data', 'sentinel-1-state', 'sentinel-2-state', 'sentinel-3-state', 'client-results', 'sandbox-data']
IP_KEYS = ['REDIS_1_IP', 'REDIS_2_IP', 'REDIS_3_IP', 'SENTINEL_1_IP', 'SENTINEL_2_IP', 'SENTINEL_3_IP', 'CLIENT_IP', 'SANDBOX_IP']


def parse_env(path: Path) -> dict[str, str]:
    env = {}
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'): continue
        if '=' not in line: raise ValueError(f'{path.name}:{number}: expected KEY=value')
        key, value = (item.strip() for item in line.split('=', 1))
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or key in env:
            raise ValueError(f'{path.name}:{number}: invalid or duplicate key')
        if not value or any(ch in value for ch in '\r\n\x00') or value.startswith(('"', "'")):
            raise ValueError(f'{path.name}:{number}: use a plain, unquoted value')
        env[key] = value
    return env


def validate_env(env: dict) -> None:
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', env['LAB_NAME']):
        raise ValueError('LAB_NAME must match [a-z][a-z0-9-]{0,39}')
    for key in ('REDIS_PASSWORD', 'SENTINEL_PASSWORD'):
        if not re.fullmatch(r'[A-Za-z0-9_.@%+-]{12,128}', env[key]):
            raise ValueError(f'{key}: use 12..128 letters, digits, _, ., @, %, +, -')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', env['MASTER_NAME']):
        raise ValueError('Invalid MASTER_NAME')
    network = ipaddress.ip_network(env['LAB_SUBNET'], strict=True)
    if network.version != 4 or not 16 <= network.prefixlen <= 27:
        raise ValueError('LAB_SUBNET must be an IPv4 /16../27 network')
    addresses = [ipaddress.ip_address(env[key]) for key in IP_KEYS]
    if len(set(addresses)) != len(addresses): raise ValueError('All eight node addresses must be distinct')
    for address in addresses:
        if address not in network or address in (network.network_address, network.broadcast_address, network.network_address + 1):
            raise ValueError(f'{address} is not a usable non-gateway address in {network}')
    for key in ('REDIS_MAXMEMORY', 'REDIS_CONTAINER_MEMORY', 'SENTINEL_CONTAINER_MEMORY', 'CLIENT_CONTAINER_MEMORY'):
        if not re.fullmatch(r'[1-9][0-9]*[mMgG](?:[bB])?', env[key]):
            raise ValueError(f'{key}: use a memory value such as 192mb or 512m')
    for key in ('REDIS_IMAGE', 'PYTHON_IMAGE'):
        if not re.fullmatch(r'[A-Za-z0-9._/:@+-]+', env[key]): raise ValueError(f'Invalid image reference in {key}')
    if not 1000 <= int(env['DOWN_AFTER_MS']) <= 120000: raise ValueError('DOWN_AFTER_MS must be 1000..120000')
    if not 10000 <= int(env['FAILOVER_TIMEOUT_MS']) <= 600000: raise ValueError('FAILOVER_TIMEOUT_MS must be 10000..600000')


def load_env() -> dict:
    defaults = parse_env(ROOT / '.env.example')
    path = ROOT / '.env'
    if not path.exists():
        text = (ROOT / '.env.example').read_text()
        text = text.replace('CHANGE_ME_REDIS_123', secrets.token_hex(20)).replace('CHANGE_ME_SENTINEL_123', secrets.token_hex(20))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream: stream.write(text)
        print('Created .env with random Redis/Sentinel passwords (0600).', file=sys.stderr)
    overrides = parse_env(path)
    unknown = set(overrides) - set(defaults)
    if unknown: raise ValueError('Unknown .env keys: ' + ', '.join(sorted(unknown)))
    defaults.update(overrides)
    validate_env(defaults)
    (ROOT / '.lab').mkdir(mode=0o700, exist_ok=True)
    (ROOT / 'output').mkdir(exist_ok=True)
    return defaults


class Lab:
    def __init__(self, env: dict):
        self.env = env
        self.name = env['LAB_NAME']
        self.network = self.name + '-net'
        self.proc_env = dict(os.environ, **env)
        self._compose = None

    def redact(self, text: str) -> str:
        for key in ('REDIS_PASSWORD', 'SENTINEL_PASSWORD'): text = text.replace(self.env[key], '<redacted>')
        return text

    def run(self, args, *, capture=True, check=True, timeout=30, input=None):
        try:
            result = subprocess.run([str(a) for a in args], cwd=ROOT, env=self.proc_env,
                text=True, input=input, capture_output=capture, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'Timed out: {args[0]} {args[1] if len(args)>1 else ""}; inspect the affected node.') from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f'{args[0]} is not installed or not on PATH.') from exc
        if check and result.returncode:
            message = self.redact((result.stderr or '') + '\n' + (result.stdout or ''))
            raise RuntimeError(f'Command failed (exit {result.returncode}): {args[0]} {args[1] if len(args)>1 else ""}\n{message.strip()}')
        return result

    def compose(self, *args, capture=False, check=True, timeout=None):
        if self._compose is None:
            if not shutil.which('podman-compose'): raise RuntimeError('podman-compose is required; see README.md.')
            if '--in-pod' not in self.run(['podman-compose', '--help']).stdout:
                raise RuntimeError('This podman-compose lacks --in-pod; install a newer version.')
            self._compose = ['podman-compose', '--in-pod=false', '-p', self.name, '-f', str(ROOT / 'compose.yaml')]
        return self.run(self._compose + list(args), capture=capture, check=check, timeout=timeout)

    def cname(self, node: str) -> str:
        if node not in ALL: raise ValueError(f'Unknown lab node {node!r}; allowed: {", ".join(ALL)}')
        return self.name + '-' + node

    def inspect(self, node: str, required=True) -> dict | None:
        result = self.run(['podman', 'container', 'inspect', self.cname(node)], check=False)
        if result.returncode:
            if required: raise RuntimeError(f'{node} does not exist. Run ./lab.sh up first.')
            return None
        data = json.loads(result.stdout)[0]
        if (data.get('Config', {}).get('Labels') or {}).get(LABEL) != self.name:
            raise RuntimeError(f'REFUSED: {self.cname(node)} does not have this lab ownership label.')
        return data

    def volume_owned(self, suffix: str, required=False):
        if suffix not in VOLUMES: raise ValueError('Unexpected volume name')
        result = self.run(['podman', 'volume', 'inspect', f'{self.name}-{suffix}'], check=False)
        if result.returncode:
            if required: raise RuntimeError('Expected volume not found: ' + suffix)
            return None
        data = json.loads(result.stdout)[0]
        if (data.get('Labels') or {}).get(LABEL) != self.name:
            raise RuntimeError(f'REFUSED: volume {self.name}-{suffix} is not owned by this lab.')
        return data

    def check_network(self):
        desired = ipaddress.ip_network(self.env['LAB_SUBNET'])
        existing = json.loads(self.run(['podman', 'network', 'ls', '--format', 'json']).stdout)
        own_exists = False
        for entry in existing:
            name = entry.get('name', entry.get('Name'))
            if not name: continue
            data = json.loads(self.run(['podman', 'network', 'inspect', name]).stdout)[0]
            if name == self.network:
                labels = data.get('labels') or data.get('Labels') or {}
                if labels.get(LABEL) != self.name: raise RuntimeError(f'REFUSED: network {name} is not owned by this lab.')
                own_exists = True
                subnets = [item['subnet'] for item in data.get('subnets', []) if item.get('subnet')]
                if subnets and str(desired) not in subnets: raise RuntimeError('Existing lab subnet differs from .env.')
                continue
            for item in data.get('subnets', []):
                other = ipaddress.ip_network(item['subnet'])
                if other.version == 4 and desired.overlaps(other):
                    raise RuntimeError(f'Network overlap: {desired} with {name} ({other}). Change LAB_SUBNET and all IPs before startup.')
        if shutil.which('ip'):
            routes = self.run(['ip', '-j', '-4', 'route', 'show'], check=False)
            if routes.returncode == 0:
                for entry in json.loads(routes.stdout):
                    target = entry.get('dst', 'default')
                    if target == 'default': continue
                    route = ipaddress.ip_network(target, strict=False)
                    if desired.overlaps(route) and not (own_exists and desired == route):
                        raise RuntimeError(f'Host/VPN route {route} overlaps {desired}; change the lab subnet and all IPs.')

    def check_env_state(self, save=False):
        path = ROOT / '.lab' / 'environment.sha256'
        keys = ['LAB_NAME', 'REDIS_PASSWORD', 'SENTINEL_PASSWORD', 'MASTER_NAME', 'LAB_SUBNET'] + IP_KEYS
        digest = hashlib.sha256('\n'.join(f'{key}={self.env[key]}' for key in keys).encode()).hexdigest()
        if path.exists() and path.read_text().strip() != digest:
            raise RuntimeError('Persistent settings differ from .env. Restore previous .env or deliberately reset using previous settings. No data was deleted.')
        if save: path.write_text(digest + '\n')

    def doctor(self):
        print('Checking Podman, Compose, resource ownership and subnet conflicts...', flush=True)
        self.run(['podman', 'info', '--format', 'json'])
        print(self.run(['podman', '--version']).stdout.strip())
        version = self.run(['podman-compose', '--version']); print((version.stdout + version.stderr).strip())
        self.compose('config', capture=True)  # Do not print secrets.
        for node in ALL: self.inspect(node, required=False)
        for suffix in VOLUMES: self.volume_owned(suffix)
        self.check_network(); self.check_env_state()
        selinux = self.run(['getenforce'], check=False).stdout.strip() if shutil.which('getenforce') else 'not detected'
        print(f'SELinux: {selinux}; runtime mounts: named volumes only')
        print(f'Network: {self.network} ({self.env["LAB_SUBNET"]}); host ports: none')
        print('PASS: preflight. Images and containers still need to be built and tested.')

    def execute(self, node: str, command: list, *, capture=True, check=True, timeout=30, tty=False):
        self.inspect(node)
        args = ['podman', 'exec'] + (['-it'] if tty else [])
        return self.run(args + [self.cname(node)] + command, capture=capture, check=check, timeout=timeout)

    def cli(self, node: str, args: list, *, capture=True, check=True, timeout=30, tty=False):
        if node not in REDIS + SENTINELS + ['redis-sandbox']: raise ValueError('CLI target must be Redis or Sentinel')
        port, env_key = ('26379', 'SENTINEL_PASSWORD') if node in SENTINELS else ('6379', 'REDIS_PASSWORD')
        shell = f'export REDISCLI_AUTH="${env_key}"; exec redis-cli -e --raw -h 127.0.0.1 -p {port} "$@"'
        return self.execute(node, ['sh', '-c', shell, 'redis-cli'] + args, capture=capture, check=check, timeout=timeout, tty=tty)

    def client(self, *args, capture=False, check=True, timeout=None):
        return self.execute('lab-client', ['python', '/app/client.py'] + list(args), capture=capture, check=check, timeout=timeout)

    def master(self):
        value = self.client('master', capture=True, timeout=15).stdout.strip()
        if value not in REDIS: raise RuntimeError('Cannot safely determine current master: ' + self.redact(value))
        return value

    def resolve(self, node: str):
        return self.master() if node == 'master' else node

    def state(self) -> dict:
        path = ROOT / '.lab' / 'faults.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def save_state(self, state: dict):
        path = ROOT / '.lab' / 'faults.json'; temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(state, indent=2) + '\n'); os.replace(temp, path)

    def failover_budget(self) -> int:
        return max(90, (int(self.env['DOWN_AFTER_MS']) + 2 * int(self.env['FAILOVER_TIMEOUT_MS'])) // 1000 + 30)

    def readiness_budget(self) -> int:
        return max(180, self.failover_budget())

    def up(self, build=True):
        self.doctor()
        if self.state(): raise RuntimeError('Active faults recorded. Inspect ./lab.sh faults and run ./lab.sh recover first.')
        if build: self.compose('build', 'redis-1', 'lab-client')
        self.check_env_state(save=True)
        self.compose('up', '-d', *BASE)
        self.client('wait', '--timeout', str(self.readiness_budget()))
        print('\nReady. Next: ./lab.sh demo ; ./lab.sh seed ; ./lab.sh status')

    def fault(self, action: str, node: str | None):
        if action in ('kill-master', 'pause-master'):
            node = self.master(); action = 'kill' if action == 'kill-master' else 'pause'
        elif node is None: raise ValueError(f'fault {action} requires a node')
        node = self.resolve(node)
        if node not in REDIS + SENTINELS: raise ValueError('Faults target only the six lab Redis/Sentinel nodes')
        data, state = self.inspect(node), self.state()
        if node in state: raise RuntimeError(f'{node} has a recorded fault; recover it first.')
        if not data['State'].get('Running') or data['State'].get('Paused'): raise RuntimeError(f'{node} is not running normally')
        if action == 'auth-replica':
            if node not in REDIS or not self.cli(node, ['ROLE']).stdout.startswith('slave\n'):
                raise RuntimeError('auth-replica is allowed only on a current replica')
        if action == 'isolate' and self.network not in data.get('NetworkSettings', {}).get('Networks', {}):
            raise RuntimeError('Node is not on the expected lab network')
        state[node] = {'action': action, 'time_utc': datetime.now(timezone.utc).isoformat()}
        self.save_state(state)  # Persist before change so interruption leaves a recovery route.
        print(f'Injecting {action} into {node}.', flush=True)
        try:
            if action == 'kill': self.run(['podman', 'kill', '--signal', 'KILL', self.cname(node)])
            elif action == 'stop': self.run(['podman', 'stop', '-t', '10', self.cname(node)])
            elif action == 'pause': self.run(['podman', 'pause', self.cname(node)])
            elif action == 'isolate': self.run(['podman', 'network', 'disconnect', self.network, self.cname(node)])
            elif action == 'auth-replica':
                self.cli(node, ['CONFIG', 'SET', 'masterauth', 'IntentionallyWrongLabPassword'])
                self.cli(node, ['CLIENT', 'KILL', 'TYPE', 'master'])
            else: raise ValueError('Unknown fault action')
        except Exception:
            print('Fault failed/interrupted; recovery record retained. Inspect then run ./lab.sh recover.', file=sys.stderr)
            raise
        print('Observe: ./lab.sh status ; ./lab.sh logs sentinel-1')
        print('Recovery: ./lab.sh recover ' + node)

    def connect_network(self, node: str):
        info = self.inspect(node)
        if self.network in info.get('NetworkSettings', {}).get('Networks', {}): return
        key = node.upper().replace('-', '_') + '_IP'
        self.run(['podman', 'network', 'connect', '--ip', self.env[key], '--alias', node, self.network, self.cname(node)])

    def recover(self, selected='all', wait=True):
        state = self.state()
        if selected != 'all' and selected not in REDIS + SENTINELS:
            raise ValueError('Use a concrete node name or all, not the dynamic name master.')
        for node in list(state) if selected == 'all' else [selected]:
            fault = state.get(node)
            if not fault: print(f'{node}: no recorded fault'); continue
            info = self.inspect(node)
            if info['State'].get('Paused'): self.run(['podman', 'unpause', self.cname(node)])
            if fault['action'] == 'isolate': self.connect_network(node)
            if not self.inspect(node)['State'].get('Running'): self.run(['podman', 'start', self.cname(node)])
            if fault['action'] == 'auth-replica':
                script = 'export REDISCLI_AUTH="$REDIS_PASSWORD"; redis-cli -e CONFIG SET masterauth "$REDIS_PASSWORD"; redis-cli -e CLIENT KILL TYPE master'
                self.execute(node, ['sh', '-eu', '-c', script])
            del state[node]; self.save_state(state)
            print(f'{node}: fault removed; Sentinel determines its role.')
        if wait and not state: self.client('wait', '--timeout', str(self.readiness_budget()))
        elif state: print('Other faults remain: ' + ', '.join(state))

    def sandbox_up(self):
        self.check_env_state()
        self.inspect('redis-sandbox', required=False)
        self.compose('--profile', 'sandbox', 'up', '-d', '--no-deps', 'redis-sandbox')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.cli('redis-sandbox', ['PING'], check=False)
            if result.returncode == 0 and result.stdout.strip() == 'PONG':
                print('Sandbox ready. Sentinel does NOT monitor this node.'); return
            time.sleep(1)
        raise RuntimeError('Sandbox startup failed; inspect ./lab.sh logs redis-sandbox')

    def sandbox_persistence_fault(self):
        self.sandbox_up()
        # Block only the sandbox RDB target with a directory. Never fill the host disk.
        script = '''
set -eu
export REDISCLI_AUTH="$REDIS_PASSWORD"
test ! -e /data/state/persistence-fault-active || { echo 'Fault already active'; exit 1; }
touch /data/state/persistence-fault-active
if [ -f /data/db/dump.rdb ]; then mv /data/db/dump.rdb /data/state/pre-fault.rdb; fi
mkdir -p /data/db/dump.rdb
redis-cli -e BGSAVE
'''
        self.execute('redis-sandbox', ['sh', '-c', script])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            info = self.cli('redis-sandbox', ['INFO', 'persistence']).stdout
            if 'rdb_bgsave_in_progress:0' in info and 'rdb_last_bgsave_status:err' in info:
                print(info)
                result = self.cli('redis-sandbox', ['SET', 'lab:sandbox:write-test', 'blocked'], check=False)
                text = result.stdout + result.stderr; print(self.redact(text))
                if 'MISCONF' not in text:
                    raise RuntimeError('RDB failed but MISCONF was not observed; inspect save/stop-writes settings.')
                print('Expected MISCONF observed. Recovery: ./lab.sh sandbox-recover'); return
            time.sleep(0.5)
        raise RuntimeError('Expected RDB failure not observed; run sandbox-recover and inspect logs.')

    def sandbox_recover(self):
        self.inspect('redis-sandbox')
        script = '''
set -eu
export REDISCLI_AUTH="$REDIS_PASSWORD"
if [ -f /data/state/persistence-fault-active ]; then
    if [ -d /data/db/dump.rdb ]; then rmdir /data/db/dump.rdb; fi
    if [ -f /data/state/pre-fault.rdb ]; then mv /data/state/pre-fault.rdb /data/db/dump.rdb; fi
    redis-cli -e BGSAVE
fi
'''
        self.execute('redis-sandbox', ['sh', '-c', script])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            info = self.cli('redis-sandbox', ['INFO', 'persistence']).stdout
            if 'rdb_bgsave_in_progress:0' in info and 'rdb_last_bgsave_status:ok' in info: break
            time.sleep(0.5)
        else: raise RuntimeError('Sandbox persistence did not recover; inspect logs.')
        self.client('sandbox-recover')
        self.cli('redis-sandbox', ['SET', 'lab:sandbox:recovered', 'yes'], capture=False)
        self.execute('redis-sandbox', ['rm', '-f', '/data/state/persistence-fault-active'])
        print('Sandbox recovered without disabling stop-writes-on-bgsave-error.')

    def backup(self):
        node = self.master()
        metadata = json.loads(self.client('marker', capture=True).stdout)
        if node != self.master(): raise RuntimeError('Master changed during backup preparation; retry after convergence.')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3)
        target = ROOT / 'output' / 'backups' / (stamp + '.rdb'); target.parent.mkdir(exist_ok=True)
        remote = '/data/state/backup-' + stamp + '.rdb'
        script = 'export REDISCLI_AUTH="$REDIS_PASSWORD"; exec redis-cli -e --rdb "$1"'
        self.execute(node, ['sh', '-eu', '-c', script, 'backup', remote], capture=False, timeout=180)
        self.run(['podman', 'cp', self.cname(node) + ':' + remote, target])
        self.execute(node, ['rm', '-f', remote])
        with target.open('rb') as stream:
            if stream.read(5) != b'REDIS': raise RuntimeError('Backup does not have an RDB header')
        metadata.update(source_node=node, filename=target.name, sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            note='RDB stream snapshot; marker checked after isolated restore, not a complete application-consistency proof.')
        target.with_suffix('.json').write_text(json.dumps(metadata, indent=2) + '\n')
        print('Backup:', target.relative_to(ROOT))
        print('Restore into sandbox only: ./lab.sh restore ' + str(target.relative_to(ROOT)) + ' --yes')

    def restore(self, filename: str, confirmed: bool):
        if not confirmed: raise ValueError('Restore replaces ONLY redis-sandbox data. Review the backup and re-run with --yes.')
        source = Path(filename).expanduser().resolve()
        if not source.is_file() or source.stat().st_size > 256 * 1024 * 1024:
            raise ValueError('Choose a local RDB file no larger than 256 MiB')
        with source.open('rb') as stream:
            if stream.read(5) != b'REDIS': raise ValueError('Not an RDB file')
        meta_path = source.with_suffix('.json')
        metadata = json.loads(meta_path.read_text()) if meta_path.exists() else None
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError('Backup metadata must be a JSON object')
        if metadata is not None and metadata.get('sha256') != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ValueError('Backup checksum mismatch; restore refused')
        if metadata is not None and (
            not isinstance(metadata, dict) or
            not isinstance(metadata.get('marker_key'), str) or
            not isinstance(metadata.get('marker_value'), str)
        ):
            raise ValueError('Backup metadata must contain string marker_key and marker_value fields')
        self.sandbox_up(); self.volume_owned('sandbox-data', required=True)
        staged = '/data/state/restore-candidate-' + secrets.token_hex(8) + '.rdb'
        try:
            self.run(['podman', 'cp', source, self.cname('redis-sandbox') + ':' + staged])
            self.execute('redis-sandbox', ['redis-check-rdb', staged], timeout=180)
        except BaseException:
            self.execute('redis-sandbox', ['rm', '-f', staged], check=False)
            raise
        # Only a structurally valid RDB may reach the destructive sandbox-only step.
        self.run(['podman', 'stop', '-t', '10', self.cname('redis-sandbox')])
        # One owned disposable volume; no host filesystem bind mount.
        script = '''
set -eu
rm -rf /restore/db
mkdir -p /restore/db
cp "/restore/state/$1" /restore/db/dump.rdb
rm -f "/restore/state/$1"
sed -i 's/^appendonly .*/appendonly no/' /restore/state/redis.conf
rm -f /restore/state/persistence-fault-active /restore/state/pre-fault.rdb
'''
        self.run(['podman', 'run', '--rm', '--network', 'none', '--label', LABEL + '=' + self.name,
            '-v', self.name + '-sandbox-data:/restore', '--entrypoint', 'sh',
            f'localhost/{self.name}-redis:1.1', '-c', script, 'restore', staged.rsplit('/', 1)[1]])
        self.run(['podman', 'start', self.cname('redis-sandbox')])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = self.cli('redis-sandbox', ['PING'], check=False)
            if result.returncode == 0 and result.stdout.strip() == 'PONG': break
            time.sleep(1)
        else: raise RuntimeError('Restored sandbox failed to start; original cluster is unchanged.')
        if metadata:
            value = self.cli('redis-sandbox', ['GET', metadata['marker_key']]).stdout.rstrip('\n')
            if value != metadata['marker_value']: raise RuntimeError('Restored backup marker mismatch')
            print('PASS: restored marker matches backup metadata')
        else: print('No metadata: RDB loading checked, but not a backup marker.')
        print('Restored DBSIZE:', self.cli('redis-sandbox', ['DBSIZE']).stdout.strip())
        self.cli('redis-sandbox', ['CONFIG', 'SET', 'appendonly', 'yes'])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            info = self.cli('redis-sandbox', ['INFO', 'persistence']).stdout
            if ('aof_rewrite_in_progress:0' in info and 'aof_rewrite_scheduled:0' in info and 'aof_last_bgrewrite_status:ok' in info): break
            time.sleep(1)
        else: raise RuntimeError('AOF regeneration not complete; inspect the sandbox before restarting it.')
        self.cli('redis-sandbox', ['CONFIG', 'REWRITE'])
        print('PASS: sandbox AOF regenerated and configuration persisted. Main Redis/Sentinel nodes were not overwritten.')

    def collect(self):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(2)
        out = ROOT / 'output' / ('diagnostics-' + stamp); out.mkdir()
        for node in ALL:
            data = self.inspect(node, required=False)
            if data is None: continue
            # Full inspect contains passwords; export only operational fields.
            minimal = {'Name': data.get('Name'), 'State': data.get('State'),
                'RestartCount': data.get('RestartCount'), 'NetworkSettings': data.get('NetworkSettings')}
            (out / (node + '-state.json')).write_text(self.redact(json.dumps(minimal, indent=2)))
            logs = self.run(['podman', 'logs', '--tail', '1000', self.cname(node)], check=False)
            (out / (node + '.log')).write_text(self.redact(logs.stdout + logs.stderr))
        result = self.client('status', '--json', capture=True, check=False, timeout=60)
        (out / 'topology.json').write_text(self.redact(result.stdout + result.stderr))
        (out / 'faults.json').write_text(json.dumps(self.state(), indent=2))
        print('Diagnostics:', out.relative_to(ROOT), '(passwords redacted; review before external sharing)')

    def results(self):
        self.inspect('lab-client')
        dest = ROOT / 'output' / ('results-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(2))
        dest.mkdir()
        self.run(['podman', 'cp', self.cname('lab-client') + ':/results/.', dest])
        print('Copied client results:', dest.relative_to(ROOT))

    def down(self, reset=False, confirmed=False):
        if reset and not confirmed:
            raise ValueError('reset removes ONLY this lab\'s eight volumes. Export backups/results, then use ./lab.sh reset --yes.')
        if not reset and self.state():
            self.recover(wait=False)
        for node in ALL:
            data = self.inspect(node, required=False)
            if not data: continue
            if data['State'].get('Paused'): self.run(['podman', 'unpause', self.cname(node)])
            if node in self.state() and self.state()[node]['action'] == 'isolate': self.connect_network(node)
        for suffix in VOLUMES: self.volume_owned(suffix)
        self.check_network()
        self.compose('--profile', 'sandbox', 'down', *(['-v'] if reset else []))
        self.save_state({})
        if reset:
            # Some providers skip unused profile volumes; remove only exact owned leftovers.
            for suffix in VOLUMES:
                if self.volume_owned(suffix): self.run(['podman', 'volume', 'rm', self.name + '-' + suffix])
            (ROOT / '.lab' / 'environment.sha256').unlink(missing_ok=True)
            print('Reset complete. .env and exported output/ preserved. Next: ./lab.sh up')
        else: print('Data/runtime configs preserved. Next: ./lab.sh up --no-build')

    def test_failover(self):
        report_path = ROOT / 'output' / 'failover-test.json'
        report_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        report = {'passed': False, 'status': 'RUNNING',
            'time_utc': datetime.now(timezone.utc).isoformat(),
            'before': None, 'after': None, 'observation_timeout_seconds': self.failover_budget(),
            'note': 'Observed baseline SET/WAIT 2/GET and recovery; not a zero-loss guarantee.'}
        def save():
            temp = report_path.with_suffix('.tmp')
            temp.write_text(json.dumps(report, indent=2) + '\n')
            os.replace(temp, report_path)
        save()  # A new failure must never leave a stale successful report.
        before = after = None
        fault_attempted = False
        original_error = cleanup_error = None
        try:
            if self.state(): raise RuntimeError('Recover recorded faults before the test.')
            self.client('wait', '--timeout', str(self.readiness_budget()))
            before = self.master(); report['before'] = before
            key = 'lab:test:failover:' + secrets.token_hex(8); value = secrets.token_hex(12)
            script = 'export REDISCLI_AUTH="$REDIS_PASSWORD"; printf "SET %s %s\\nWAIT 2 1000\\n" "$1" "$2" | redis-cli -e --raw'
            baseline = self.execute(before, ['sh', '-c', script, 'baseline', key, value]).stdout.strip().splitlines()
            if len(baseline) < 2 or baseline[-2:] != ['OK', '2']:
                raise RuntimeError('Baseline was not acknowledged by two replicas; no failure injected.')
            fault_attempted = True
            fault_start = time.monotonic()
            self.fault('kill', before)
            deadline = time.monotonic() + self.failover_budget()
            while time.monotonic() < deadline:
                try:
                    candidate = self.master()
                    if candidate != before and self.cli(candidate, ['GET', key]).stdout.strip() == value:
                        after = candidate; break
                except RuntimeError: pass
                time.sleep(1)
            if after is None:
                raise RuntimeError('Failover/baseline verification did not succeed within the observation window.')
            report.update(after=after, failover_observed_seconds=round(time.monotonic()-fault_start, 2))
        except BaseException as exc:
            original_error = exc
            report['error'] = self.redact(str(exc) or type(exc).__name__)
        finally:
            if fault_attempted:
                try:
                    self.recover(before, wait=True)
                    if after is not None and self.master() != after:
                        raise RuntimeError('Master changed unexpectedly during recovery')
                    report['recovery_passed'] = True
                except BaseException as exc:
                    cleanup_error = exc
                    report.update(recovery_passed=False, recovery_error=self.redact(str(exc) or type(exc).__name__))
            report['passed'] = original_error is None and cleanup_error is None
            report['status'] = 'PASS' if report['passed'] else 'FAIL'
            report['elapsed_seconds_including_recovery'] = round(time.monotonic() - start, 2)
            save()
        if original_error is not None:
            if cleanup_error is not None:
                raise RuntimeError(f"Test failed: {report['error']}; recovery also failed: {report['recovery_error']}") from original_error
            raise original_error
        if cleanup_error is not None: raise cleanup_error
        print(f'PASS: {before} -> {after}; baseline preserved; old primary returned as replica.')
        print('Report: output/failover-test.json')


def parser():
    p = argparse.ArgumentParser(description='Redis Sentinel Podman lab — Korean walkthroughs in README.md')
    sub = p.add_subparsers(dest='command')
    for command in ['help', 'init', 'doctor', 'config', 'build', 'master', 'demo', 'faults', 'sandbox-up', 'sandbox-oom', 'sandbox-persistence', 'sandbox-recover', 'backup', 'collect', 'results', 'test', 'down']:
        sub.add_parser(command)
    validation = sub.add_parser('validate'); validation.add_argument('--yes', action='store_true'); validation.add_argument('--no-build', action='store_true')
    up = sub.add_parser('up'); up.add_argument('--no-build', action='store_true')
    status = sub.add_parser('status'); status.add_argument('--json', action='store_true')
    wait = sub.add_parser('wait'); wait.add_argument('--timeout', type=int, default=180)
    seed = sub.add_parser('seed'); seed.add_argument('--users', type=int, default=2000); seed.add_argument('--payload-bytes', type=int, default=256)
    work = sub.add_parser('workload'); work.add_argument('--seconds', type=int, default=120); work.add_argument('--rate', type=float, default=5)
    work.add_argument('--size', type=int, default=64); work.add_argument('--mode', choices=['sentinel', 'fixed'], default='sentinel')
    work.add_argument('--fixed-node', choices=REDIS, default='redis-1'); work.add_argument('--wait-replicas', type=int, choices=[0, 1, 2], default=0)
    work.add_argument('--run-id')
    verify = sub.add_parser('verify'); verify.add_argument('run_id', nargs='?', default='latest')
    cli = sub.add_parser('cli'); cli.add_argument('node'); cli.add_argument('redis_args', nargs=argparse.REMAINDER)
    logs = sub.add_parser('logs'); logs.add_argument('node'); logs.add_argument('--follow', action='store_true'); logs.add_argument('--tail', type=int, default=100)
    fault = sub.add_parser('fault'); fault.add_argument('action', choices=['kill-master', 'pause-master', 'stop', 'kill', 'pause', 'isolate', 'auth-replica']); fault.add_argument('node', nargs='?')
    recover = sub.add_parser('recover'); recover.add_argument('node', nargs='?', default='all')
    start = sub.add_parser('start'); start.add_argument('node', choices=ALL)
    restore = sub.add_parser('restore'); restore.add_argument('file'); restore.add_argument('--yes', action='store_true')
    reset = sub.add_parser('reset'); reset.add_argument('--yes', action='store_true')
    return p


def main(argv=None):
    p = parser(); args = p.parse_args(argv)
    if args.command in (None, 'help'):
        p.print_help(); print('\nQuick start: ./lab.sh up ; ./lab.sh demo ; ./lab.sh seed ; ./lab.sh test'); return 0
    lab = Lab(load_env())
    if args.command == 'init': print('Environment ready. Review .env, then ./lab.sh doctor')
    elif args.command == 'doctor': lab.doctor()
    elif args.command == 'config': print(lab.redact(lab.compose('config', capture=True).stdout))
    elif args.command == 'build': lab.doctor(); lab.compose('build', 'redis-1', 'lab-client')
    elif args.command == 'up': lab.up(build=not args.no_build)
    elif args.command == 'status': lab.client('status', *(['--json'] if args.json else []))
    elif args.command == 'wait': lab.client('wait', '--timeout', str(args.timeout))
    elif args.command == 'master': print(lab.master())
    elif args.command == 'demo': lab.client('basic-demo')
    elif args.command == 'seed': lab.client('seed', '--users', str(args.users), '--payload-bytes', str(args.payload_bytes))
    elif args.command == 'workload':
        lab.client('workload', '--seconds', str(args.seconds), '--rate', str(args.rate), '--size', str(args.size),
                   '--mode', args.mode, '--fixed-node', args.fixed_node, '--wait-replicas', str(args.wait_replicas),
                   *(['--run-id', args.run_id] if args.run_id else []))
    elif args.command == 'verify': lab.client('verify', args.run_id)
    elif args.command == 'cli': lab.cli(lab.resolve(args.node), args.redis_args, capture=False, timeout=None, tty=not args.redis_args and sys.stdin.isatty())
    elif args.command == 'logs':
        node = lab.resolve(args.node); lab.inspect(node)
        lab.run(['podman', 'logs', '--tail', str(args.tail)] + (['--follow'] if args.follow else []) + [lab.cname(node)], capture=False, timeout=None)
    elif args.command == 'faults': print(json.dumps(lab.state(), indent=2))
    elif args.command == 'fault': lab.fault(args.action, args.node)
    elif args.command == 'recover': lab.recover(args.node)
    elif args.command == 'start':
        if args.node in lab.state(): lab.recover(args.node)
        else: lab.inspect(args.node); lab.run(['podman', 'start', lab.cname(args.node)], capture=False)
    elif args.command == 'sandbox-up': lab.sandbox_up()
    elif args.command == 'sandbox-oom': lab.sandbox_up(); lab.client('sandbox-oom')
    elif args.command == 'sandbox-persistence': lab.sandbox_persistence_fault()
    elif args.command == 'sandbox-recover': lab.sandbox_recover()
    elif args.command == 'backup': lab.backup()
    elif args.command == 'restore': lab.restore(args.file, args.yes)
    elif args.command == 'collect': lab.collect()
    elif args.command == 'results': lab.results()
    elif args.command == 'test': lab.test_failover()
    elif args.command == 'validate':
        from live_validate import validate
        return validate(lab, confirmed=args.yes, no_build=args.no_build)
    elif args.command == 'down': lab.down()
    elif args.command == 'reset': lab.down(reset=True, confirmed=args.yes)
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nInterrupted. Remove any recorded fault using ./lab.sh recover.', file=sys.stderr); raise SystemExit(130)
    except Exception as exc:
        message = str(exc)
        try:
            for key in ('REDIS_PASSWORD', 'SENTINEL_PASSWORD'):
                value = parse_env(ROOT / '.env').get(key)
                if value: message = message.replace(value, '<redacted>')
        except Exception: pass
        print('ERROR: ' + message, file=sys.stderr); raise SystemExit(1)
