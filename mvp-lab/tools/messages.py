"""Pinned-local-Docker orchestration for isolated Kafka message recovery trials."""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import re
import signal
import subprocess
import time
import uuid
from .sim_engine import DockerLab
from mvp_app.message_safety import Scope, RUN_RE, SCENARIOS


def add_parser(sub):
    parser = sub.add_parser('messages', help='Isolated poison/DLQ/replay/version drills; normal worker remains strict')
    actions = parser.add_subparsers(dest='message_action', required=True)
    actions.add_parser('list')
    plan = actions.add_parser('plan')
    plan.add_argument('scenario', choices=SCENARIOS)
    run = actions.add_parser('run')
    run.add_argument('scenario', choices=SCENARIOS)
    run.add_argument('--yes', action='store_true', required=True)
    run.add_argument('--keep', action='store_true', help='Keep only study topics/index for inspection; explicit cleanup needed')
    for action in ('inspect', 'cleanup'):
        p = actions.add_parser(action)
        p.add_argument('run_id')
        if action == 'cleanup':
            p.add_argument('--yes', action='store_true', required=True)
    recovery = actions.add_parser('recover')
    recovery.add_argument('--yes', action='store_true', required=True)


class Session:
    def __init__(self, lab, manage, record, directory):
        self.lab, self.m, self.record, self.directory = lab, manage, record, directory
        self.path = manage.ROOT / '.state/message-active.json'
        self.scope = Scope(**record['scope'])
        events_path = directory / 'events.json'
        if events_path.exists() and events_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Saved event log exceeds limit')
        self.events = json.loads(events_path.read_text()) if events_path.exists() else []
        if not isinstance(self.events, list):
            raise ValueError('Invalid saved event log')

    def validate(self):
        if (self.record.get('schema') != 1 or self.scope.project != self.lab.c.config['MVP_PROJECT']
                or self.record.get('engine') != self.lab.binding()
                or self.record.get('scenario') not in SCENARIOS
                or not re.fullmatch(r'[a-f0-9]{12,64}', self.record.get('api_id', ''))):
            raise RuntimeError('Message recovery ledger is foreign or malformed')
        self.lab.inspect(self.record['api_id'], 'api')

    def save(self):
        self.m.atomic_json(self.directory / 'ledger.json', self.record)
        self.m.atomic_json(self.path, self.record)

    def rpc(self, action, **fields):
        self.validate()
        request = {'scope': asdict(self.scope), 'resources': self.record['resources'], **fields}
        command = [*self.lab.c.engine, 'exec', '-i', self.record['api_id'], 'python', '-m', 'mvp_app.message_drill', action]
        result = subprocess.run(command, env=self.lab.c.inherited, input=json.dumps(request),
                                capture_output=True, text=True, timeout=175, check=False)
        if len(result.stdout.encode()) > 2 * 1024 * 1024:
            raise RuntimeError('Study helper output budget exceeded')
        events = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get('kind') in ('observation', 'result', 'failure'):
                events.append({'rpc': action, **event})
        self.events.extend(events)
        # Private artifact may contain only this run's synthetic payloads/DLQ bytes.
        self.m.atomic_json(self.directory / 'events.json', self.events)
        results = [event['result'] for event in events if event['kind'] == 'result']
        if result.returncode != 0 or len(results) != 1:
            raise RuntimeError('Message helper failed or returned no evidence; inspect events.json (raw stderr redacted)')
        return results[0]

    def create(self):
        self.save()  # write intent BEFORE requests which may have uncertain outcomes
        for topic in (self.scope.topic, self.scope.dlq):
            self.record['stage'] = 'creating-topic'
            self.save()
            result = self.rpc('create-topic', topic=topic)
            if result.get('name') != topic or not result.get('topic_id'):
                raise RuntimeError('Missing created topic UUID; no name-only adoption')
            self.record['resources']['topics'][topic] = result['topic_id']
            self.save()
        self.record['stage'] = 'creating-index'
        self.save()
        result = self.rpc('create-index')
        if result.get('name') != self.scope.index or not result.get('index_uuid'):
            raise RuntimeError('Missing created index UUID')
        self.record['resources']['index'] = result
        self.record['stage'] = 'ready'
        self.save()

    def cleanup(self):
        self.validate()
        if self.path.exists() and json.loads(self.path.read_text()).get('scope') != self.record['scope']:
            raise RuntimeError('Another message run is active; no cleanup performed')
        result = self.rpc('cleanup')
        if result.get('cleaned') is not True:
            raise RuntimeError('No confirmed cleanup evidence')
        self.record['stage'] = 'cleaned'
        self.record['cleanup'] = result
        self.m.atomic_json(self.directory / 'ledger.json', self.record)
        if self.path.exists():
            active = json.loads(self.path.read_text())
            if active.get('scope') != self.record['scope']:
                raise RuntimeError('Another message record became active; refusing to clear it')
            self.path.unlink()
        return result


def location(manage, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(RUN_RE, run_id):
        raise ValueError('Expected a msg- followed by 24 hexadecimal digits, not a path')
    base = manage.ROOT / 'reports/messages'
    directory = base / run_id
    if directory.is_symlink() or (directory / 'ledger.json').is_symlink():
        raise ValueError('Symlink message records are not accepted')
    return directory


def report_text(summary, events):
    lines = ['# 메시지 정합성 실습 결과', '',
             f"- 실행: `{summary['run_id']}` / `{summary['scenario']}`",
             f"- 판정: **{summary['status']}**",
             f"- 증거 종류: `{summary['evidence_kind']}`",
             '', '기본 주문 이벤트 토픽/소비자 그룹을 수정하거나 되감지 않습니다. 합성 주문은 원본 SQL에 남습니다.',
             'commit-gap은 의도적인 애플리케이션 예외이며 실제 프로세스 종료·네트워크 장애·리밸런스 시험이 아닙니다.',
             '', '## 단계별 관측', '', '| 단계 | 관측 |', '|---|---|']
    for event in events:
        if event['kind'] == 'observation':
            stage = event.get('stage', '')
            keys = ('offset', 'policy', 'outcome', 'reason', 'dlq_id', 'checkpoint', 'matched', 'duplicate_quarantines')
            fields = {key: event[key] for key in keys if key in event}
            lines.append('| ' + stage + ' | `' + json.dumps(fields, ensure_ascii=False).replace('|', '\\|') + '` |')
    lines += ['', '## 결과 파일', '', '`summary.json`, `events.json`, `ledger.json`, `source-containers.json`을 함께 확인합니다.',
              'DLQ는 격리된 전달 기록입니다. Kafka 1노드 실습의 ACK는 다중 노드 내구성 인증이 아닙니다.',
              '최종 데이터 검사는 강제 캐시 삭제나 검색 refresh 없이 원본/문서/검색 가시성/남은 캐시를 비교합니다.', '']
    return '\n'.join(lines)


def cli(args, manage):
    if args.message_action == 'list':
        print(json.dumps(SCENARIOS, ensure_ascii=False, indent=2))
        return 0
    if args.message_action == 'plan':
        print(json.dumps({'scenario': args.scenario, 'goal': SCENARIOS[args.scenario],
            'writes': '3 synthetic orders; replay-ordering also advances one order twice',
            'resources': '2 one-partition topics, 1 group, 1 index, run-prefixed 30s Redis keys',
            'policy': 'strict observation, allowlisted permanent-error quarantine after ACK, current-SQL repair',
            'normal_worker': 'unchanged, strict; no original offset reset',
            'limits': '100 publishes, 32 DLQ records, 150s in-container helper limit',
            'cleanup': 'default deletes ONLY UUID-verified study topics/index; --keep is opt-in',
            'injection': 'explicit application exception for commit-gap; NOT process crash'}, ensure_ascii=False, indent=2))
        return 0
    with manage.lock():
        config = manage.validate(manage.parse_env(manage.ROOT / '.env'))
        lab = DockerLab(manage.Compose(config), manage)
        if args.message_action in ('recover', 'cleanup', 'inspect'):
            active_path = manage.ROOT / '.state/message-active.json'
            if args.message_action == 'recover':
                if not active_path.exists():
                    print(json.dumps({'cleaned': True, 'reason': 'no active message run'}))
                    return 0
                record = json.loads(active_path.read_text())
                directory = location(manage, record['scope']['run_id'])
            else:
                directory = location(manage, args.run_id)
                record = json.loads((directory / 'ledger.json').read_text())
                if active_path.exists() and json.loads(active_path.read_text()).get('scope') != record.get('scope'):
                    raise RuntimeError('A different message run is unfinished; recover it first')
            if (manage.ROOT / '.state/simulation-active.json').exists() or (manage.ROOT / '.state/drill-active.json').exists():
                raise RuntimeError('Another fault is pending; restore it before touching message resources')
            session = Session(lab, manage, record, directory)
            if args.message_action == 'inspect':
                print(json.dumps(session.rpc('inspect'), ensure_ascii=False, indent=2))
            else:
                session.validate()
                session.save()  # interrupted cleanup must leave a recovery record
                print(json.dumps(session.cleanup(), ensure_ascii=False, indent=2))
            return 0
        manage.require_no_active_fault()
        run_id = 'msg-' + uuid.uuid4().hex[:24]
        directory = location(manage, run_id)
        directory.mkdir(parents=True, mode=0o700)
        summary = {'schema': 1, 'run_id': run_id, 'scenario': args.scenario, 'status': 'failed',
                   'evidence_kind': 'NOT-EXECUTED', 'started_at_utc': datetime.now(timezone.utc).isoformat(),
                   'kept': False, 'cleanup': None}
        session = None
        previous = signal.getsignal(signal.SIGTERM)
        def interrupt(*_): raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, interrupt)
        try:
            # Bound retained sessions; orphan resources are never automatically adopted/deleted.
            retained = [p for p in directory.parent.glob('*/ledger.json')
                        if json.loads(p.read_text()).get('stage') == 'retained']
            if len(retained) >= 8:
                raise RuntimeError('Eight retained message runs; explicitly cleanup a run before starting another')
            states = lab.preflight()
            api = next(s for s in states if s['service'] == 'api')
            raw = json.loads(lab.docker('inspect', api['id']))[0]
            env = dict(v.split('=', 1) for v in raw['Config'].get('Env', []) if '=' in v)
            expected = {'SQL_HOST': 'mariadb', 'SQL_DATABASE': 'mvp', 'SQL_USER': 'mvp', 'STUDY_PROJECT': config['MVP_PROJECT'],
                        'KAFKA_BOOTSTRAP': 'kafka:9092', 'KAFKA_TOPIC': 'mvp.orders.v1', 'KAFKA_GROUP': 'mvp.search.v1',
                        'ES_URL': 'http://elasticsearch:9200', 'ES_INDEX': 'mvp-orders-v1', 'REDIS_HOST': 'redis'}
            if any(env.get(k) != v for k, v in expected.items()):
                raise RuntimeError('Live API is not the selected normal MVP; no message injection performed')
            scope = Scope(config['MVP_PROJECT'], run_id, uuid.uuid4().hex)
            record = {'schema': 1, 'scope': asdict(scope), 'engine': lab.binding(), 'api_id': api['id'],
                      'scenario': args.scenario, 'stage': 'planned', 'resources': {'topics': {}}}
            session = Session(lab, manage, record, directory)
            summary['scope'] = scope.public()
            summary['evidence_kind'] = 'LIVE-LOCAL-DOCKER-ISOLATED-MESSAGE-DRILL'
            manage.atomic_json(directory / 'source-containers.json', states)
            sources = {str(p.relative_to(manage.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for top in ('mvp_app', 'tools') for p in (manage.ROOT / top).rglob('*.py')}
            manage.atomic_json(directory / 'source-manifest.json', sources)
            session.create()
            result = session.rpc('run', scenario=args.scenario)
            summary['result'] = result
            if result.get('status') != 'passed':
                raise RuntimeError('Scenario did not return a successful evidence-based result')
            summary['status'] = 'passed'
        except KeyboardInterrupt:
            summary['status'] = 'aborted'
        except Exception as exc:
            summary['error'] = type(exc).__name__
            summary['reason'] = str(exc) if type(exc) in (RuntimeError, ValueError) else 'Preflight/helper failed; no raw connection details recorded'
        finally:
            if session is not None:
                try:
                    if args.keep and summary['status'] == 'passed':
                        session.record['stage'] = 'retained'
                        session.save()
                        session.path.unlink()
                        summary['kept'] = True
                        summary['cleanup_command'] = 'bash ./all.sh mvp messages cleanup ' + run_id + ' --yes'
                    elif summary['status'] == 'passed':
                        summary['cleanup'] = session.cleanup()
                    else:
                        # Preserve failed evidence; recovery is explicit and refuses a live helper.
                        session.record['stage'] = 'failed_or_interrupted'
                        session.save()
                        summary['recover_command'] = 'bash ./all.sh mvp messages recover --yes'
                except Exception as exc:
                    summary['status'] = 'failed'
                    summary['cleanup_error'] = type(exc).__name__
                    summary['recover_command'] = 'bash ./all.sh mvp messages recover --yes'
            signal.signal(signal.SIGTERM, previous)
            summary['ended_at_utc'] = datetime.now(timezone.utc).isoformat()
            manage.atomic_json(directory / 'summary.json', summary)
            (directory / 'report.md').write_text(report_text(summary, session.events if session else []))
            (directory / 'report.md').chmod(0o600)
        print(json.dumps({**summary, 'report_directory': str(directory)}, ensure_ascii=False, indent=2))
        return {'passed': 0, 'failed': 1, 'aborted': 130}[summary['status']]
