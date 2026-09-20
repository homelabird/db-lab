"""One fail-closed live acceptance entrypoint. Never builds/starts/resets the stack.

Each run gets new evidence; no previous PASS is reused. A separate suite lock avoids
concurrent suite runners; existing controllers keep their own per-operation lock.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from .sim_engine import DockerLab
from .simulation import SCENARIOS as SIMULATIONS
from .advanced import DRILLS, candidate
from mvp_app.message_safety import SCENARIOS as MESSAGES
from mvp_app.transaction_model import SCENARIOS as TRANSACTIONS
from mvp_app.runtime_contract import source_manifest, target_manifest, Settings

RUN_RE = r'accept-[a-f0-9]{12}'
SUITES = ('core', 'simulations', 'transactions', 'messages', 'network', 'resources', 'recovery', 'all', 'candidate')
CODES = {'passed': 0, 'failed': 1, 'blocked': 1, 'inconclusive': 2, 'aborted': 130}

@dataclass(frozen=True)
class Step:
    name: str
    command: tuple[str, ...]
    family: str
    scenario: str | None = None

    def public(self):
        return {'name': self.name, 'command': list(self.command), 'family': self.family, 'scenario': self.scenario}


def steps_for(suite: str, image: str | None = None) -> list[Step]:
    if suite not in SUITES:
        raise ValueError('Unknown acceptance suite')
    if suite == 'candidate':
        candidate(image)
    elif image is not None:
        raise ValueError('--candidate-image applies only to the candidate suite')
    steps = [Step('smoke', ('smoke',), 'smoke')]
    sims = list(SIMULATIONS) if suite in {'all', 'simulations'} else (['db-network-delay', 'db-network-loss'] if suite == 'network' else ['baseline'])
    for name in sims:
        steps.append(Step('simulate-' + name, ('simulate', 'run', name, '--seed', '42', '--workload', 'write-heavy', '--yes'), 'simulate', name))
    if suite in {'all', 'transactions'}:
        names = list(TRANSACTIONS)
    else:
        names = []
    if suite in {'all', 'resources'}:
        names += ['redis-disk-full', 'redis-oom']
    if suite in {'all', 'recovery'}:
        names += ['backup-restore', 'deadlock']
    if suite == 'candidate':
        names += ['upgrade-restore']
    for name in names:
        argv = ('drills', 'run', name, '--yes')
        if name == 'upgrade-restore':
            argv += ('--candidate-image', str(image))
        steps.append(Step('drills-' + name, argv, 'drills', name))
    if suite in {'all', 'messages'}:
        for name in MESSAGES:
            steps.append(Step('messages-' + name, ('messages', 'run', name, '--yes'), 'messages', name))
    return steps


def plan(suite: str, image: str | None = None) -> dict:
    steps = steps_for(suite, image)
    return {'schema': 1, 'suite': suite, 'runtime_executed': False,
            'prerequisites': ['pinned local Linux Docker + Compose v2', 'six ready containers',
                              'API AND worker built from this source and exact direct dependency pins',
                              'no pending recovery marker; normal mode'],
            'read_only_checks': ['container-targets', 'api-runtime-contract', 'worker-runtime-contract'],
            'steps': [s.public() for s in steps],
            'selected_scenarios': len([s for s in steps if s.scenario]),
            'excluded': ['upgrade-restore requires an explicitly chosen candidate image; use candidate suite'] if suite == 'all' else [],
            'writes': 'synthetic orders retained; selected drills may stop/pause containers or create disposable resources',
            'no_automatic': ['up', 'pull', 'build', 'reset', 'volume deletion', 'repair', 'resume previous PASS'],
            'network_helper': 'run drills prepare --yes separately before network/simulations/all',
            'coverage_limit': 'one local stack, not multi-host HA/performance/durability certification'}


@contextmanager
def suite_lock(root: Path):
    import fcntl
    state = root / '.state'
    state.mkdir(exist_ok=True, mode=0o700)
    fd = os.open(state / 'acceptance.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another acceptance suite is active') from exc
        yield


def execute(argv, *, cwd: Path, env: dict, timeout: float, data: str | None = None) -> dict:
    """Bound execution, private temporary output, signal only this child's process group.

    A timeout remains a failure even when the child completes during its cleanup grace.
    docker exec may outlive its CLI; no claim of a hard server-side lease is made.
    """
    started = time.monotonic()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                 stdout=output, stderr=errors, start_new_session=True)
        timed_out = interrupted = False
        try:
            child.communicate(None if data is None else data.encode(), timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            timed_out = isinstance(exc, subprocess.TimeoutExpired)
            interrupted = not timed_out
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.communicate(timeout=25)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.communicate(timeout=5)
        output.seek(0); raw = output.read(16 * 1024**2 + 1)
        errors.seek(0, 2); stderr_bytes = errors.tell()
        if len(raw) > 16 * 1024**2:
            raw = b''
            timed_out = True  # fail-closed on oversized evidence, never count it as PASS
            limit_reason = 'output_limit_exceeded'
        else:
            limit_reason = 'step_timeout' if timed_out else None
        return {'returncode': child.returncode, 'stdout': raw.decode('utf-8', errors='replace'),
                'stderr_bytes': stderr_bytes, 'stderr_policy': 'raw stderr not stored (may contain credentials)',
                'timed_out': timed_out, 'interrupted': interrupted, 'limit_reason': limit_reason,
                'elapsed_seconds': round(time.monotonic() - started, 3)}


def read_json(path: Path, limit=16 * 1024**2):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise RuntimeError('Missing, symlinked or oversized structured evidence')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError('Evidence must be a JSON object')
    return value


def result_path(root: Path, payload: dict, family: str) -> Path:
    value = payload.get('report') if family == 'simulate' else payload.get('report_directory')
    if not isinstance(value, str):
        raise RuntimeError('Child did not identify a report')
    path = Path(value)
    if family == 'simulate':
        path = path.parent
    base = root / 'reports' / {'simulate': 'simulations', 'drills': 'drills', 'messages': 'messages'}[family]
    if path.resolve().parent != base.resolve() or path.is_symlink():
        raise RuntimeError('Child evidence path lies outside the selected report family')
    return path / 'summary.json'


def validate_child(root: Path, step: Step, value: dict, previous_reports=frozenset()) -> dict:
    if step.family == 'smoke':
        if (value.get('passed') is not True or value.get('submitted_payload_checked') is not True
                or value.get('full_retry_payload_checked') is not True):
            raise RuntimeError('Smoke did not confirm full request and retry contents')
        return value
    path = result_path(root, value, step.family)
    if str(path.resolve()) in previous_reports:
        raise RuntimeError('A previous report cannot count as this step execution')
    evidence = read_json(path)
    if (evidence.get('status') != 'passed' or evidence.get('run_id') != value.get('run_id')
            or evidence.get('scenario', evidence.get('kind')) != step.scenario):
        raise RuntimeError('Scenario identity/status disagrees with stored evidence')
    expected_kind = {'simulate': 'real-http-and-docker-actions', 'drills': 'LIVE-LOCAL-DOCKER-DRILL',
                     'messages': 'LIVE-LOCAL-DOCKER-ISOLATED-MESSAGE-DRILL'}[step.family]
    if evidence.get('evidence_kind') != expected_kind:
        raise RuntimeError('Non-live or unrecognized evidence cannot satisfy live acceptance')
    if step.family == 'simulate' and evidence.get('fault_restored') is not True:
        raise RuntimeError('Fault restoration was not confirmed')
    if step.family == 'drills' and evidence.get('cleanup') is not True:
        raise RuntimeError('Disposable cleanup was not confirmed')
    if step.family == 'messages' and (evidence.get('cleanup') or {}).get('cleaned') is not True:
        raise RuntimeError('Message resources were not cleaned')
    return {'report': str(path.relative_to(root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'run_id': evidence['run_id'], 'status': evidence['status'], 'evidence_kind': expected_kind}


def write_reports(directory: Path, summary: dict, atomic):
    atomic(directory / 'summary.json', summary)
    steps = summary['steps']
    lines = ['# 실제 DB 인수시험 결과', '', f"실행: `{summary['run_id']}` · 범위: `{summary['suite']}`",
             f"판정: **{summary['status']}** · 실제 시나리오 통과: {summary['passed_scenarios']}/{summary['planned_scenarios']}",
             '', '계획·미실행·차단은 성공이 아닙니다. 이 보고서는 선택한 범위만 판정합니다.', '',
             '| 단계 | 상태 | 종료 코드 | 이유 |', '|---|---|---:|---|']
    for row in steps:
        reason = str(row.get('reason', '')).replace('|', '/').replace('\n', ' ')
        lines.append(f"|{row['name']}|{row['status']}|{row.get('returncode', '')}|{reason}|")
    lines += ['', '## 범위와 복구', '', '누락·실패 항목은 summary.json과 단계별 JSON을 확인합니다. 이전 실행의 PASS를 재사용하지 않습니다.',
              '실패 시 원래 복구 marker를 유지합니다. simulate/drills/messages recover --yes는 해당 marker를 확인한 뒤 별도로 실행하세요.',
              '자동 재기동·삭제·재색인·캐시 삭제·이미지 pull/build는 하지 않습니다. 합성 주문은 남습니다.',
              '개별 관리 명령/다른 디렉터리/수동 Docker 조작을 전체 실행 동안 잠그지는 못합니다. 실행 중 다른 조작을 하지 마세요.',
              '실제 HA·성능·물리 내구성 인증이 아니며 candidate 버전 시험은 별도 선택입니다.']
    report = directory / 'report.md'; report.write_text('\n'.join(lines) + '\n'); report.chmod(0o600)
    failures = sum(s['status'] in {'failed', 'inconclusive'} for s in steps)
    errors = sum(s['status'] in {'blocked', 'aborted', 'running'} for s in steps)
    skipped = sum(s['status'] == 'not_run' for s in steps)
    xml = ET.Element('testsuite', name='db-lab-' + summary['suite'], tests=str(len(steps)),
                     failures=str(failures), errors=str(errors), skipped=str(skipped))
    for row in steps:
        case = ET.SubElement(xml, 'testcase', name=row['name'], time=str(row.get('elapsed_seconds', 0)))
        status = row['status']
        if status in {'failed', 'inconclusive'}:
            ET.SubElement(case, 'failure', message=row.get('reason', status))
        elif status in {'blocked', 'aborted', 'running'}:
            ET.SubElement(case, 'error', message=row.get('reason', status))
        elif status == 'not_run':
            ET.SubElement(case, 'skipped', message='not executed, never counted as PASS')
    path = directory / 'junit.xml'
    ET.ElementTree(xml).write(path, encoding='utf-8', xml_declaration=True); path.chmod(0o600)


class Gate:
    def __init__(self, manage, suite: str, image=None, timeout=650):
        self.m, self.suite, self.image, self.timeout = manage, suite, image, timeout
        self.steps = steps_for(suite, image)
        self.directory = manage.ROOT / 'reports/acceptance' / ('accept-' + uuid.uuid4().hex[:12])
        self.directory.mkdir(parents=True, mode=0o700)
        self.summary = {'schema': 1, 'run_id': self.directory.name, 'suite': suite, 'status': 'blocked',
                        'evidence_kind': 'LIVE-ACCEPTANCE-ATTEMPT', 'started_at_utc': datetime.now(timezone.utc).isoformat(),
                        'planned_scenarios': sum(s.scenario is not None for s in self.steps), 'passed_scenarios': 0,
                        'steps': [{'name': n, 'status': 'not_run'} for n in ('container-targets', 'api-runtime-contract', 'worker-runtime-contract')]
                                 + [{**s.public(), 'status': 'not_run'} for s in self.steps]}
        self.lab = None
        self.c = None
        self.expected_source = source_manifest(manage.ROOT)
        self.m.atomic_json(self.directory / 'plan.json', plan(suite, image))
        self.m.atomic_json(self.directory / 'source-manifest.json', self.expected_source)
        self.persist()

    def persist(self):
        self.summary['passed_scenarios'] = sum(s.get('scenario') is not None and s['status'] == 'passed' for s in self.summary['steps'])
        write_reports(self.directory, self.summary, self.m.atomic_json)

    def preflight(self):
        if not shutil.which('docker'):
            raise FileNotFoundError('Docker is unavailable')
        self.m.require_no_active_fault()
        config = self.m.validate(self.m.parse_env(self.m.ROOT / '.env'))
        self.c = self.m.Compose(config)
        self.lab = DockerLab(self.c, self.m)
        self.c.ready()
        info = json.loads(self.lab.docker('info', '--format', '{{json .}}'))
        if info.get('OSType') != 'linux':
            raise RuntimeError('Live acceptance requires Linux containers')
        states = self.lab.preflight()
        for state in states:
            if state['service'] != 'api' and any(state['published_ports'].values()):
                raise RuntimeError('Only the API may publish a host port')
        paths = {'mariadb': '/var/lib/mysql', 'kafka': '/var/lib/kafka/data',
                 'elasticsearch': '/usr/share/elasticsearch/data', 'redis': '/data'}
        for service, destination in paths.items():
            state = next(s for s in states if s['service'] == service)
            if not state['volumes'].get(destination):
                raise RuntimeError('A required DB data directory is not backed by a named volume')
        api, worker = (next(s for s in states if s['service'] == name) for name in ('api', 'worker'))
        if api['image_id'] != worker['image_id']:
            raise RuntimeError('API and worker are running different application image IDs')
        self.m.atomic_json(self.directory / 'containers-before.json', {'engine': self.lab.binding(), 'containers': states})
        settings = Settings(sql_password=config['SQL_PASSWORD'], redis_password=config['REDIS_PASSWORD'], cache_ttl=int(config['CACHE_TTL']))
        self.target = target_manifest(settings, config['MVP_PROJECT'])
        self.initial = {s['service']: {'image_id': s['image_id'], 'volumes': s['volumes']} for s in states}
        return states

    def check_unchanged(self):
        self.m.require_no_active_fault()
        if source_manifest(self.m.ROOT) != self.expected_source:
            raise RuntimeError('Host source changed during acceptance')
        states = self.lab.preflight()
        current = {s['service']: {'image_id': s['image_id'], 'volumes': s['volumes']} for s in states}
        if current != self.initial:
            raise RuntimeError('Image or data volume identity changed during acceptance')
        return states

    def native(self, service, states):
        state = next(s for s in states if s['service'] == service)
        self.lab.inspect(state['id'], service)
        request = {'schema': 1, 'service': service, 'source_sha256': self.expected_source['sha256'], 'target': self.target}
        return execute([*self.c.engine, 'exec', '-i', state['id'], 'python', '-m', 'mvp_app.runtime_contract', 'probe'],
                       cwd=self.m.ROOT, env=self.c.inherited, timeout=90, data=json.dumps(request))

    def run(self):
        rows = self.summary['steps']
        index = 0
        previous = signal.getsignal(signal.SIGTERM)
        def interrupted(*_):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, interrupted)
        try:
            with suite_lock(self.m.ROOT):
                rows[0]['status'] = 'running'; self.persist()
                with self.m.lock():
                    states = self.preflight()
                    rows[0]['status'] = 'passed'; self.persist()
                    for index, service in ((1, 'api'), (2, 'worker')):
                        rows[index]['status'] = 'running'; self.persist()
                        result = self.native(service, states)
                        detail = self.record_result(index, result)
                        expected = {'image_source', 'driver_imports_and_pins', 'effective_target', 'mariadb', 'redis', 'elasticsearch', 'kafka'}
                        checks = detail.get('checks', [])
                        if (detail.get('status') != 'passed' or detail.get('service') != service or len(checks) != len(expected)
                                or {c.get('name') for c in checks} != expected or any(c.get('status') != 'passed' for c in checks)
                                or detail.get('evidence_kind') != 'LIVE-DRIVER-READ-ONLY-PROBE' or detail.get('data_writes') is not False):
                            raise RuntimeError('Image/driver/read-only contract did not pass; see stage JSON, rebuild only after investigation')
                        rows[index]['status'] = 'passed'; self.persist()
                for index, step in enumerate(self.steps, 3):
                    rows[index]['status'] = 'running'; self.persist()
                    # Child commands own manage.lock themselves; do not hold it recursively.
                    with self.m.lock():
                        self.check_unchanged()
                    previous_reports = {str(p.resolve()) for family in ('simulations', 'drills', 'messages')
                                        for p in (self.m.ROOT / 'reports' / family).glob('*/summary.json')}
                    result = execute([sys.executable, str(self.m.ROOT / 'tools/manage.py'), *step.command],
                                     cwd=self.m.ROOT, env=self.c.inherited, timeout=self.timeout)
                    detail = self.record_result(index, result)
                    rows[index]['proof'] = validate_child(self.m.ROOT, step, detail, previous_reports)
                    with self.m.lock():
                        self.check_unchanged()
                    rows[index]['status'] = 'passed'; self.persist()
                self.summary['status'] = 'passed'
        except FileNotFoundError:
            rows[index].update(status='blocked', reason='Required runtime or project file is missing', returncode=127)
            self.summary['status'] = 'blocked'
        except KeyboardInterrupt:
            rows[index].update(status='aborted', reason='Interrupted; verify pending recovery markers before another run')
            self.summary['status'] = 'aborted'
        except Exception as exc:
            status = rows[index].get('status')
            if status not in {'failed', 'inconclusive', 'aborted'}:
                rows[index]['status'] = 'blocked' if index <= 2 else 'failed'
            rows[index]['reason'] = str(exc) if type(exc) is RuntimeError else type(exc).__name__
            self.summary['status'] = rows[index]['status']
        finally:
            signal.signal(signal.SIGTERM, previous)
            self.summary['ended_at_utc'] = datetime.now(timezone.utc).isoformat()
            self.summary['recovery_markers'] = [p.name for p in (self.m.ROOT / '.state').glob('*-active.json')]
            if self.summary['recovery_markers'] and self.summary['status'] == 'passed':
                self.summary['status'] = 'failed'
                rows[-1].update(status='failed', reason='Unresolved recovery marker remains')
            self.persist()
        code = 127 if rows[0].get('returncode') == 127 else CODES[self.summary['status']]
        print(json.dumps({'status': self.summary['status'], 'run_id': self.summary['run_id'],
                          'passed_scenarios': self.summary['passed_scenarios'], 'planned_scenarios': self.summary['planned_scenarios'],
                          'report_directory': str(self.directory)}, ensure_ascii=False, indent=2))
        return code

    def record_result(self, index, result):
        row = self.summary['steps'][index]
        record = {k: v for k, v in result.items() if k != 'stdout'}
        try:
            detail = json.loads(result['stdout'])
            if not isinstance(detail, dict):
                raise ValueError()
        except (ValueError, TypeError):
            detail = {'status': 'failed', 'reason': 'missing_or_invalid_json_evidence'}
        record['result'] = detail
        self.m.atomic_json(self.directory / (row['name'] + '.json'), record)
        row.update({k: record[k] for k in ('returncode', 'elapsed_seconds', 'timed_out', 'stderr_bytes')})
        if result.get('interrupted'):
            row['status'] = 'aborted'
            raise KeyboardInterrupt()
        if result.get('timed_out'):
            row['status'] = 'failed'
            raise RuntimeError('Step exceeded execution/output budget; it is not a confirmed completed test')
        if result['returncode'] != 0:
            row['status'] = 'inconclusive' if result['returncode'] == 2 and detail.get('status') == 'inconclusive' else 'failed'
            raise RuntimeError('Child did not exit successfully; no later scenario was executed')
        return detail


def add_parser(sub):
    parser = sub.add_parser('verify', help='Fail-closed live acceptance; images/drivers/DB contracts before scenarios')
    actions = parser.add_subparsers(dest='verify_action', required=True)
    actions.add_parser('list')
    for action in ('plan', 'run'):
        p = actions.add_parser(action)
        p.add_argument('suite', nargs='?', choices=SUITES, default='core')
        p.add_argument('--candidate-image')
        if action == 'run':
            p.add_argument('--yes', action='store_true', required=True)
            p.add_argument('--step-timeout', type=int, default=650, help='Per scenario subprocess budget, 120..1200 seconds')
    p = actions.add_parser('report'); p.add_argument('run_id')
    p = actions.add_parser('export', help='Write an allowlisted shareable result; no raw logs/orders/env'); p.add_argument('run_id')


def cli(args, manage):
    if args.verify_action == 'list':
        print(json.dumps({'suites': SUITES, 'default': 'core', 'all_excludes': 'explicit candidate-image trial'}, ensure_ascii=False, indent=2))
        return 0
    if args.verify_action == 'export':
        from tools.evidence_export import export
        path = export(manage.ROOT, args.run_id, manage.atomic_json)
        print(json.dumps({'export_directory': str(path), 'automatic_upload': False}))
        return 0
    if args.verify_action == 'report':
        if not re.fullmatch(RUN_RE, args.run_id):
            raise ValueError('Invalid acceptance run ID')
        value = read_json(manage.ROOT / 'reports/acceptance' / args.run_id / 'summary.json')
        print(json.dumps(value, ensure_ascii=False, indent=2)); return 0
    document = plan(args.suite, args.candidate_image)
    if args.verify_action == 'plan':
        print(json.dumps(document, ensure_ascii=False, indent=2)); return 0
    if not 120 <= args.step_timeout <= 1200:
        raise ValueError('--step-timeout must be 120..1200 seconds')
    return Gate(manage, args.suite, args.candidate_image, args.step_timeout).run()
