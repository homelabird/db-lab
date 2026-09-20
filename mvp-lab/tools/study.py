"""Paired baseline/fault study using existing live controllers. No automatic repair/up.

Run: existing core acceptance, a fresh baseline, then the requested fault. A failed
baseline never authorizes the fault. Compare: bounded local files only, no runtime.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import uuid
from .acceptance import execute, suite_lock
from .measurement import strict_json, digest, capture_context, comparable_context
from .sim_engine import DockerLab
from .simulation import Plan, WORKLOADS
from .study_analysis import PAIRED, LIVE, load_run, compare, render, read_file, objects, require, SIM_ID, EvidenceError


@contextmanager
def study_lock(root):
    import fcntl
    state = root / '.state'; state.mkdir(exist_ok=True, mode=0o700)
    fd = os.open(state / 'study.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('another_paired_study_is_active') from exc
        yield


def command(plan):
    result = ['simulate','run',plan.scenario]
    for key in Plan.__dataclass_fields__:
        if key != 'scenario':
            result += ['--' + key.replace('_','-'),str(getattr(plan,key))]
    return result + ['--yes']


def study_plan(plan):
    plan.validate()
    if plan.scenario not in PAIRED:
        raise ValueError('This scenario changes operation semantics or has no fault; it cannot use a baseline pair')
    baseline = replace(plan, scenario='baseline')
    if baseline.operations() != plan.operations():
        raise ValueError('Paired runs must have the exact same operation plan')
    return {'schema':1, 'runtime_executed':False, 'scenario':plan.scenario,
            'baseline':baseline.document(), 'fault':plan.document(), 'operations_sha256':digest(plan.operations()),
            'steps':[{'name':'core-gate','command':['verify','run','core','--yes']},
                     {'name':'baseline','command':command(baseline)}, {'name':'fault','command':command(plan)}],
            'writes':'core smoke and three workload runs create synthetic orders that remain in the source DB',
            'fault_policy':'only the final paired run injects a fault; failed/blocked/inconclusive predecessors stop the sequence',
            'no_automatic':['pull','build','up','reset','volume deletion','cache flush','reindex','retry failed runs'],
            'measurement_limits':['one sequential pair, not randomized A/B or benchmark',
                                  'source data/cache/host load are not reset between runs',
                                  'actual admitted workflows and request branches can differ'],
            'unsupported':['row-lock','version-race','duplicate-retry','baseline: use individual simulate instead'],
            'network_helper':'prepare separately with mvp drills prepare --yes for network scenarios'}


def core_result(root, detail, previous):
    run_id = detail.get('run_id')
    require(type(run_id) is str and re.fullmatch(r'accept-[a-f0-9]{12}',run_id), 'invalid_core_run_id')
    directory = root / 'reports/acceptance' / run_id
    require(detail.get('report_directory') == str(directory), 'core_report_path_mismatch')
    require(not any(p.is_symlink() for p in (root/'reports',directory.parent,directory)), 'symlinked_core_report')
    require(str(directory.resolve()) not in previous, 'old_core_report_cannot_authorize_new_fault')
    raw, checksum = read_file(directory,'summary.json')
    value = objects(raw)
    expected = ['container-targets','api-runtime-contract','worker-runtime-contract','smoke','simulate-baseline']
    require(value.get('run_id') == run_id and value.get('suite') == 'core' and value.get('status') == 'passed'
            and value.get('evidence_kind') == 'LIVE-ACCEPTANCE-ATTEMPT' and value.get('passed_scenarios') == 1
            and value.get('planned_scenarios') == 1 and not value.get('recovery_markers')
            and [s.get('name') for s in value.get('steps',[])] == expected
            and all(s.get('status') == 'passed' for s in value['steps']), 'core_not_fully_passed')
    return {'run_id':run_id, 'summary_sha256':checksum}


class Study:
    def __init__(self, manage, plan, step_timeout=650, executor=execute):
        self.m, self.plan, self.executor, self.timeout = manage, plan.validate(), executor, step_timeout
        self.document = study_plan(plan)
        if type(step_timeout) is not int or not 120 <= step_timeout <= 1200:
            raise ValueError('step_timeout_must_be_120_to_1200')
        self.directory = manage.ROOT / 'reports/studies' / ('study-' + uuid.uuid4().hex[:12])
        self.directory.mkdir(parents=True,mode=0o700)
        self.summary = {'schema':1, 'run_id':self.directory.name, 'scenario':plan.scenario,
                        'evidence_kind':'LIVE-PAIRED-STUDY-ATTEMPT', 'status':'blocked',
                        'started_at_utc':datetime.now(timezone.utc).isoformat(),
                        'paired_runs_passed':0, 'paired_runs_planned':2,
                        'steps':[{'name':s['name'],'status':'not_run'} for s in self.document['steps']]}
        self.m.atomic_json(self.directory/'plan.json',self.document)
        self.persist()

    def persist(self):
        self.summary['paired_runs_passed'] = sum(s['status'] == 'passed' for s in self.summary['steps'][1:])
        self.m.atomic_json(self.directory/'summary.json',self.summary)
        lines = ['# 정상·장애 짝 실험 실행', '', '판정: **'+self.summary['status']+'**',
                 '증거: LIVE-PAIRED-STUDY-ATTEMPT (실제 완료 단계만 집계)', '',
                 '|단계|상태|종료 코드|', '|---|---|---:|']
        lines += ['|'+r['name']+'|'+r['status']+'|'+str(r.get('returncode','—'))+'|' for r in self.summary['steps']]
        lines += ['', '첫 실패에서 중단합니다. 앞선 실행의 성공을 재사용하지 않으며 원본 데이터를 초기화하지 않습니다.',
                  '비교가 만들어졌다면 comparison/report.html을 확인하세요. 없으면 그 단계에 도달하지 않은 것입니다.']
        (self.directory/'report.md').write_text('\n'.join(lines)+'\n'); (self.directory/'report.md').chmod(0o600)
        html = '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'"><title>DB Lab study</title><body style="font:16px/1.7 system-ui;max-width:900px;margin:32px auto;padding:20px"><h1>정상·장애 짝 실험</h1><p>'+escape(self.summary['status'])+'</p><pre>'+escape('\n'.join(lines))+'</pre>'
        if (self.directory/'comparison/report.html').is_file():
            html += '<p><a href="comparison/report.html">요청·지연·복구 비교 보고서 열기</a></p>'
        html += '</body></html>'
        (self.directory/'report.html').write_text(html); (self.directory/'report.html').chmod(0o600)

    def invoke(self, index, argv):
        row = self.summary['steps'][index]
        row['status'] = 'running'; self.persist()
        result = self.executor([sys.executable,str(self.m.ROOT/'tools/manage.py'),*argv],
                               cwd=self.m.ROOT,env=dict(os.environ),timeout=self.timeout)
        row.update({k:result[k] for k in ('returncode','elapsed_seconds','timed_out','stderr_bytes')})
        # No raw stdout/stderr or arbitrary error message is copied into the study bundle.
        self.m.atomic_json(self.directory/(row['name']+'-execution.json'),{k:v for k,v in result.items() if k != 'stdout'})
        if result.get('interrupted'):
            row['status'] = 'aborted'; raise KeyboardInterrupt()
        if result.get('timed_out'):
            row['status'] = 'failed'; raise RuntimeError('paired_step_budget_exceeded')
        try:
            detail = strict_json(result['stdout'])
            require(type(detail) is dict, 'child_object_required')
        except (ValueError,TypeError) as exc:
            row['status'] = 'failed'; raise RuntimeError('invalid_child_evidence') from exc
        if result['returncode'] != 0:
            row['status'] = 'blocked' if result['returncode'] == 127 else 'inconclusive' if detail.get('status') == 'inconclusive' else 'failed'
            # Keep a validated run ID for manual investigation, never a free-form path.
            rid = detail.get('run_id','')
            if type(rid) is str and re.fullmatch(SIM_ID,rid):
                row['run_id'] = rid
            raise RuntimeError('paired_predecessor_not_passed')
        return detail

    def check_reference(self, reference):
        with self.m.lock():
            self.m.require_no_active_fault()
            config = self.m.validate(self.m.parse_env(self.m.ROOT / '.env'))
            compose = self.m.Compose(config)
            lab = DockerLab(compose, self.m)
            current = capture_context(self.m.ROOT, compose, lab.preflight())
            require(comparable_context(current) == comparable_context(reference.context['before']),
                    'context_changed_before_fault_no_fault_was_started')

    def run(self):
        index = 0
        previous_signal = signal.getsignal(signal.SIGTERM)
        def interrupt(*_):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM,interrupt)
        try:
            with study_lock(self.m.ROOT):
                if not shutil.which('docker'):
                    raise FileNotFoundError('Docker missing')
                self.m.require_no_active_fault()
                previous = {str(p.resolve()) for p in (self.m.ROOT/'reports/acceptance').glob('accept-*')}
                detail = self.invoke(0,self.document['steps'][0]['command'])
                self.summary['steps'][0]['proof'] = core_result(self.m.ROOT,detail,previous)
                self.summary['steps'][0]['status'] = 'passed'; self.persist()
                paired = []
                # Existing acceptance uses the same lock; do not hold it around the core child.
                with suite_lock(self.m.ROOT):
                    for index in (1,2):
                        self.m.require_no_active_fault()
                        if index == 2:
                            self.check_reference(paired[0])
                        previous = {p.name for p in (self.m.ROOT/'reports/simulations').glob('sim-*')}
                        detail = self.invoke(index,self.document['steps'][index]['command'])
                        rid = detail.get('run_id')
                        require(rid not in previous, 'old_simulation_cannot_count_as_new_pair')
                        require(detail.get('status') == 'passed' and detail.get('report') == str(self.m.ROOT/'reports/simulations'/str(rid)/'report.md'), 'child_report_pointer_mismatch')
                        run = load_run(self.m.ROOT,rid)
                        expected = self.document['baseline' if index == 1 else 'fault']
                        require(run.plan == expected and run.summary['status'] == 'passed' and run.summary['evidence_kind'] == LIVE,
                                'child_plan_status_or_evidence_mismatch')
                        require(run.context.get('available') is True and run.context.get('stable') is True, 'child_context_not_stable')
                        self.summary['steps'][index].update(status='passed',run_id=rid,input_sha256=run.hashes)
                        paired.append(run); self.persist()
                    result = compare(*paired)
                    render(self.directory/'comparison',result)
                    self.summary['comparison_status'] = result['comparison_status']
                    self.summary['status'] = 'passed' if result['comparison_status'] == 'complete' else 'inconclusive'
        except FileNotFoundError:
            self.summary['steps'][index].update(status='blocked',returncode=127,reason='required_runtime_or_file_missing')
            self.summary['status'] = 'blocked'
        except KeyboardInterrupt:
            self.summary['steps'][index]['status'] = 'aborted'; self.summary['status'] = 'aborted'
        except (Exception,) as exc:
            row = self.summary['steps'][index]
            if row['status'] not in {'blocked','failed','inconclusive','aborted'}:
                row['status'] = 'failed'
            row['reason'] = str(exc) if isinstance(exc,EvidenceError) else type(exc).__name__
            self.summary['status'] = row['status']
        finally:
            signal.signal(signal.SIGTERM,previous_signal)
            self.summary['ended_at_utc'] = datetime.now(timezone.utc).isoformat()
            self.summary['recovery_markers'] = [p.name for p in (self.m.ROOT/'.state').glob('*-active.json')]
            if self.summary['recovery_markers'] and self.summary['status'] == 'passed':
                self.summary['status'] = 'failed'
            self.persist()
        print(json.dumps({'status':self.summary['status'],'run_id':self.directory.name,'report_directory':str(self.directory),
                          'paired_runs_passed':self.summary['paired_runs_passed'],'paired_runs_planned':2},ensure_ascii=False,indent=2))
        return 127 if self.summary['steps'][0].get('returncode') == 127 else {'passed':0,'failed':1,'blocked':1,'inconclusive':2,'aborted':130}[self.summary['status']]


def add_parser(sub):
    parser = sub.add_parser('study',help='Paired normal/fault observation and strict read-only report comparison')
    actions = parser.add_subparsers(dest='study_action',required=True)
    actions.add_parser('list')
    p = actions.add_parser('compare',help='Read two v8 run IDs; no HTTP, Docker, repair or data writes')
    p.add_argument('baseline_id'); p.add_argument('fault_id')
    for action in ('plan','run'):
        p = actions.add_parser(action); p.add_argument('scenario',choices=PAIRED)
        defaults = Plan()
        for key in ('seed','seconds','workers','fault_at','fault_for','recovery_timeout'):
            p.add_argument('--'+key.replace('_','-'),type=int,default=getattr(defaults,key))
        p.add_argument('--rate',type=float,default=defaults.rate)
        p.add_argument('--workload',choices=WORKLOADS,default=defaults.workload)
        if action == 'run':
            p.add_argument('--yes',action='store_true',required=True)
            p.add_argument('--step-timeout',type=int,default=650)


def cli(args,manage):
    if args.study_action == 'list':
        print(json.dumps({'paired_scenarios':PAIRED,'new_fault_types':0,'core_gate_required':True},ensure_ascii=False,indent=2)); return 0
    if args.study_action == 'compare':
        result = compare(load_run(manage.ROOT,args.baseline_id),load_run(manage.ROOT,args.fault_id))
        directory = manage.ROOT/'reports/comparisons'/('compare-'+uuid.uuid4().hex[:12])
        render(directory,result)
        print(json.dumps({'comparison_status':result['comparison_status'],'report_directory':str(directory)},ensure_ascii=False,indent=2))
        return {'complete':0,'not_comparable':2,'test_only':2,'fault_not_recovered':1}[result['comparison_status']]
    plan = Plan(**{key:getattr(args,key) for key in Plan.__dataclass_fields__})
    if args.study_action == 'plan':
        print(json.dumps(study_plan(plan),ensure_ascii=False,indent=2)); return 0
    return Study(manage,plan,args.step_timeout).run()
