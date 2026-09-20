"""Run two controlled implementations on REAL isolated MariaDB; emit audited evidence.

No automatic SQLite/memory fallback. Only tests supply a repository double. These
experiments model a LOCAL SQL wallet, not an external payment provider or saga.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
import signal
import threading
import time
from .adapters import Settings
from .observability import safe_error
from .transaction_model import (SCENARIOS, FAILPOINTS, Purchase, Rejected, InjectedFailure,
                                CommitUnknown, audit, digest, options, plan)
from .transaction_sql import Repository


def attempt(fn) -> dict:
    try:
        return fn()
    except Rejected as exc:
        return {'outcome': 'rejected', 'reason': exc.reason}
    except InjectedFailure as exc:
        return {'outcome': 'injected_failure', 'boundary': exc.stage}
    except CommitUnknown:
        return {'outcome': 'commit_unknown'}
    except Exception as exc:
        return {'outcome': 'unexpected_error', **safe_error(exc)}


def parallel(clients: int, fn, *, synchronize_read: bool = False) -> list[dict]:
    """Bounded starts; the unsafe read barrier *forces* the bad interleaving.

    Protected code never waits at a barrier while holding a row lock. A read barrier
    timing out is a failed experiment, not evidence of an observed DB anomaly.
    """
    start = threading.Barrier(clients, timeout=6)
    read = threading.Barrier(clients, timeout=6) if synchronize_read else None
    results = [None] * clients

    def worker(index):
        results[index] = attempt(lambda: (start.wait(), fn(index, read.wait if read else None))[1])

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(clients)]
    for thread in threads:
        thread.start()
    end = time.monotonic() + 20
    for thread in threads:
        thread.join(max(0, end-time.monotonic()))
    if any(t.is_alive() for t in threads):
        raise RuntimeError('Unfinished SQL clients; no consistent final audit possible')
    return results


def clean_outcomes(outcomes: list[dict]) -> bool:
    return all(r and r.get('outcome') != 'unexpected_error' for r in outcomes)


def run(repo: Repository, scenario: str, clients: int = 4, seed: int = 42) -> dict:
    options(scenario, clients, seed)
    unit_price = 100 + seed % 101
    cases = []
    repo.initialize()

    def setup(case, stock=None):
        repo.seed(case, stock=clients+4 if stock is None else stock, balance=unit_price*(clients+10)*3, price=unit_price)

    def observe(case, outcomes, *, claims=True):
        snapshot = repo.snapshot(case)
        row = {'case': case, 'outcomes': outcomes, 'snapshot': snapshot, 'audit': audit(snapshot, require_claims=claims)}
        cases.append(row)
        return row

    def violation(row, name):
        return any(v['check'] == name for v in row['audit']['violations'])

    def counts(row):
        return Counter(r['outcome'] for r in row['outcomes'])

    expected_unsafe = False
    protected_ok = False
    scenario_checks = {}
    if scenario == 'stock-race':
        for label in ('unsafe', 'protected'):
            setup(label, stock=1)
            outcomes = parallel(clients, lambda i, hook: repo.purchase(Purchase(label, 'request-'+str(i)),
                                policy='stale-stock' if label == 'unsafe' else 'protected', after_read=hook),
                                synchronize_read=label == 'unsafe')
            row = observe(label, outcomes, claims=label != 'unsafe')
            if label == 'unsafe':
                expected_unsafe = counts(row) == {'created': clients} and violation(row, 'stock_conservation')
            else:
                reasons = [r.get('reason') for r in outcomes if r['outcome'] == 'rejected']
                protected_ok = (row['audit']['passed'] and counts(row) == {'created': 1, 'rejected': clients-1}
                                and reasons == ['sold_out']*(clients-1))
        scenario_checks['protected_orders'] = cases[-1]['audit']['counts']['orders']
    elif scenario == 'duplicate-checkout':
        for label in ('unsafe', 'protected'):
            setup(label)
            outcomes = parallel(clients, lambda i, hook: repo.purchase(Purchase(label, 'same-key'),
                                policy='no-claim' if label == 'unsafe' else 'protected'))
            row = observe(label, outcomes, claims=label != 'unsafe')
            if label == 'unsafe':
                expected_unsafe = counts(row) == {'created': clients} and violation(row, 'one_order_per_request')
            else:
                ids = {r.get('order', {}).get('id') for r in outcomes}
                protected_ok = (row['audit']['passed'] and counts(row) == {'created': 1, 'replayed': clients-1}
                                and len(ids) == 1 and None not in ids)
    elif scenario == 'checkout-rollback':
        unsafe_flags, safe_flags = [], []
        for boundary in FAILPOINTS:
            for label in ('unsafe', 'protected'):
                case = label + '-' + boundary
                setup(case)
                before = repo.snapshot(case)
                request = Purchase(case, 'retry-same-key')
                first = attempt(lambda: repo.purchase(request, policy='split-commit' if label == 'unsafe' else 'protected', fail_at=boundary))
                row = observe(case, [first], claims=label != 'unsafe')
                row['before_sha256'] = digest(before)
                row['unchanged_after_failure'] = before == row['snapshot']
                injected = first.get('outcome') == 'injected_failure' and first.get('boundary') == boundary
                if label == 'unsafe':
                    unsafe_flags.append(injected and not row['unchanged_after_failure'])
                else:
                    retry = attempt(lambda: repo.purchase(request))
                    recovered = repo.snapshot(case)
                    row['retry'] = retry
                    row['recovered_snapshot'] = recovered
                    row['recovered_audit'] = audit(recovered)
                    safe_flags.append(injected and row['unchanged_after_failure'] and retry.get('outcome') == 'created'
                                      and row['recovered_audit']['passed'])
        expected_unsafe, protected_ok = all(unsafe_flags), all(safe_flags)
        scenario_checks['fault_boundaries_checked'] = list(FAILPOINTS)
    elif scenario == 'commit-ambiguity':
        for label in ('unsafe', 'protected'):
            setup(label)
            request = Purchase(label, 'same-key')
            policy = 'no-claim' if label == 'unsafe' else 'protected'
            first = attempt(lambda: repo.purchase(request, policy=policy, lose_response=True))
            committed = repo.snapshot(label)
            observation = repo.lookup(request) if label == 'protected' else None
            read_was_readonly = committed == repo.snapshot(label)
            second = attempt(lambda: repo.purchase(request, policy=policy))
            row = observe(label, [first, second], claims=label != 'unsafe')
            row.update({'after_unknown_snapshot': committed, 'read_only_reconciliation': observation,
                        'observation_did_not_mutate': read_was_readonly})
            if label == 'unsafe':
                expected_unsafe = first['outcome'] == 'commit_unknown' and second['outcome'] == 'created' and violation(row, 'one_order_per_request')
            else:
                protected_ok = (first['outcome'] == 'commit_unknown' and second['outcome'] == 'replayed'
                                and observation is not None and second.get('order') == observation and read_was_readonly
                                and row['audit']['passed'] and row['audit']['counts']['orders'] == 1)
        scenario_checks['injection'] = 'explicit Python exception after confirmed COMMIT; no actual packet/connection drop'
    elif scenario == 'idempotency-conflict':
        for label in ('unsafe', 'protected'):
            setup(label)
            first = attempt(lambda: repo.purchase(Purchase(label, 'same-key', 1)))
            before = repo.snapshot(label)
            conflict = attempt(lambda: repo.purchase(Purchase(label, 'same-key', 2),
                               policy='unchecked-replay' if label == 'unsafe' else 'protected'))
            row = observe(label, [first, conflict])
            row['unchanged_on_conflict'] = before == row['snapshot']
            row['requested_quantity'] = 2
            if label == 'unsafe':
                expected_unsafe = (first['outcome'] == 'created' and conflict['outcome'] == 'replayed'
                                   and conflict.get('order', {}).get('quantity') != 2)
                row['request_contract_violation'] = expected_unsafe
            else:
                protected_ok = (first['outcome'] == 'created' and conflict == {'outcome': 'rejected', 'reason': 'idempotency_conflict'}
                                and row['unchanged_on_conflict'] and row['audit']['passed'])
    elif scenario == 'refund-race':
        for label in ('unsafe', 'protected'):
            setup(label)
            order = repo.purchase(Purchase(label, 'same-key'))['order']
            outcomes = parallel(clients, lambda i, hook: repo.refund(label, order['id'], protected=label != 'unsafe', after_read=hook),
                                synchronize_read=label == 'unsafe')
            row = observe(label, outcomes)
            if label == 'unsafe':
                expected_unsafe = counts(row) == {'refunded': clients} and violation(row, 'order_ledger_exact') and violation(row, 'stock_conservation')
            else:
                protected_ok = row['audit']['passed'] and counts(row) == {'refunded': 1, 'replayed': clients-1}
    all_clean = all(clean_outcomes(c['outcomes']) for c in cases)
    # A negative control is meant to be bad. Its exact symptom must nevertheless be observed.
    status = 'failed' if not protected_ok or not all_clean else ('passed' if expected_unsafe else 'inconclusive')
    return {'schema': 1, 'scenario': scenario, 'plan': plan(scenario, clients, seed), 'status': status,
            'observed': status == 'passed', 'negative_control_observed': bool(expected_unsafe),
            'protected_passed': bool(protected_ok), 'unexpected_errors_absent': all_clean,
            'server_version': repo.server_version, 'cases': cases, 'scenario_checks': scenario_checks,
            'trace': repo.trace.events, 'normal_pipeline_changed': False,
            'scope': 'disposable single MariaDB local accounting; no real payment, Kafka delivery, HA, durability or throughput claim'}


def report(result: dict) -> str:
    lines = ['# 트랜잭션 정합성 비교 실습', '',
             f"실험: `{result['scenario']}` · 판정: **{result['status']}**", '',
             f"잘못된 비교군의 목표 증상 관측: `{result['negative_control_observed']}`",
             f"보호된 구현의 계약·정합성 통과: `{result['protected_passed']}`", '',
             '정상 API나 결제 시스템을 변경하지 않는 별도 MariaDB 실험입니다. 금액은 합성 정수 단위입니다.',
             'passed는 잘못된 비교군이 안전하다는 뜻이 아니라, 예상 실패를 감지하고 보호된 구현을 대조했다는 뜻입니다.', '',
             '| 비교군 | 주문 | 남은 재고 | 구매자 잔액 | 판매자 잔액 | 관측한 정합성 위반 |',
             '|---|---:|---:|---:|---:|---|']
    for row in result['cases']:
        a = row['audit']
        codes = sorted({v['check'] for v in a['violations']})
        if row.get('request_contract_violation'):
            codes.append('request_payload_mismatch')
        lines.append(f"| {row['case']} | {a['counts']['orders']} | {a['stock']['remaining']} | {a['balances_minor_units']['buyer']} | {a['balances_minor_units']['merchant']} | {', '.join(codes) or '없음'} |")
    lines += ['', '## 확인할 파일과 순서', '',
              '`transaction.json`의 cases에는 전체 행, 실패 직후 스냅샷, 재시도 결과가 있습니다.',
              '`trace.jsonl`에서 statement_boundary → commit/rollback → replay 순서를 대조합니다.',
              '시간은 helper가 관측한 클라이언트 시간이며 DB 내부 실행 시간이나 성능 점수가 아닙니다.',
              'checkout-rollback의 표는 실패 직후이며, 재시도 후 상태는 recovered_snapshot/recovered_audit를 봅니다.',
              'commit-ambiguity는 commit 이후 명시적 예외이며 실제 네트워크 단절은 아닙니다.', '']
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Disposable txlab only; never normal orders or payment processing')
    parser.add_argument('scenario', choices=SCENARIOS)
    parser.add_argument('--clients', type=int, default=4)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args(argv)
    repo = None
    previous = signal.getsignal(signal.SIGALRM)
    def timeout(*_):
        raise TimeoutError('Transaction helper deadline exceeded')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(75)
    try:
        options(args.scenario, args.clients, args.seed)
        repo = Repository(Settings.load())
        result = run(repo, args.scenario, args.clients, args.seed)
        result['evidence_kind'] = 'REAL-MARIADB-TRANSACTIONS-IN-DISPOSABLE-CONTAINER'
        print(json.dumps(result, ensure_ascii=False))
        return {'passed': 0, 'failed': 1, 'inconclusive': 2}[result['status']]
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'observed': False, 'scenario': args.scenario,
                          'evidence_kind': 'MARIADB-ATTEMPT-NOT-COMPLETED', **safe_error(exc),
                          'trace': repo.trace.events if repo else []}, ensure_ascii=False))
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
