"""Disposable DB recovery/resource drills. Original named volumes are NEVER mounted or deleted."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import time
import uuid
from .sim_engine import DockerLab
from .simulation import HTTP
from mvp_app.snapshot import validate as validate_snapshot, MAX_BYTES
from mvp_app.transaction_model import SCENARIOS as TRANSACTIONS, options as tx_options, plan as tx_plan

DRILLS = {
    'backup-restore': '두 InnoDB 업무 테이블을 일관된 논리 스냅샷으로 읽고 별도 MariaDB에 복원·전체 행 대조',
    'upgrade-restore': '이미 내려받은 다른 MariaDB 이미지에 같은 논리 스냅샷 복원; 원본 볼륨 업그레이드 아님',
    'redis-disk-full': '폐기용 Redis의 16MiB tmpfs AOF 경로 고갈; 호스트 디스크를 채우지 않음',
    'redis-oom': '폐기용 Redis의 64MiB cgroup 초과; OOMKilled 확인, 원본 Redis 설정 불변',
    'deadlock': '이번 실행의 합성 주문 두 행에 역순 잠금; 1213 희생 트랜잭션과 rollback 관찰',
    **TRANSACTIONS,
}
RUN_RE = r'drill-[a-f0-9]{12}'
IMAGE_RE = r'(?:(?:docker\.io/)?library/)?mariadb:(?:[0-9]+\.[0-9]+(?:\.[0-9]+)?)(?:-[a-z0-9.-]+)?(?:@sha256:[a-f0-9]{64})?'


def candidate(value):
    if not value or not re.fullmatch(IMAGE_RE, value):
        raise ValueError('Use an explicitly versioned official MariaDB image, already pulled locally; no latest/arbitrary repository')
    return value


class Disposable:
    def __init__(self, lab, manage):
        self.lab, self.m = lab, manage
        self.path = manage.ROOT / '.state/drill-active.json'
        self.record = None
        self.stage = "preflight"
        self.details = {}

    def call(self, *args, timeout=45, data=None, check=True):
        self.lab.binding()
        value = subprocess.run([*self.lab.c.engine, *args], env=self.lab.c.inherited,
                               input=data, capture_output=True, text=True, timeout=timeout)
        if check and value.returncode:
            # Docker stderr/command might reveal scratch credentials. Do not repeat either.
            raise RuntimeError('Disposable Docker operation failed (' + str(value.returncode) + '); report/recovery marker retained')
        return value

    def begin(self, run_id, kind):
        if self.path.exists() or self.lab.active_path.exists():
            raise RuntimeError('An unfinished drill exists; recover it first')
        if not re.fullmatch(RUN_RE, run_id) or kind not in DRILLS:
            raise ValueError('Invalid drill record')
        self.record = {'schema': 1, 'engine': self.lab.binding(), 'project': self.lab.c.config['MVP_PROJECT'],
                       'run_id': run_id, 'kind': kind, 'resources': []}
        self.save()

    def save(self):
        self.m.atomic_json(self.path, self.record)

    def resource_limits(self):
        info = json.loads(self.call('info', '--format', '{{json .}}').stdout)
        if info.get('OSType') != 'linux' or not info.get('MemoryLimit') or not info.get('SwapLimit'):
            raise RuntimeError('Linux memory AND swap limits must be available; no unconstrained resource experiment')
        if info.get('MemTotal', 0) < 6 * 1024**3:
            raise RuntimeError('Docker reports less than 6GiB total RAM; disposable drill refused (free headroom still requires checking)')

    def image_id(self, value):
        rows = json.loads(self.call('image', 'inspect', value).stdout)
        iid = rows[0]['Id']
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', iid):
            raise RuntimeError('Image ID not available; no automatic image pull')
        return iid

    def create(self, role, image, *, network='none', tmpfs=None, memory=256, environment=None, command=(), entrypoint=None):
        if role not in {'database', 'client'} or any(r['role'] == role for r in self.record['resources']):
            raise ValueError('At most one database and one client per disposable drill')
        if network != 'none' and network not in {'container:' + r.get('id', '') for r in self.record['resources'] if r['role'] == 'database'}:
            raise ValueError('Disposable client can only join its own disposable database')
        iid = self.image_id(image)
        name = self.record['project'] + '-' + self.record['run_id'] + '-' + role
        spec = {'role': role, 'name': name, 'image_id': iid, 'network': network,
                'tmpfs': sorted((tmpfs or {}).keys()), 'memory_bytes': memory * 1024**2}
        self.stage = 'create-' + role
        self.record['resources'].append(spec)
        self.save()  # intention exists before create, including a disconnected create CLI
        opts = ['create', '--pull', 'never', '--name', name, '--network', network,
                '--restart', 'no', '--memory', str(memory) + 'm', '--memory-swap', str(memory) + 'm',
                '--pids-limit', '96', '--cpus', '1', '--security-opt', 'no-new-privileges:true',
                '--log-opt', 'max-size=1m', '--log-opt', 'max-file=1',
                '--label', 'io.db-lab.project=' + self.record['project'],
                '--label', 'io.db-lab.run=' + self.record['run_id'], '--label', 'io.db-lab.kind=disposable',
                '--label', 'io.db-lab.role=' + role]
        if role == 'client':
            opts += ['-i', '--read-only', '--cap-drop', 'ALL']
        for path, size in (tmpfs or {}).items():
            if path not in {'/data', '/var/lib/mysql', '/tmp'} or not 1 <= size <= 512:
                raise ValueError('Unexpected/big temporary filesystem')
            opts += ['--tmpfs', path + ':rw,nosuid,nodev,noexec,size=' + str(size) + 'm,mode=1777']
        envfile = self.m.ROOT / '.state' / (self.record['run_id'] + '.env')
        try:
            if environment:
                fd = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    for key, value in environment.items():
                        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or not re.fullmatch(r'[A-Za-z0-9_.-]+', str(value)):
                            raise ValueError('Nonliteral disposable environment rejected')
                        stream.write(key + '=' + str(value) + '\n')
                opts += ['--env-file', str(envfile)]
            if entrypoint:
                opts += ['--entrypoint', entrypoint]
            spec['id'] = self.call(*opts, iid, *command).stdout.strip()
            self.save()
            row = self.inspect_owned(spec)
            if not row:
                raise RuntimeError('Created disposable container disappeared')
            return row['Id']
        finally:
            if envfile.exists():
                envfile.unlink()

    def inspect_owned(self, spec):
        role = spec.get('role')
        if role not in {'database', 'client'} or spec.get('name') != self.record['project'] + '-' + self.record['run_id'] + '-' + role:
            raise RuntimeError('Invalid disposable resource specification')
        ids = self.call('ps', '-a', '-q', '--filter', 'name=^/' + spec['name'] + '$').stdout.split()
        if not ids:
            return None
        if len(ids) != 1:
            raise RuntimeError('Ambiguous disposable container')
        row = json.loads(self.call('inspect', ids[0]).stdout)[0]
        labels = row['Config'].get('Labels', {})
        host = row['HostConfig']
        if (labels.get('io.db-lab.project') != self.record['project']
                or labels.get('io.db-lab.run') != self.record['run_id']
                or labels.get('io.db-lab.kind') != 'disposable' or labels.get('io.db-lab.role') != role
                or row['Image'] != spec['image_id'] or host.get('NetworkMode') != spec['network']
                or set(host.get('Tmpfs', {})) != set(spec['tmpfs'])
                or host.get('Privileged') or (spec.get('id') and row['Id'] != spec['id'])
                or host.get('Memory') != spec['memory_bytes'] or host.get('MemorySwap') != spec['memory_bytes']
                or any(m['Type'] != 'tmpfs' or m['Destination'] not in spec['tmpfs'] for m in row.get('Mounts', []))):
            raise RuntimeError('Disposable ownership/limits/network/storage mismatch; no removal performed')
        return row

    def cleanup(self):
        if not self.path.exists():
            return {'cleaned': True, 'resources': 0}
        self.record = json.loads(self.path.read_text())
        if (self.record.get('schema') != 1 or not re.fullmatch(RUN_RE, self.record.get('run_id', ''))
                or self.record.get('kind') not in DRILLS or self.record.get('project') != self.lab.c.config['MVP_PROJECT']
                or self.record.get('engine') != self.lab.binding()):
            raise RuntimeError('Invalid/foreign recovery record; no removal performed')
        resources = self.record.get('resources')
        if not isinstance(resources, list) or len(resources) > 2:
            raise RuntimeError('Invalid disposable resource list')
        # Prove every resource BEFORE deleting any of them.
        for spec in resources:
            self.inspect_owned(spec)
        count = 0
        for spec in reversed(resources):
            row = self.inspect_owned(spec)
            if row:
                if row['State']['Running']:
                    self.call('stop', '--time', '5', row['Id'])
                self.inspect_owned(spec)
                self.call('rm', row['Id'])  # no -v, -f, volume/prune, source-volume operations
                count += 1
        envfile = self.m.ROOT / '.state' / (self.record['run_id'] + '.env')
        if envfile.exists():
            envfile.unlink()
        self.path.unlink()
        return {'cleaned': True, 'resources': count, 'scope': 'only labelled disposable containers/tmpfs; source volumes untouched'}

    def wait_db(self, cid, redis=False):
        command = (['sh', '-ec', 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping'] if redis
                   else ['healthcheck.sh', '--connect', '--innodb_initialized'])
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            self.inspect_owned(self.record['resources'][0])
            result = self.call('exec', cid, *command, check=False, timeout=8)
            if result.returncode == 0 and (not redis or result.stdout.strip() == 'PONG'):
                return
            time.sleep(1)
        raise RuntimeError('Disposable DB initialization deadline exceeded')

    def client(self, image, cid, module, args, env, data=None, accepted_codes=(0,)):
        client = self.create('client', image, network='container:' + cid, memory=256,
                             tmpfs={'/tmp': 4}, environment=env, entrypoint='python',
                             command=['-m', module, *args])
        self.stage = 'execute-disposable-client'
        result = self.call('start', '-a', '-i', client, timeout=100, data=data, check=False)
        if len(result.stdout.encode()) > 8 * 1024 * 1024:
            raise RuntimeError('Disposable helper output exceeds the 8MiB evidence budget')
        try:
            parsed = json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError('Client returned no structured evidence') from exc
        if not isinstance(parsed, dict):
            raise RuntimeError('Disposable helper must return an evidence object')
        self.details['client'] = parsed
        if result.returncode not in accepted_codes:
            raise RuntimeError('Disposable client reported failure; inspect partial_observation, not a successful restore')
        if module == 'mvp_app.transaction_drill' and result.returncode != {'passed': 0, 'failed': 1, 'inconclusive': 2}.get(parsed.get('status')):
            raise RuntimeError('Transaction helper status and exit code disagree')
        return parsed

    def snapshot(self, states, directory, candidate_image=None, snapshot_path=None):
        source = next(s for s in states if s['service'] == 'mariadb')
        api = next(s for s in states if s['service'] == 'api')
        self.lab.inspect(api['id'], 'api')
        self.stage = 'read-application-snapshot'
        if snapshot_path:
            with Path(snapshot_path).open('rb') as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError('Saved snapshot exceeds the 16MiB input limit')
            raw = data.decode('utf-8')
        else:
            raw = self.lab.docker('exec', api['id'], 'python', '-m', 'mvp_app.snapshot', 'export', timeout=45)
        snapshot = validate_snapshot(json.loads(raw))
        # This private artifact includes synthetic business data and idempotency keys.
        self.m.atomic_json(directory / 'snapshot.json', snapshot)
        image = self.image_id(candidate(candidate_image)) if candidate_image else source['image_id']
        if candidate_image and image == source['image_id']:
            raise ValueError('Candidate equals current image ID; not a version compatibility trial')
        password = secrets.token_hex(24)
        cid = self.create('database', image, memory=768, tmpfs={'/var/lib/mysql': 512},
                         environment={'MARIADB_ROOT_PASSWORD': secrets.token_hex(24), 'MARIADB_DATABASE': 'mvp',
                                      'MARIADB_USER': 'mvp', 'MARIADB_PASSWORD': password},
                         command=['--innodb-buffer-pool-size=64M', '--innodb-log-file-size=32M'])
        self.call('start', cid)
        self.wait_db(cid)
        result = self.client(api['image_id'], cid, 'mvp_app.snapshot', ['restore'],
                             {'MVP_DISPOSABLE': self.record['run_id'], 'SQL_HOST': '127.0.0.1',
                              'SQL_DATABASE': 'mvp', 'SQL_USER': 'mvp', 'SQL_PASSWORD': password}, raw)
        result.update({'source_image_id': source['image_id'], 'target_image_id': image,
                       'outbox_replay': False, 'source_overwritten': False, 'restored_saved_snapshot': bool(snapshot_path)})
        return result

    def transaction_trial(self, states, directory, kind, clients=4, seed=42):
        """No original SQL connection or source-data copy; a fresh txlab on tmpfs only."""
        tx_options(kind, clients, seed)
        source = next(s for s in states if s['service'] == 'mariadb')
        api = next(s for s in states if s['service'] == 'api')
        self.lab.inspect(api['id'], 'api')
        password = secrets.token_hex(24)
        cid = self.create('database', source['image_id'], memory=768, tmpfs={'/var/lib/mysql': 512},
                         environment={'MARIADB_ROOT_PASSWORD': secrets.token_hex(24), 'MARIADB_DATABASE': 'txlab',
                                      'MARIADB_USER': 'txlab', 'MARIADB_PASSWORD': password},
                         command=['--innodb-buffer-pool-size=64M', '--innodb-log-file-size=32M'])
        self.call('start', cid)
        self.wait_db(cid)
        result = self.client(api['image_id'], cid, 'mvp_app.transaction_drill',
                             [kind, '--clients', str(clients), '--seed', str(seed)],
                             {'MVP_DISPOSABLE': self.record['run_id'], 'SQL_HOST': '127.0.0.1',
                              'SQL_DATABASE': 'txlab', 'SQL_USER': 'txlab', 'SQL_PASSWORD': password},
                             accepted_codes=(0, 1, 2))
        if result.get('status') != 'failed' or 'cases' in result:
            from mvp_app.transaction_model import verify_result
            verify_result(result, kind, clients, seed)
        elif result.get('observed') is not False or result.get('scenario') != kind:
            raise RuntimeError('Malformed failed transaction evidence')
        result.update({'source_image_id': source['image_id'], 'client_image_id': api['image_id'],
                       'source_database_modified': False, 'temporary_database': 'txlab'})
        self.m.atomic_json(directory / 'transaction.json', result)
        if 'cases' in result:
            from mvp_app.transaction_drill import report
            fd = os.open(directory / 'report.md', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(report(result))
        fd = os.open(directory / 'trace.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            for event in result.get('trace', []):
                stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        self.details['transaction'] = result
        return result

    def pressure(self, states, kind):
        source = next(s for s in states if s['service'] == 'redis')
        api = next(s for s in states if s['service'] == 'api')
        password = secrets.token_hex(24)
        disk = kind == 'redis-disk-full'
        mode = 'disk-full' if disk else 'oom'
        command = ('exec redis-server --requirepass "$REDIS_PASSWORD" --save "" --maxmemory 0 '
                   + ('--appendonly yes --appendfsync always --auto-aof-rewrite-percentage 0' if disk else '--appendonly no'))
        cid = self.create('database', source['image_id'], memory=128 if disk else 64,
                         tmpfs={'/data': 16 if disk else 4}, environment={'REDIS_PASSWORD': password},
                         command=['sh', '-ec', command])
        self.call('start', cid)
        self.wait_db(cid, redis=True)
        before = self.inspect_owned(self.record['resources'][0])
        result = self.client(api['image_id'], cid, 'mvp_app.redis_pressure', [mode],
                             {'MVP_DISPOSABLE': self.record['run_id'], 'REDIS_HOST': '127.0.0.1', 'REDIS_PASSWORD': password})
        time.sleep(1)
        after = self.inspect_owned(self.record['resources'][0])
        state = after['State']
        result.update({'server_state': {k: state.get(k) for k in ('Running', 'ExitCode', 'OOMKilled')},
                       'source_image_id': source['image_id'], 'memory_bytes': before['HostConfig']['Memory']})
        if disk:
            # Docker logs can put Redis stderr into stderr instead of stdout.
            log_result = self.call('logs', '--tail', '60', cid)
            logs = log_result.stdout + log_result.stderr
            result['enospc_in_server_log'] = 'No space left on device' in logs
            result['observed'] = result.get('aof_last_write_status') == 'err' and result['enospc_in_server_log'] and not state.get('OOMKilled')
            result['scope'] = 'AOF persistence failure in 16MiB disposable tmpfs; not physical disk hardware failure'
        else:
            result['observed'] = state.get('OOMKilled') is True and state.get('ExitCode') == 137 and not state.get('Running')
            result['scope'] = 'Redis process exceeded disposable 64MiB cgroup; not Redis maxmemory eviction or original server failover'
        return result

    def deadlock(self, states):
        run = self.record['run_id'].replace('drill-', 'sim-')
        http = HTTP('http://127.0.0.1:' + self.lab.c.config['API_PORT'], run)
        ids = []
        before = []
        for i in range(2):
            body = {'item': run + '-deadlock-' + str(i), 'quantity': 1, 'unit_price': 7}
            response = http.call('POST', '/api/orders', body, key=run + '-deadlock-' + str(i))
            if response.status not in (200, 201):
                raise RuntimeError('Synthetic deadlock fixture creation failed')
            order = response.payload['order']
            ids.append(order['id']); before.append(order)
        api = next(s for s in states if s['service'] == 'api')
        self.lab.inspect(api['id'], 'api')
        result = self.call('exec', api['id'], 'python', '-m', 'mvp_app.deadlock_drill',
                           '--run-id', run, '--orders', *ids, timeout=20, check=False)
        observed = json.loads(result.stdout)
        self.details['deadlock_helper'] = observed
        if result.returncode not in (0, 2):
            raise RuntimeError('Deadlock helper failed; not an observed InnoDB deadlock')
        after = [http.call('GET', '/api/study/key/' + run + '-deadlock-' + str(i)) for i in range(2)]
        observed['orders_unchanged'] = all(r.status == 200 and r.payload.get('order') == initial for r, initial in zip(after, before))
        observed['order_ids'] = ids
        observed['observed'] = observed.get('observed_deadlock') is True and observed['orders_unchanged']
        return observed


def add_parser(sub):
    p = sub.add_parser('drills', help='Optional disposable DB recovery/resource drills; normal stack remains unchanged')
    actions = p.add_subparsers(dest='drill_action', required=True)
    actions.add_parser('list')
    plan = actions.add_parser('plan')
    plan.add_argument('kind', choices=DRILLS)
    plan.add_argument('--clients', type=int)
    plan.add_argument('--seed', type=int)
    for name in ('prepare', 'recover'):
        q = actions.add_parser(name)
        q.add_argument('--yes', action='store_true', required=True)
    run = actions.add_parser('run')
    run.add_argument('kind', choices=DRILLS)
    run.add_argument('--clients', type=int, help='Transaction lessons only: 2..8, default 4')
    run.add_argument('--seed', type=int, help='Transaction lessons only: fixture price seed, default 42')
    run.add_argument('--candidate-image')
    run.add_argument('--snapshot', help='Previously captured application snapshot; restore only into a fresh disposable DB')
    run.add_argument('--yes', action='store_true', required=True)


def cli(args, manage):
    if args.drill_action == 'list':
        print(json.dumps(DRILLS, ensure_ascii=False, indent=2))
        return 0
    if args.drill_action in {'plan', 'run'}:
        if args.kind in TRANSACTIONS:
            args.clients = 4 if args.clients is None else args.clients
            args.seed = 42 if args.seed is None else args.seed
            tx_options(args.kind, args.clients, args.seed)
        elif args.clients is not None or args.seed is not None:
            raise ValueError('--clients/--seed apply only to transaction lessons')
    if args.drill_action == 'plan':
        value = tx_plan(args.kind, args.clients, args.seed) if args.kind in TRANSACTIONS else {
            'kind': args.kind, 'goal': DRILLS[args.kind], 'reference': 'docs/ADVANCED-DRILLS.md',
            'runtime_executed': False}
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    with manage.lock():
        config = manage.validate(manage.parse_env(manage.ROOT / '.env'))
        lab = DockerLab(manage.Compose(config), manage)
        store = Disposable(lab, manage)
        if args.drill_action == 'recover':
            print(json.dumps(store.cleanup(), ensure_ascii=False, indent=2))
            return 0
        lab.binding(); lab.c.guard_identity()
        if args.drill_action == 'prepare':
            manage.require_no_active_fault()
            if store.path.exists() or lab.active_path.exists():
                raise RuntimeError('Recover an unfinished drill before building its helper')
            tag = config['MVP_PROJECT'] + '-drills:local'
            result = store.call('build', '-f', str(manage.ROOT / 'Containerfile.drills'), '-t', tag, str(manage.ROOT), timeout=600, check=False)
            if result.returncode:
                raise RuntimeError('Optional helper build failed; requires package/image network access')
            print(json.dumps({'helper_image': store.image_id(tag), 'normal_stack_changed': False}))
            return 0
        if args.kind == 'upgrade-restore':
            candidate(args.candidate_image)
        elif args.candidate_image:
            raise ValueError('--candidate-image is only valid for upgrade-restore')
        if args.snapshot and args.kind not in {'backup-restore', 'upgrade-restore'}:
            raise ValueError('--snapshot only applies to isolated restore trials')
        # Reserve output before preflight so environmental failures also leave honest evidence.
        run = 'drill-' + uuid.uuid4().hex[:12]
        directory = manage.ROOT / 'reports/drills' / run
        directory.mkdir(parents=True, mode=0o700)
        summary = {'schema': 1, 'run_id': run, 'kind': args.kind, 'status': 'failed',
                   'evidence_kind': 'LIVE-LOCAL-DOCKER-DRILL', 'cleanup': False,
                   'source_volumes_mounted_in_clones': False}
        from datetime import datetime, timezone
        summary['started_at_utc'] = datetime.now(timezone.utc).isoformat()
        sources = [p for top in ('mvp_app', 'tools') for p in (manage.ROOT / top).rglob('*.py')]
        manage.atomic_json(directory / 'source-manifest.json', {str(p.relative_to(manage.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)})
        started = time.monotonic()
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(*_):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, terminate)
        try:
            states = lab.preflight()
            api_state = next(s for s in states if s['service'] == 'api')
            raw_api = json.loads(lab.docker('inspect', api_state['id']))[0]
            app_env = dict(v.split('=', 1) for v in raw_api['Config'].get('Env', []) if '=' in v)
            expected = {'SQL_HOST': 'mariadb', 'SQL_DATABASE': 'mvp', 'SQL_USER': 'mvp', 'STUDY_PROJECT': config['MVP_PROJECT']}
            if any(app_env.get(k) != v for k, v in expected.items()):
                raise RuntimeError('Live API target differs from isolated MVP configuration')
            manage.atomic_json(directory / 'source-containers.json', states)
            store.resource_limits()
            store.begin(run, args.kind)
            if args.kind in {'backup-restore', 'upgrade-restore'}:
                result = store.snapshot(states, directory, args.candidate_image, args.snapshot)
                okay = result.get('matched') is True and result.get('canary_passed') is True
            elif args.kind in TRANSACTIONS:
                result = store.transaction_trial(states, directory, args.kind, args.clients, args.seed)
                okay = result.get('observed') is True
            elif args.kind == 'deadlock':
                result = store.deadlock(states)
                if result.get('orders_unchanged') is False:
                    store.details['deadlock'] = result
                    raise RuntimeError('Synthetic fixture changed during read-only lock trial')
                okay = result.get('observed') is True
            else:
                result = store.pressure(states, args.kind)
                okay = result.get('observed') is True
            summary['observation'] = result
            summary['status'] = result['status'] if args.kind in TRANSACTIONS else ('passed' if okay else 'inconclusive')
        except KeyboardInterrupt:
            summary['status'] = 'aborted'
        except Exception as exc:
            summary['error'] = type(exc).__name__
            summary['failed_stage'] = store.stage
            summary['partial_observation'] = store.details
            summary['reason'] = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'See local prerequisites; raw driver/command output redacted'
        finally:
            try:
                summary['cleanup_result'] = store.cleanup() if store.record is not None else {'cleaned': True, 'resources': 0, 'reason': 'this run created nothing'}
                summary['cleanup'] = True
            except Exception as exc:
                summary['cleanup_error'] = type(exc).__name__
                summary['status'] = 'failed'
                summary['recover_command'] = 'bash ./all.sh mvp drills recover --yes'
            signal.signal(signal.SIGTERM, previous)
            summary['elapsed_seconds'] = round(time.monotonic() - started, 3)
            manage.atomic_json(directory / 'summary.json', summary)
        print(json.dumps({**summary, 'report_directory': str(directory)}, ensure_ascii=False, indent=2))
        return {'passed': 0, 'failed': 1, 'inconclusive': 2, 'aborted': 130}[summary['status']]
