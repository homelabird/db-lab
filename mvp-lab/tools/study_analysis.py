"""Read-only paired evidence analysis. No DB connection, repair, commands, or imports from uploads.

The HTML is a static escaped report, not a server or executable dashboard. Numerical
comparability is not a causal/statistical/HA certification. Old runs lacking v8 clocks
and context are rejected rather than filled with invented observations.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import dataclass
from html import escape
import hashlib
import json
import math
import os
from pathlib import Path
import re
from .measurement import strict_json, digest, comparable_context
from .simulation import Plan, SCENARIOS, request_summary, percentiles

SIM_ID = r'sim-[a-f0-9]{12}'
LIVE = 'real-http-and-docker-actions'
PAIRED = tuple(name for name, (action, _, _) in SCENARIOS.items() if action and name != 'row-lock')
CONTEXT_KEYS = ('engine_sha256', 'source_sha256', 'config_sha256', 'topology_sha256', 'client_python', 'client_platform')
OPERATIONS = {'identity', 'create', 'prime_cache', 'source_lookup', 'read_only_comparison', 'diagnostics',
              'update', 'detail', 'search', 'terminal_read'}
ERROR_CODES = {'TimeoutError','OSError','ConnectionRefusedError','ConnectionResetError','RemoteDisconnected',
               'HTTPException','ValueError','BadStatusLine','IncompleteRead','client_concurrency_timeout',
               'mariadb_authentication','mariadb_lock_wait_timeout','mariadb_deadlock','mariadb_unavailable',
               'elasticsearch_unavailable','redis_unavailable','kafka_unavailable','version_conflict'}
STATES = {'acknowledged_present', 'acknowledged_missing', 'unacknowledged_absent', 'present_without_ack',
          'unresolved', 'not_observed_before_deadline'}
FILES = ('summary.json', 'plan.json', 'comparison-context.json', 'runtime-snapshot.json', 'requests.jsonl', 'timeline.jsonl')
LIMITS = [
    '단일 정상→장애 순차 관측입니다. 무작위 대조시험·성능 우열·통계적 유의성·RTO/RPO 판정이 아닙니다.',
    '동일 seed는 계획된 입력을 맞춥니다. 실제 스케줄·DB 크기·캐시 상태·CPU/디스크 경합은 같게 만들지 않습니다.',
    '계획 슬롯 누락과 타임아웃을 따로 표시합니다. 성공 응답만 빨라져도 시스템이 개선됐다는 뜻이 아닙니다.',
    '전체 지연은 클라이언트 관측이며 transport도 서버 SQL 시간만이 아닙니다. 관측 요청 자체도 부하를 만듭니다.',
    '장애 경계를 걸친 요청은 boundary로 분리합니다. 오류 총계에서는 빼지 않습니다.',
    '대기·통신 지연의 중앙값은 각각 계산합니다. 두 중앙값의 합이 전체 중앙값과 같을 필요는 없습니다.',
    'p50 차이는 각 5개, p95 차이는 각 20개, p99 차이는 각 100개 이상에서만 표시합니다. 이는 표시 정책이지 신뢰수준이 아닙니다.',
    '보고된 해시·runtime 표지는 서명이나 독립적인 DB 실행 증명이 아닙니다. 원본 로그를 새로 실행하지 않습니다.',
]


class EvidenceError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def number(value, code, maximum=1e12):
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum, code)
    return value


def integer(value, code, maximum=100000):
    require(type(value) is int and 0 <= value <= maximum, code)
    return value


def read_file(base: Path, name: str, limit=16 * 1024**2) -> tuple[bytes, str]:
    path = base / name
    require(not path.is_symlink() and path.is_file(), 'missing_or_symlinked_' + name.replace('.', '_'))
    require(path.resolve().parent == base.resolve(), 'evidence_path_escape')
    require(path.stat().st_size <= limit, 'evidence_size_limit')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, 'evidence_size_limit')
    return raw, hashlib.sha256(raw).hexdigest()


def objects(raw, lines=False):
    try:
        if not lines:
            return strict_json(raw)
        parts = raw.splitlines()
        require(len(parts) <= 20000 and all(parts), 'jsonl_record_limit_or_blank')
        result = [strict_json(part) for part in parts]
        require(all(type(x) is dict for x in result), 'jsonl_object_required')
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise EvidenceError('invalid_structured_evidence') from exc


@dataclass
class Run:
    run_id: str
    summary: dict
    plan: dict
    context: dict
    snapshot: list
    requests: list
    timeline: list
    hashes: dict

    def origin(self):
        rows = [r for r in self.timeline if r.get('stage') == 'workload_started']
        require(len(rows) == 1, 'workload_origin_missing_or_duplicate')
        return number(rows[0].get('clock_origin_seconds'), 'invalid_workload_origin', 100000)


def load_run(root: Path, run_id: str) -> Run:
    require(type(run_id) is str and re.fullmatch(SIM_ID, run_id), 'invalid_simulation_id')
    reports = root / 'reports'
    family = reports / 'simulations'
    base = family / run_id
    require(all(not p.is_symlink() for p in (reports, family, base)), 'symlinked_report_directory')
    require(base.resolve().parent == family.resolve() and root.resolve() in base.resolve().parents, 'evidence_path_escape')
    loaded, hashes = {}, {}
    for name in FILES:
        raw, hashes[name] = read_file(base, name)
        loaded[name] = objects(raw, name.endswith('.jsonl'))
    run = Run(run_id, loaded['summary.json'], loaded['plan.json'], loaded['comparison-context.json'],
              loaded['runtime-snapshot.json'], loaded['requests.jsonl'], loaded['timeline.jsonl'], hashes)
    validate_run(run)
    return run


def validate_run(run: Run):
    s, p = run.summary, run.plan
    require(type(s) is dict and type(p) is dict and type(run.context) is dict, 'object_required')
    require(s.get('schema') == 1 and s.get('measurement_schema') == 2, 'v8_measurement_required_rerun_not_backfill')
    require(s.get('run_id') == run.run_id and s.get('scenario') == p.get('scenario'), 'run_identity_mismatch')
    require(s.get('status') in {'passed', 'failed', 'aborted', 'inconclusive'}, 'invalid_run_status')
    kind = s.get('evidence_kind')
    require(type(kind) is str and (kind == LIVE or kind.startswith('TEST-')), 'unrecognized_evidence_kind')
    try:
        plan = Plan(**{k: p[k] for k in Plan.__dataclass_fields__}).validate()
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceError('invalid_recorded_plan') from exc
    require(p.get('operations') == plan.operations(), 'recorded_plan_operations_mismatch')
    require(type(run.timeline) is list and type(run.requests) is list and all(type(r) is dict for r in run.timeline + run.requests), 'record_objects_required')
    convergence = s.get('observed_convergence_seconds_after_restore')
    if convergence is not None:
        number(convergence, 'invalid_convergence_seconds', 100000)
    for row in run.timeline:
        number(row.get('elapsed_seconds'), 'invalid_timeline_clock', 100000)
    for row in run.requests:
        require(row.get('scope') in {'setup', 'workload', 'observer', 'audit'}, 'invalid_request_scope')
        require(row.get('operation') in OPERATIONS, 'invalid_request_operation')
        status = integer(row.get('status'), 'invalid_http_status', 599)
        require(status == 0 or 100 <= status <= 599, 'invalid_http_status')
        expected = 'success' if 200 <= status < 300 else 'conflict' if status == 409 else 'client_unknown' if status == 0 else 'error'
        require(row.get('outcome') == expected, 'outcome_disagrees_with_status')
        start = number(row.get('request_started_seconds'), 'request_clock_missing_or_invalid', 100000)
        finish = number(row.get('request_finished_seconds'), 'request_clock_missing_or_invalid', 100000)
        require(finish >= start, 'request_clock_inverted')
        elapsed = number(row.get('elapsed_seconds'), 'invalid_request_record_clock', 100000)
        require(elapsed + .01 >= finish, 'request_record_before_finish')
        latency = number(row.get('latency_ms'), 'invalid_request_latency', 1e8)
        # Transport wrappers do not include journal bookkeeping; allow 25ms of recording overhead.
        require(latency <= (finish - start) * 1000 + 25, 'latency_exceeds_observed_call')
        queue = row.get('client_queue_ms'); transport = row.get('transport_ms'); attempted = row.get('transport_attempted')
        if queue is not None:
            number(queue, 'invalid_client_queue_ms', 1e8)
            require(queue <= latency + .01, 'client_queue_exceeds_total')
        if transport is not None:
            number(transport, 'invalid_transport_ms', 1e8)
            require(queue is not None and attempted is True and abs(queue + transport - latency) <= .02, 'latency_components_disagree')
        if attempted is False:
            require(status == 0 and transport is None, 'unsent_request_reported_as_http_success')
        if row['scope'] == 'workload':
            n = integer(row.get('workflow_number'), 'workflow_correlation_missing', len(plan.operations()) - 1)
            require(n < len(plan.operations()), 'workflow_outside_plan')
    workload = [r for r in run.requests if r['scope'] == 'workload']
    require(integer(s.get('workload_http_requests'), 'invalid_workload_count', 2400) == len(workload), 'request_count_disagrees_with_log')
    # Recompute statistics; never trust a hand-edited p95/summary PASS.
    require(s.get('request_statistics') == request_summary(run.requests), 'summary_statistics_disagree_with_log')
    admitted = [r.get('operation_number') for r in run.timeline if r.get('stage') == 'workflow_admitted']
    require(all(type(n) is int and 0 <= n < len(plan.operations()) for n in admitted) and len(set(admitted)) == len(admitted), 'invalid_workflow_admission')
    require(integer(s.get('admitted_workflows'), 'invalid_admitted_count', 400) == len(admitted), 'admission_count_disagrees_with_log')
    skipped = integer(s.get('skipped_workflows'), 'invalid_skip_count', 400)
    require(len(admitted) + skipped <= len(plan.operations()), 'workflow_budget_exceeded')
    require(all(r['workflow_number'] in admitted for r in workload), 'request_without_workflow_admission')
    finished = [r for r in run.timeline if r.get('stage') == 'workflow_finished']
    require(all(type(r.get('operation_number')) is int and r['operation_number'] in admitted and r.get('outcome') in {'completed','failed','cancelled'} for r in finished), 'invalid_workflow_completion')
    require(len({r['operation_number'] for r in finished}) == len(finished), 'duplicate_workflow_completion')
    final = s.get('final_consistency', {})
    require(type(final) is dict and type(final.get('rows', [])) is list and all(type(r) is dict for r in final.get('rows', [])), 'invalid_final_consistency')
    if s['status'] == 'passed':
        require(admitted and workload, 'passed_without_workload')
        require(len(finished) == len(admitted) and all(r['outcome'] == 'completed' for r in finished), 'passed_with_unfinished_workflow')
        final = s.get('final_consistency', {})
        rows = final.get('rows', [])
        require(s.get('fault_restored') is True and s.get('fault_effect_observed') is True and s.get('final_dependencies_reachable') is True,
                'passed_without_effect_recovery_and_dependencies')
        require(final.get('consistent') is True and type(final.get('stable_rounds')) is int and final['stable_rounds'] >= 2
                and rows and all(r.get('consistent') is True and r.get('state') in {'acknowledged_present', 'present_without_ack', 'unacknowledged_absent'} for r in rows)
                and not s.get('contract_errors'), 'passed_with_inconsistent_or_missing_orders')
        require(len(admitted) + skipped == len(plan.operations()), 'unaccounted_planned_workflows')
    context = run.context
    if context.get('available') is True:
        for side in ('before', 'after'):
            value = context.get(side)
            if value is None and s['status'] != 'passed':
                continue
            require(type(value) is dict and all(k in value for k in CONTEXT_KEYS), 'context_missing_fields')
            require(all(type(value[k]) is str and re.fullmatch(r'[a-f0-9]{64}', value[k]) for k in CONTEXT_KEYS[:4]), 'invalid_context_digest')
            require(type(value.get('source_files')) is dict and digest(value['source_files']) == value['source_sha256'], 'source_fingerprint_mismatch')
        if context.get('after') is not None:
            equal = comparable_context(context['before']) == comparable_context(context['after'])
            require(context.get('stable') is equal, 'context_stability_claim_mismatch')
        states = run.snapshot
        require(type(states) is list and len(states) == 6 and {x.get('service') for x in states if type(x) is dict} == {'mariadb','kafka','elasticsearch','redis','api','worker'}, 'invalid_topology_snapshot')
        try:
            topology = [{k: state[k] for k in ('service','image_id','volumes')} for state in sorted(states, key=lambda x: x['service'])]
        except (KeyError, TypeError) as exc:
            raise EvidenceError('invalid_topology_snapshot') from exc
        require(digest(topology) == context['before']['topology_sha256'], 'topology_fingerprint_mismatch')
    return run


def events(run, stage):
    return sorted((r for r in run.timeline if r.get('stage') == stage), key=lambda x: x['elapsed_seconds'])


def aligned_windows(fault: Run) -> list[dict]:
    origin = fault.origin()
    times = []
    for stage in ('fault_requested','fault_applied','fault_restore_requested','fault_restore'):
        rows = events(fault, stage)
        require(len(rows) == 1, 'fault_timeline_missing_or_duplicate')
        times.append(rows[0]['elapsed_seconds'] - origin)
    end = fault.plan['seconds']
    require(0 <= times[0] <= times[1] <= times[2] <= times[3] <= end, 'fault_outside_paired_workload_window')
    bounds = [0, *times, end]
    return [{'name': name, 'start_seconds': round(a, 6), 'end_seconds': round(b, 6)}
            for name, a, b in zip(('before','apply_transition','fault','restore_transition','after'), bounds, bounds[1:])]


def classify(row, origin, windows):
    start, finish = row['request_started_seconds'] - origin, row['request_finished_seconds'] - origin
    for window in windows:
        if window['start_seconds'] <= start < window['end_seconds']:
            return window['name'] + (':boundary' if finish > window['end_seconds'] else '')
    return 'outside_window'


def workload_metrics(run: Run, windows: list[dict]) -> dict:
    origin = run.origin()
    rows = [r for r in run.requests if r['scope'] == 'workload']
    grouped = defaultdict(list)
    for row in rows:
        grouped[(classify(row, origin, windows), row['operation'], row['outcome'])].append(row)
    groups = []
    for (window, operation, outcome), records in sorted(grouped.items()):
        total = percentiles([r['latency_ms'] for r in records])
        groups.append({'window': window, 'operation': operation, 'outcome': outcome, **total,
                       'queue': percentiles([r['client_queue_ms'] for r in records if r.get('client_queue_ms') is not None]),
                       'transport': percentiles([r['transport_ms'] for r in records if r.get('transport_ms') is not None]),
                       'transport_not_attempted': sum(r.get('transport_attempted') is False for r in records),
                       'small_sample': len(records) < 20})
    finished = [r for r in run.timeline if r.get('stage') == 'workflow_finished']
    require(len({r.get('operation_number') for r in finished}) == len(finished), 'duplicate_workflow_completion')
    planned = len(run.plan['operations'])
    admitted = run.summary['admitted_workflows']
    diagnostics = [r.get('data', {}).get('dependencies', {}) for r in events(run, 'diagnostics')]
    def peak(dependency, key):
        values = [d.get(dependency, {}).get(key) for d in diagnostics if d.get(dependency, {}).get('reachable') is True]
        values = [v for v in values if type(v) is int and 0 <= v <= 10**12]
        return {'observations': len(values), 'sampled_maximum': max(values) if values else None}
    counts = Counter(r['outcome'] for r in rows)
    failures = Counter(r.get('error') if type(r.get('error')) is str and r['error'] in ERROR_CODES else 'unclassified'
                       for r in rows if r['outcome'] in {'error','client_unknown'})
    errors = dict(failures)
    final_rows = run.summary.get('final_consistency', {}).get('rows', [])
    states = Counter(r.get('state') if r.get('state') in STATES else 'unclassified' for r in final_rows)
    return {'run_id': run.run_id, 'scenario': run.plan['scenario'], 'run_status': run.summary['status'],
            'planned_workflows': planned, 'admitted_workflows': admitted, 'skipped_workflows': run.summary['skipped_workflows'],
            'completed_workflows': sum(r.get('outcome') == 'completed' for r in finished),
            'unfinished_workflows': max(0, admitted - len(finished)),
            'admission_percent': round(admitted / planned * 100, 3), 'http_requests': len(rows),
            'outcomes': {k: counts[k] for k in ('success','conflict','error','client_unknown')},
            'error_or_unknown_percent': round((counts['error'] + counts['client_unknown']) / len(rows) * 100, 3) if rows else None,
            'boundary_requests': sum(':boundary' in classify(r, origin, windows) or classify(r, origin, windows) == 'outside_window' for r in rows),
            'nonworkload_requests': len(run.requests) - len(rows), 'groups': groups, 'error_codes': errors,
            'outbox': peak('mariadb','outbox_pending'), 'kafka_lag': peak('kafka','lag'),
            'final_order_states': dict(states), 'final_consistency': run.summary.get('final_consistency', {}).get('consistent') is True,
            'fault_restored': run.summary.get('fault_restored') is True,
            'observed_convergence_seconds_after_restore': run.summary.get('observed_convergence_seconds_after_restore')}


def compare(left: Run, right: Run) -> dict:
    validate_run(left); validate_run(right)
    require(left.run_id != right.run_id, 'two_distinct_runs_required')
    require(left.plan['scenario'] == 'baseline' and right.plan['scenario'] in PAIRED, 'baseline_and_supported_fault_required')
    blockers, warnings = [], []
    if left.plan['operations'] != right.plan['operations']:
        blockers.append('planned_operations_differ')
    for key in ('seed','seconds','rate','workers','workload','fault_at','fault_for','recovery_timeout'):
        if left.plan[key] != right.plan[key]:
            blockers.append('plan_' + key + '_differs')
    for run, side in ((left,'baseline'), (right,'fault')):
        if run.context.get('available') is not True or run.context.get('stable') is not True:
            blockers.append(side + '_context_missing_or_changed')
    if all(r.context.get('available') is True for r in (left,right)):
        for key in CONTEXT_KEYS:
            if left.context['before'][key] != right.context['before'][key]:
                blockers.append(key + '_differs')
    if left.summary['status'] != 'passed':
        blockers.append('baseline_not_passed')
    live = all(r.summary['evidence_kind'] == LIVE for r in (left,right))
    if not live:
        warnings.append('TEST_ONLY_NOT_LIVE_DATABASE_EVIDENCE')
    try:
        windows = aligned_windows(right)
        left.origin()
    except EvidenceError as exc:
        blockers.append(str(exc))
        windows = [{'name':'whole_workload', 'start_seconds':0, 'end_seconds':min(left.plan['seconds'],right.plan['seconds'])}]
    try:
        baseline, fault = workload_metrics(left, windows), workload_metrics(right, windows)
    except EvidenceError:
        # An incomplete run without a load origin is not a completed comparison.
        raise
    if baseline['skipped_workflows'] or fault['skipped_workflows']:
        warnings.append('admission_slots_skipped_do_not_compare_success_latency_alone')
    if baseline['http_requests'] != fault['http_requests']:
        warnings.append('actual_request_counts_differ_even_with_same_plan')
    if baseline['boundary_requests'] or fault['boundary_requests']:
        warnings.append('boundary_requests_excluded_from_pure_window_deltas_but_retained_in_totals')
    a = {(g['window'],g['operation'],g['outcome']):g for g in baseline['groups']}
    b = {(g['window'],g['operation'],g['outcome']):g for g in fault['groups']}
    deltas = []
    for key in sorted(set(a) | set(b)):
        if ':' in key[0] or key[0] in {'outside_window','apply_transition','restore_transition'}:
            continue
        first, second = a.get(key), b.get(key)
        record = {'window':key[0], 'operation':key[1], 'outcome':key[2],
                  'baseline_n':first['count'] if first else 0, 'fault_n':second['count'] if second else 0}
        for percentile, minimum in ((50,5),(95,20),(99,100)):
            field = f'p{percentile}_ms'
            ok = not blockers and first and second and min(first['count'],second['count']) >= minimum
            record['delta_' + field] = round(second[field] - first[field],3) if ok else None
        deltas.append(record)
    comparable = not blockers
    status = ('not_comparable' if blockers else 'test_only' if not live else
              'complete' if right.summary['status'] == 'passed' else 'fault_not_recovered')
    return {'schema':1, 'comparison_status':status, 'conditions_match':comparable,
            'evidence_kind':'PAIRED-DECLARED-LIVE-REPORTS' if live else 'TEST-ONLY-PAIRED-REPORTS-NOT-DB-BENCHMARK',
            'not_an_independent_runtime_attestation':True,
            'baseline_id':left.run_id, 'fault_id':right.run_id,
            'operation_plan_sha256':digest(left.plan['operations']), 'blockers':blockers, 'warnings':warnings,
            'windows':windows, 'baseline':baseline, 'fault':fault, 'deltas':deltas,
            'input_sha256':{'baseline':left.hashes, 'fault':right.hashes}, 'limits':LIMITS}


def render(directory: Path, result: dict):
    """Only allowlisted/derived values reach the static report; raw logs are never embedded."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    def save(name, content):
        path = directory / name
        require(not path.exists() and not path.is_symlink(), 'report_output_already_exists')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
    save('comparison.json', json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    title = '정상 / 장애 실행 비교'
    left, right = result['baseline'], result['fault']
    rows = []
    for label,key in [('계획 workflow','planned_workflows'),('수용 workflow','admitted_workflows'),('건너뛴 workflow','skipped_workflows'),
                      ('완료 workflow','completed_workflows'),('미완료 workflow','unfinished_workflows'),('HTTP 요청','http_requests'),
                      ('오류·결과불명 비율 (%)','error_or_unknown_percent'),('경계를 걸친 요청','boundary_requests'),('관측·준비·대조 요청','nonworkload_requests')]:
        rows.append((label, left[key], right[key]))
    for key,label in [('success','성공'),('conflict','409 충돌'),('error','HTTP 오류'),('client_unknown','클라이언트 결과불명')]:
        rows.append((label,left['outcomes'][key],right['outcomes'][key]))
    def text(value):
        return '—' if value is None else str(value)
    md = [f'# {title}', '', f"비교 판정: **{result['comparison_status']}**", f"증거: `{result['evidence_kind']}`", '',
          f"정상 `{result['baseline_id']}` / 장애 `{result['fault_id']}`", '', '## 비교 조건', '',
          '차단: ' + (', '.join(result['blockers']) or '없음'), '주의: ' + (', '.join(result['warnings']) or '아래 해석 제한 참조'), '',
          '| 지표 | 정상 | 장애 |', '|---|---:|---:|', *['|'+ '|'.join(text(v) for v in row)+'|' for row in rows], '',
          '## 같은 작업·결과·시간 구간의 지연 차이', '', '단위 ms, 장애 − 정상. 표본 부족/조건 불일치는 — 입니다.', '',
          '|구간|작업|결과|정상 n|장애 n|Δ p50|Δ p95|Δ p99|', '|---|---|---|---:|---:|---:|---:|---:|']
    delta_rows = [tuple(r[k] for k in ('window','operation','outcome','baseline_n','fault_n','delta_p50_ms','delta_p95_ms','delta_p99_ms')) for r in result['deltas']]
    delta_rows = [tuple(row) for row in delta_rows]
    md += ['|'+'|'.join(text(v) for v in row)+'|' for row in delta_rows]
    md += ['', '## 복구', '', f"최종 대조: 정상={left['final_consistency']} / 장애={right['final_consistency']}",
           f"장애 복원 확인={right['fault_restored']}", '이 값은 해당 실행의 주문 범위에 한정하며 영구 유실 여부를 단정하지 않습니다.', '',
           '## 해석 제한', '', *LIMITS]
    save('report.md', '\n'.join(md)+'\n')
    def table(headers, data):
        return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+escape(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join(
            '<tr>'+''.join('<td>'+escape(text(v))+'</td>' for v in row)+'</tr>' for row in data)+'</tbody></table></div>'
    status = escape(result['comparison_status'])
    notice = '<p class="notice"><strong>'+escape(result['evidence_kind'])+'</strong><br>표본과 조건을 확인하는 학습용 비교입니다. 성능 우열·HA·RTO/RPO 인증이 아닙니다.</p>'
    html = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>DB Lab — 정상 / 장애 비교</title><style>
body{overflow-wrap:anywhere;font:15px/1.7 system-ui,sans-serif;margin:0;background:#f4f6fa;color:#17233b}main{max-width:1100px;margin:auto;padding:32px 24px}
h1{font-size:32px;line-height:1.2;margin:12px 0}h2{margin:28px 0 12px;font-size:21px}.eyebrow{font-size:12px;letter-spacing:2px;color:#526989}
section{background:#fff;border:1px solid #dce3ee;border-radius:12px;padding:24px;margin:20px 0}.notice{padding:16px;border-left:4px solid #476aa0;background:#edf2fa}
code{overflow-wrap:anywhere}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:10px 12px;text-align:right;border-bottom:1px solid #e5eaf1;white-space:nowrap}td:first-child,th:first-child{text-align:left}th{background:#edf2f8}
.state{display:inline-block;padding:4px 12px;background:#e5eaf3;border-radius:20px}li{margin:10px 0}.muted{color:#5b6980}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:650px){main{padding:18px 12px}.grid{grid-template-columns:1fr}section{padding:16px}h1{font-size:27px}}</style></head><body><main>'''
    html += '<div class="eyebrow">DB LAB / COMPARATIVE STUDY</div><h1>'+title+'</h1><span class="state">'+status+'</span>'+notice
    html += '<p class="muted">정상 <code>'+escape(result['baseline_id'])+'</code> → 장애 <code>'+escape(result['fault_id'])+'</code></p>'
    html += '<section><h2>01. 비교 조건</h2><p>차단: '+escape(', '.join(result['blockers']) or '없음')+'</p><p>주의: '+escape(', '.join(result['warnings']) or '아래 해석 제한 참조')+'</p></section>'
    html += '<section><h2>02. 요청을 빠뜨리지 않고 비교</h2>'+table(('지표','정상','장애'),rows)+'</section>'
    html += '<section><h2>03. 같은 시간 구간·작업·결과의 지연</h2><p class="muted">장애 − 정상 (ms). — 는 0이 아니라 비교 조건 또는 표본 부족입니다. 경계를 걸친 요청은 총계에 남기고 구간별 차이에서는 제외합니다.</p>'+table(('구간','작업','결과','정상 n','장애 n','Δ p50','Δ p95','Δ p99'),delta_rows)+'</section>'
    for name, metric in (('정상',left),('장애',right)):
        group_rows=[(g['window'],g['operation'],g['outcome'],g['count'],g['p50_ms'],g['queue']['p50_ms'],g['transport']['p50_ms']) for g in metric['groups']]
        html += '<section><h2>04. '+name+' 원시 요청 분류</h2>'+table(('구간','작업','결과','n','전체 p50','대기 p50','통신 p50'),group_rows)+'</section>'
    html += '<section><h2>05. 오류 분류</h2><p>허용된 오류 코드만 표시합니다. 자유 형식 오류나 연결 주소는 복사하지 않습니다.</p>'+table(('오류 코드','정상','장애'),[(code,left['error_codes'].get(code,0),right['error_codes'].get(code,0)) for code in sorted(set(left['error_codes']) | set(right['error_codes']))])+'</section>'
    html += '<section><h2>06. 복구와 처리 대기</h2>'+table(('확인','정상','장애'),[
        ('실험 자체 판정',left['run_status'],right['run_status']),('최종 주문 대조',left['final_consistency'],right['final_consistency']),
        ('관측 outbox 최대',left['outbox']['sampled_maximum'],right['outbox']['sampled_maximum']),
        ('관측 Kafka lag 최대',left['kafka_lag']['sampled_maximum'],right['kafka_lag']['sampled_maximum']),
        ('복원 후 최종 대조 관측(초)',left['observed_convergence_seconds_after_restore'],right['observed_convergence_seconds_after_restore'])])+'<p>위 최대값은 표본에서 본 값입니다. 수집 실패는 — 이며 실제 전체 최대나 유실 주문 수가 아닙니다.</p></section>'
    html += '<section><h2>해석 제한</h2><ul>'+''.join('<li>'+escape(x)+'</li>' for x in LIMITS)+'</ul></section></main></body></html>'
    save('report.html', html)
