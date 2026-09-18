#!/usr/bin/env python3
"""Host-only incident scenario orchestration. Standard library; no image rebuild required."""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path
import secrets
import signal
import subprocess
import time

NODES = ('galera1', 'galera2', 'galera3')
ROOT = Path(__file__).resolve().parent.parent
ACTIONS = {
    'slow': '슬로우 로그 확인 + 인덱스 전후 실행 계획/소요시간 비교',
    'fragmentation': '대량 삽입 → 80% 삭제 → InnoDB 테이블 재구축/공간 비교',
    'node-failure': '한 노드 SIGKILL → 프록시 신규 쓰기 연결 관찰 → 재가입',
    'node-hang': '한 컨테이너 pause → 감지/접속 오류 관찰 → unpause',
    'quorum': '두 노드 pause → 과반수 상실 관찰 → 두 노드 복귀 (별도 승인)',
    'lock': '같은 노드의 두 SQL 세션으로 행 잠금 대기 → ROLLBACK',
    'demo': 'slow, fragmentation, node-failure 순서로 실행 (quorum 제외)',
}


class ScenarioError(RuntimeError):
    pass


def bounded_int(low, high):
    def parse(text):
        try:
            value = int(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError('정수를 지정하세요') from exc
        if not low <= value <= high:
            raise argparse.ArgumentTypeError('%d..%d 범위여야 합니다' % (low, high))
        return value
    return parse


def register(subparsers):
    parser = subparsers.add_parser('scenario', help='원클릭 슬로우쿼리/단편화/장애 시나리오')
    sub = parser.add_subparsers(dest='scenario', required=True)
    sub.add_parser('list', help='시나리오 목록; DB 불필요')
    sub.add_parser('report', help='가장 최근 실행 결과; DB 불필요')
    sub.add_parser('inspect', help='현재 노드/실습 테이블/락 상태')
    sub.add_parser('repair', help='중단된 실습의 노드/슬로우로그 설정 복구')
    p = sub.add_parser('cleanup', help='incident_lab만 삭제 (다른 DB/볼륨 보존)')
    p.add_argument('--confirm-cleanup', action='store_true')
    for name, helptext in ACTIONS.items():
        p = sub.add_parser(name, help=helptext.replace('%', '%%'))
        if name in ('slow', 'fragmentation', 'demo'):
            p.add_argument('--rows', type=bounded_int(1000, 500000), default=50000)
        if name in ('fragmentation', 'demo'):
            p.add_argument('--payload-bytes', type=bounded_int(128, 4096), default=1024)
        if name in ('slow', 'fragmentation'):
            p.add_argument('--leave-broken', action='store_true', help='비교/개선은 생략하고 문제 테이블을 남김')
        if name in ('node-failure', 'node-hang'):
            p.add_argument('--node', choices=NODES, default='galera1')
        if name in ('node-failure', 'node-hang', 'quorum', 'lock', 'demo'):
            lo, hi, default = (30, 180, 45) if name == 'quorum' else ((6,120,12) if name == 'lock' else (5,180,20))
            p.add_argument('--hold', type=bounded_int(lo,hi), default=default, help='장애/잠금 유지 목표 시간(초); 복구 대기는 별도')
        if name == 'quorum':
            p.add_argument('--confirm-quorum', action='store_true')
    p = sub.add_parser('fix', help='--leave-broken으로 남긴 테이블 개선')
    p.add_argument('target', choices=['slow', 'fragmentation'])


def no_runtime(args):
    if args.scenario == 'list':
        print('실행: ./lab.sh scenario <이름> [옵션]\n')
        for name, helptext in ACTIONS.items():
            print('  %-16s %s' % (name, helptext))
        print('\n진단: inspect | report | repair\n개선: fix slow | fix fragmentation\n정리: cleanup --confirm-cleanup')
        print('전용 DB: incident_lab / 문서: docs/SCENARIOS.md')
        return True
    if args.scenario == 'report':
        latest = ROOT / 'reports' / 'scenarios' / 'latest.json'
        if not latest.exists():
            print('아직 시나리오 실행 기록이 없습니다.'); return True
        data = json.loads(latest.read_text())
        print(json.dumps(data, ensure_ascii=False, indent=2))
        path = ROOT / data['directory'] / 'summary.md'
        if path.is_file():
            print('\n' + path.read_text())
        return True
    return False


def private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
    temp.chmod(0o600)
    temp.replace(path)


def check_health(status):
    if set(status) != set(NODES):
        raise ScenarioError('세 노드 모두의 상태가 필요합니다')
    for n, s in status.items():
        if not s.get('ready') or str(s.get('wsrep_cluster_size')) != '3' or s.get('node',n) != n:
            raise ScenarioError('%s가 Primary/Synced/size=3이 아닙니다. ./lab.sh status로 확인하세요' % n)
    ids = {s.get('wsrep_cluster_state_uuid') for s in status.values()}
    if len(ids) != 1 or None in ids or '' in ids:
        raise ScenarioError('세 노드의 클러스터 UUID가 같지 않습니다')


class Runner:
    def __init__(self, lab):
        self.lab = lab
        self.root = ROOT
        self.journal_path = self.root / '.state' / 'scenario-pending.json'
        self.journal = None
        self.report = None
        self.directory = None
        self.worker_source = (self.root / 'scripts' / 'scenario_worker.py').read_text()

    def log(self, message, **details):
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        print(message, flush=True)
        if self.report is not None:
            event = {'at':stamp, 'message':message, **details}
            with (self.directory / 'events.jsonl').open('a', encoding='utf8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + '\n')

    def save_journal(self):
        private_json(self.journal_path, self.journal)

    def begin(self, kind):
        if self.journal_path.exists():
            raise ScenarioError('이전 실습 복구 기록이 남아 있습니다. ./lab.sh scenario repair부터 실행하세요')
        token = secrets.token_hex(12)
        stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        self.directory = self.root / 'reports' / 'scenarios' / (stamp + '-' + kind + '-' + token[:6])
        self.directory.mkdir(parents=True, mode=0o700)
        self.report = {'scenario':kind, 'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                       'directory':str(self.directory.relative_to(self.root)), 'status':'running',
                       'project':self.lab.project, 'token':token, 'results':{}}
        self.journal = {'version':1, 'project':self.lab.project, 'engine':self.lab.engine,
                        'token':token, 'nodes':[], 'logging':{}, 'active_worker':None}
        self.save_journal()
        self.flush_report()
        self.log('[시작] ' + kind + ' / 결과: ' + self.report['directory'])

    def flush_report(self):
        if self.report is None: return
        private_json(self.directory / 'summary.json', self.report)
        private_json(self.root / 'reports' / 'scenarios' / 'latest.json', {
            'scenario':self.report['scenario'], 'status':self.report['status'],
            'directory':self.report['directory'], 'started_at':self.report['started_at']})
        rows = ['# MariaDB 시나리오 실행 결과', '',
                '- 시나리오: ' + self.report['scenario'], '- 상태: ' + self.report['status'],
                '- 시작: ' + self.report['started_at'], '- 실제 측정/상세: `summary.json`, `events.jsonl`', '']
        if self.report.get('error'): rows.extend(['## 오류', self.report['error'], ''])
        if self.report.get('recovery_error'): rows.extend(['## 복구 확인 필요', self.report['recovery_error'], './lab.sh scenario repair', ''])
        for name, result in self.report['results'].items():
            rows += ['## ' + name, '```json', json.dumps(result, ensure_ascii=False, indent=2), '```', '']
        (self.directory / 'summary.md').write_text('\n'.join(rows), encoding='utf8')

    def worker(self, node, action, timeout=600, track=True, **params):
        token = self.journal['token'] if self.journal else secrets.token_hex(12)
        request = {'action':action, 'owner':self.lab.project, 'token':token, **params}
        if self.journal and track:
            self.journal['active_worker'] = {'node':node, 'token':token, 'action':action}
            self.save_journal()
        args = [self.lab.engine, 'exec', '-i', self.lab.name(node), 'python3', '-c',
                self.worker_source, '--mariadb-incident-worker', request['token']]
        p = self.lab.run(args, input=json.dumps(request), capture=True, check=False, timeout=timeout)
        try:
            payload = json.loads(p.stdout)
        except (ValueError, TypeError) as exc:
            detail = (p.stderr or p.stdout or '')[-1500:]
            for key in ('ROOT_PASSWORD','LAB_PASSWORD','READONLY_PASSWORD','SST_PASSWORD'):
                detail = detail.replace(self.lab.settings.get(key,'<unset>'), '<redacted>')
            raise ScenarioError('Worker did not return JSON (%s/%s): %s' % (node, action, detail)) from exc
        if not payload.get('ok') or p.returncode:
            raise ScenarioError('%s/%s: %s' % (node, action, payload.get('error', 'worker failed')))
        if self.journal and track:
            self.journal['active_worker'] = None
            self.save_journal()
        return payload['result']

    def preflight(self):
        status = {n:self.lab.health(n) for n in NODES}
        check_health(status)
        # Confirm this is the lab image, not an arbitrary MariaDB container with a reused name.
        for n in NODES:
            if status[n].get('mode') != 'galera':
                raise ScenarioError('Expected this lab Galera image: ' + n)
        return status

    def remember_logging(self, node):
        if node not in self.journal['logging']:
            self.journal['logging'][node] = self.worker(node, 'logging-state', track=False)
            self.save_journal()

    def recover_pending(self):
        if not self.journal_path.exists():
            return
        self.journal = json.loads(self.journal_path.read_text())
        j = self.journal
        if (j.get('version'),j.get('project'),j.get('engine')) != (1,self.lab.project,self.lab.engine):
            raise ScenarioError('복구 기록의 프로젝트/런타임이 다릅니다. 원래 .env를 사용하세요')
        errors = []
        # Unpause ALL affected containers first. Quorum may require two to return together.
        for n in j['nodes']:
            if n not in NODES: raise ScenarioError('Unknown node in recovery journal')
            try:
                state = self.lab.state(n)
                if state == 'paused':
                    self.lab.run([self.lab.engine,'unpause',self.lab.name(n)], capture=True, timeout=30)
            except Exception as exc:
                errors.append(str(exc))
        for n in j['nodes']:
            try:
                state = self.lab.state(n)
                if state == 'stopped':
                    self.lab.healthy_node()  # Never bootstrap a random node; live majority required.
                    self.lab.comp('up','-d','--no-deps',n,capture=True,timeout=120)
                elif state == 'absent':
                    raise ScenarioError('Container removed externally: ' + n)
            except Exception as exc:
                errors.append(str(exc))
        active = j.get('active_worker')
        if active:
            try:
                self.worker(active['node'], 'cancel', timeout=12, track=False, token=active['token'])
                j['active_worker'] = None
                self.save_journal()
            except Exception as exc:
                errors.append(str(exc))
        # Do not clear the original settings snapshot while a worker may still be alive.
        for n, saved in ([] if j.get('active_worker') else list(j['logging'].items())):
            try:
                self.worker(n,'restore-logging',state=saved,timeout=30,track=False)
                del j['logging'][n]
                self.save_journal()
            except Exception as exc:
                errors.append(str(exc))
        if j['nodes']:
            try:
                for n in NODES: self.lab.wait(n,3)
                self.preflight()
                j['nodes'] = []
                self.save_journal()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise ScenarioError('\n'.join(errors))
        self.journal_path.unlink()
        self.log('[복구] 실습이 변경한 노드 상태/슬로우로그 설정을 복구했습니다.')

    def save_result(self, name, result):
        self.report['results'][name] = result
        private_json(self.directory / (name + '.json'), result)
        self.flush_report()

    def run_data(self, kind, args, fix=False):
        node = 'galera1'
        if kind == 'slow':
            self.remember_logging(node)
            self.log('[슬로우쿼리] 전용 데이터 적재, 0.3초 지연 로그, 인덱스 전후 비교')
        elif kind == 'fragmentation':
            self.log('[단편화] 삽입/80% 삭제/재구축 비교. DDL 동안 클러스터 쓰기가 대기할 수 있습니다.')
        else:
            self.log('[락 대기] galera1의 두 세션으로 재현합니다. 다른 터미널에서 inspect/SQL로 관찰하세요.')
        params = {k:getattr(args,k) for k in ('rows','payload_bytes','leave_broken','hold') if hasattr(args,k)}
        result = self.worker(node, ('fix-' if fix else '') + kind, timeout=900, **params)
        if kind == 'fragmentation':
            result['node_local_final_stats'] = {n:self.worker(n,'table-stats',label='final-' + n) for n in NODES}
            with (self.directory / 'fragmentation.csv').open('w',newline='',encoding='utf8') as f:
                fields = ['stage','exact_rows','payload_bytes','data_bytes','index_bytes','free_extent_bytes','file_logical_bytes','file_allocated_bytes']
                out = csv.DictWriter(f,fieldnames=fields); out.writeheader()
                for s in result['snapshots']:
                    row = {k:s[k] for k in fields if k in s}
                    row.update(file_logical_bytes=s['file']['logical_bytes'],file_allocated_bytes=s['file']['allocated_bytes'])
                    out.writerow(row)
        if kind == 'slow':
            with (self.directory / 'slow-log.tsv').open('w',newline='',encoding='utf8') as f:
                fields = ['start_time','query_time','lock_time','rows_sent','rows_examined','sql_text']
                out=csv.DictWriter(f,fieldnames=fields,delimiter='\t');out.writeheader();out.writerows(result['slow_log'])
        self.save_result(kind, result)
        self.log('[완료] ' + kind + ' 측정값 저장')

    def fault_probe(self, host, run_id, number, phase):
        result = self.worker(host,'probe',timeout=15,track=False,token=run_id,request_id=run_id + '-' + str(number))
        result['phase'] = phase
        result['sampled_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.log('[접속] %-8s %s' % (phase, ('성공 → ' + result['backend']) if result['acknowledged'] else ('미확인/오류 ' + str(result.get('error_code')))), **result)
        return result

    def fault(self, kind, args):
        target = getattr(args,'node','galera1')
        affected = ['galera2','galera3'] if kind == 'quorum' else [target]
        observer = next(n for n in NODES if n not in affected)
        self.worker('galera1','prepare')
        run_id = self.journal['token']
        probes, samples = [], []
        probes.append(self.fault_probe(observer,run_id,0,'before'))
        if not probes[-1]['acknowledged']:
            raise ScenarioError('장애 주입 전 writer 접속 실패: ./lab.sh up / routes로 프록시부터 확인하세요')
        self.journal['nodes'] = affected
        self.save_journal()  # Write BEFORE disruption, including partial command failures.
        self.log('[장애 주입] ' + kind + ': ' + ', '.join(affected))
        started = time.monotonic()
        for n in affected:
            verb = ['kill','--signal','KILL'] if kind == 'node-failure' else ['pause']
            self.lab.run([self.lab.engine] + verb + [self.lab.name(n)],capture=True,timeout=30)
        try:
            deadline = time.monotonic() + args.hold
            first_success = None
            quorum_observed = False
            quorum_write_rejection_observed = False
            while time.monotonic() < deadline:
                p = self.fault_probe(observer,run_id,len(probes),'during')
                probes.append(p)
                if p['acknowledged'] and first_success is None:
                    first_success = round(time.monotonic()-started,3)
                statuses = {n:self.lab.health(n) for n in NODES}
                samples.append(statuses)
                if kind == 'quorum':
                    d = statuses[observer]
                    if d.get('wsrep_cluster_status') == 'non-Primary' and not d.get('ready'):
                        quorum_observed = True
                        if not p['acknowledged']: quorum_write_rejection_observed = True
                time.sleep(min(1,max(0,deadline-time.monotonic())))
        finally:
            # Even Ctrl+C, a failed probe, or a failed pause triggers attempted restoration.
            self.recover_pending()
        after = self.fault_probe(observer,run_id,len(probes),'after')
        probes.append(after)
        # HAProxy rise checks can lag the DB readiness. Give it bounded retries.
        for _ in range(8):
            if probes[-1]['acknowledged']: break
            time.sleep(2)
            probes.append(self.fault_probe(observer,run_id,len(probes),'after'))
        # Read each node with wsrep_sync_wait=1 and compare exact acknowledged request ids.
        stored = {n:self.worker(n,'reconcile',run_id=run_id) for n in NODES}
        observed_sets = {n:{r['request_id'] for r in rows} for n,rows in stored.items()}
        ack = {p['request_id'] for p in probes if p['acknowledged']}
        expected_consistent = len({frozenset(s) for s in observed_sets.values()}) == 1
        for p in probes:
            p['present_after_recovery'] = p['request_id'] in observed_sets['galera1']
        result = {'kind':kind, 'affected_nodes':affected, 'probe_host':observer,
                  'connection_model':'one NEW connection per attempt; no migration of persistent sessions',
                  'initial_writer':probes[0].get('backend'),
                  'initial_writer_was_targeted':probes[0].get('backend') in affected,
                  'backend_change_observed':any(p['phase']=='during' and p['acknowledged'] and p.get('backend') != probes[0].get('backend') for p in probes),
                  'requested_hold_seconds':args.hold,
                  'first_ack_after_fault_seconds':first_success,
                  'probes':probes, 'status_samples':samples,
                  'all_nodes_same_probe_ids':expected_consistent,
                  'acknowledged_requests_present':all(ack <= s for s in observed_sets.values()),
                  'recovered_health':self.preflight()}
        if kind == 'quorum':
            result['non_primary_observed'] = quorum_observed
            result['write_rejection_observed'] = quorum_write_rejection_observed
        result['surviving_backend_ack_observed'] = any(
            p['phase'] == 'during' and p['acknowledged'] and p.get('backend') not in affected
            for p in probes)
        self.save_result(kind,result)
        if not expected_consistent or not result['acknowledged_requests_present'] or not probes[-1]['acknowledged']:
            raise ScenarioError('복구 후 데이터/쓰기 접속 검증 실패. 보고서를 확인하세요.')
        if kind == 'quorum' and not quorum_observed:
            raise ScenarioError('지정 시간 안에 non-Primary가 관찰되지 않았습니다. 재현 성공으로 처리하지 않습니다.')
        if kind == 'quorum' and not quorum_write_rejection_observed:
            raise ScenarioError('non-Primary 상태에서 writer 요청 거부가 관찰되지 않았습니다. 성공으로 처리하지 않습니다.')
        if kind != 'quorum' and not result['surviving_backend_ack_observed']:
            raise ScenarioError('장애 유지 시간 내 생존 노드가 응답한 신규 쓰기가 없습니다. hold/실제 상태를 확인하세요.')
        self.log('[검증] 복구 후 3노드의 요청 ID 일치 및 응답받은 쓰기 보존 확인')

    def execute(self, args):
        if args.scenario == 'repair':
            if not self.journal_path.exists():
                self.log('중단된 시나리오 복구 기록이 없습니다. 테이블 개선은 scenario fix를 사용하세요.')
            else: self.recover_pending()
            return
        if args.scenario == 'inspect':
            self.lab.status()
            try: result = self.worker(self.lab.healthy_node(),'inspect',track=False)
            except ScenarioError as exc: self.log(str(exc)); return
            print(json.dumps(result,ensure_ascii=False,indent=2)); return
        if args.scenario == 'quorum' and not args.confirm_quorum:
            raise ScenarioError('두 노드가 일시 중단됩니다. 실행하려면 --confirm-quorum이 필요합니다.')
        if args.scenario == 'cleanup' and not args.confirm_cleanup:
            raise ScenarioError('incident_lab 삭제에는 --confirm-cleanup이 필요합니다.')
        if getattr(args,'rows',0) * getattr(args,'payload_bytes',0) > 512*1024*1024:
            raise ScenarioError('rows * payload_bytes <= 512 MiB로 제한합니다 (노드당 원시 payload).')
        baseline = self.preflight()
        kind = ('fix-' + args.target) if args.scenario == 'fix' else args.scenario
        self.begin(kind)
        self.report['baseline'] = baseline
        old_term = signal.getsignal(signal.SIGTERM)
        def interrupt(sig, frame): raise KeyboardInterrupt('SIGTERM')
        signal.signal(signal.SIGTERM, interrupt)
        failure = None
        try:
            if args.scenario in ('slow','fragmentation','lock'):
                self.run_data(args.scenario,args)
            elif args.scenario in ('node-failure','node-hang','quorum'):
                self.fault(args.scenario,args)
            elif args.scenario == 'fix':
                self.run_data(args.target,args,fix=True)
            elif args.scenario == 'cleanup':
                self.save_result('cleanup',self.worker('galera1','cleanup',confirm_cleanup=True))
            elif args.scenario == 'demo':
                self.run_data('slow',args)
                self.run_data('fragmentation',args)
                self.fault('node-failure',args)
            self.report['status'] = 'passed'
        except BaseException as exc:
            failure = exc
            self.report['status'] = 'interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed'
            self.report['error'] = str(exc) or type(exc).__name__
        finally:
            # A second Ctrl+C during recovery is not a transactional guarantee; keep the journal.
            try:
                self.recover_pending()
            except BaseException as exc:
                self.report['recovery_error'] = str(exc) or type(exc).__name__
                self.report['status'] = 'recovery-needed'
                if failure is None: failure = exc
            self.report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            self.flush_report()
            signal.signal(signal.SIGTERM,old_term)
            self.log('[결과] ' + self.report['status'] + ' / ' + self.report['directory'])
        if failure:
            raise failure
