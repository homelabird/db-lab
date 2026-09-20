"""Pure accounting oracle for isolated transaction lessons (never a payment service).

The oracle reads ROWS, not driver success flags. Unsafe control cases intentionally use
exactly the same tables without some application safeguards; this makes their failures
visible instead of relying on a prewritten expected result.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

SCENARIOS = {
    'stock-race': '마지막 재고 동시 구매: 잠금 없는 읽기-덮어쓰기와 조건부 원자적 차감 비교',
    'duplicate-checkout': '같은 결제 요청 동시 재시도: 요청별 중복 실행과 UNIQUE 멱등성 기록 비교',
    'checkout-rollback': '차감·송금·주문·원장·outbox 중간 실패: 단계별 commit과 전체 rollback 비교',
    'commit-ambiguity': 'commit 뒤 응답만 소실: 무조건 재결제와 읽기 확인·동일 키 재요청 비교',
    'idempotency-conflict': '같은 요청 키·다른 수량: 키만 비교하는 구현과 payload fingerprint 비교',
    'refund-race': '동일 주문 동시 환불: 오래된 상태 검사와 행 잠금·단회 전이 비교',
}
TABLES = ('inventory', 'wallets', 'requests', 'orders', 'ledger', 'events')
FAILPOINTS = ('inventory', 'debit', 'credit', 'order', 'ledger', 'outbox')
MAX_CLIENTS = 8
MAX_ROWS = 1000


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def options(scenario: str, clients: int, seed: int) -> None:
    if scenario not in SCENARIOS:
        raise ValueError('Unknown transaction scenario')
    if type(clients) is not int or not 2 <= clients <= MAX_CLIENTS:
        raise ValueError('Transaction clients must be an integer in 2..8')
    if type(seed) is not int or not 0 <= seed <= 2**31 - 1:
        raise ValueError('Transaction seed must be an integer in 0..2147483647')


@dataclass(frozen=True)
class Purchase:
    case_id: str
    key: str
    quantity: int = 1

    def __post_init__(self) -> None:
        if (not isinstance(self.case_id, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', self.case_id)
                or not isinstance(self.key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.key)
                or type(self.quantity) is not int or not 1 <= self.quantity <= 20):
            raise ValueError('Invalid synthetic purchase')

    @property
    def fingerprint(self) -> str:
        # Price is authoritative fixture data, not a client-supplied charge amount.
        return digest({'case_id': self.case_id, 'key': self.key,
                       'quantity': self.quantity, 'sku': 'study-widget', 'buyer': 'buyer'})


class Rejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class InjectedFailure(Exception):
    def __init__(self, stage: str):
        self.stage = stage
        super().__init__('deliberate application failure at ' + stage)


class CommitUnknown(Exception):
    """Never automatically retry a commit with a new key, nor label it a rollback."""


def audit(snapshot: dict, *, require_claims: bool = True) -> dict:
    """Check per-order links as well as aggregates; balanced wrong transfers must fail.

    Call after clients have joined. snapshots from SQL use one repeatable-read view.
    A missing/malformed snapshot is a failed observation, not an empty good database.
    """
    if not isinstance(snapshot, dict) or set(snapshot) != set(TABLES):
        raise ValueError('Incomplete transaction snapshot')
    if any(not isinstance(snapshot[t], list) or len(snapshot[t]) > MAX_ROWS for t in TABLES):
        raise ValueError('Invalid or oversized transaction snapshot')
    if len(snapshot['inventory']) != 1 or len(snapshot['wallets']) != 2:
        raise ValueError('Expected one inventory and two wallet rows')
    violations: list[dict] = []

    def check(name: str, ok: bool, **details: Any) -> None:
        if not ok:
            violations.append({'check': name, **details})

    inv = snapshot['inventory'][0]
    case_id = inv['case_id']
    check('fixture_values', all(type(inv[k]) is int for k in ('initial_qty', 'available', 'unit_price'))
          and inv['initial_qty'] >= 0 and inv['unit_price'] > 0)
    if any(row.get('case_id') != case_id for rows in snapshot.values() for row in rows):
        raise ValueError('Cross-case rows in snapshot')
    wallets = {r['owner']: r for r in snapshot['wallets']}
    if set(wallets) != {'buyer', 'merchant'}:
        raise ValueError('Unexpected wallet identities')
    orders = {r['id']: r for r in snapshot['orders']}
    check('unique_order_id', len(orders) == len(snapshot['orders']))
    stock_sold = sum(o['quantity'] for o in orders.values() if o['state'] == 'paid')
    check('stock_conservation', inv['available'] + stock_sold == inv['initial_qty'],
          initial=inv['initial_qty'], available=inv['available'], net_sold=stock_sold)
    check('stock_bounds', 0 <= inv['available'] <= inv['initial_qty'], available=inv['available'])
    check('money_conservation', sum(w['balance'] for w in wallets.values()) == sum(w['initial_balance'] for w in wallets.values()))
    keys = Counter(o['request_key'] for o in orders.values())
    check('one_order_per_request', all(n == 1 for n in keys.values()), duplicates=sum(n-1 for n in keys.values()))
    for owner, w in wallets.items():
        check('wallet_value_types', type(w['initial_balance']) is int and type(w['balance']) is int and w['initial_balance'] >= 0, owner=owner)
        check('nonnegative_balance', w['balance'] >= 0, owner=owner, balance=w['balance'])
        delta = sum(e['delta'] for e in snapshot['ledger'] if e['owner'] == owner)
        check('wallet_ledger_agreement', w['balance'] == w['initial_balance'] + delta, owner=owner)
    check('known_ledger_owners', all(e['owner'] in wallets for e in snapshot['ledger']))
    check('ledger_zero_sum', sum(e['delta'] for e in snapshot['ledger']) == 0)
    check('ledger_no_orphans', all(e['order_id'] in orders for e in snapshot['ledger']))
    check('events_no_orphans', all(e['order_id'] in orders for e in snapshot['events']))
    claims = {r['request_key']: r for r in snapshot['requests']}
    check('unique_claim', len(claims) == len(snapshot['requests']))
    check('claims_no_orphans', all(c['order_id'] in orders for c in claims.values()))
    check('claim_targets_unique', len({c['order_id'] for c in claims.values()}) == len(claims))
    check('claim_request_identity', all(c['order_id'] in orders and orders[c['order_id']]['request_key'] == c['request_key'] for c in claims.values()))
    for oid, order in orders.items():
        q, amount, state = order['quantity'], order['amount'], order['state']
        check('order_values', type(q) is int and q > 0 and amount == q * inv['unit_price'] and state in {'paid', 'refunded'}, order_id=oid)
        if require_claims:
            c = claims.get(order['request_key'])
            expected = Purchase(case_id, order['request_key'], q).fingerprint
            check('claim_payload_and_order', c is not None and c['order_id'] == oid and c['fingerprint'] == expected, order_id=oid)
        expected_ledger = Counter({('buyer', 'paid', -amount): 1, ('merchant', 'paid', amount): 1})
        expected_events = Counter({'paid': 1})
        if state == 'refunded':
            expected_ledger += Counter({('buyer', 'refunded', amount): 1, ('merchant', 'refunded', -amount): 1})
            expected_events['refunded'] = 1
        actual_ledger = Counter((r['owner'], r['phase'], r['delta']) for r in snapshot['ledger'] if r['order_id'] == oid)
        check('order_ledger_exact', actual_ledger == expected_ledger, order_id=oid)
        event_rows = [r for r in snapshot['events'] if r['order_id'] == oid]
        check('order_events_exact', Counter(r['kind'] for r in event_rows) == expected_events, order_id=oid)
        for event in event_rows:
            try:
                payload = json.loads(event['payload'])
            except (TypeError, ValueError):
                payload = None
            check('event_payload_matches', payload == {'order_id': oid, 'quantity': q, 'amount': amount, 'state': event['kind']}, order_id=oid)
    return {'passed': not violations, 'violations': violations, 'snapshot_sha256': digest(snapshot),
            'counts': {t: len(snapshot[t]) for t in TABLES},
            'stock': {'initial': inv['initial_qty'], 'remaining': inv['available'], 'net_sold': stock_sold},
            'balances_minor_units': {k: v['balance'] for k, v in wallets.items()}}


def plan(scenario: str, clients: int = 4, seed: int = 42) -> dict:
    options(scenario, clients, seed)
    return {'scenario': scenario, 'goal': SCENARIOS[scenario], 'clients': clients, 'seed': seed,
            'unit_price_minor': 100 + seed % 101,
            'scope': 'new isolated txlab schema only; synthetic buyer/merchant balances, NOT a payment gateway',
            'comparison': 'unsafe negative control AND protected implementation; each starts with fresh case fixtures',
            'tables': list(TABLES), 'fault_stages': list(FAILPOINTS) if scenario == 'checkout-rollback' else [],
            'limits': {'clients': 8, 'case_rows': MAX_ROWS, 'sql_lock_wait_seconds': 2, 'helper_seconds': 75},
            'source_writes': False, 'normal_api_changed': False, 'kafka_delivery': False,
            'commit_ambiguity': 'deliberate exception after successful commit, NOT real network loss',
            'cleanup': 'owned temporary database/client containers and tmpfs; never original volumes',
            'pass_contract': 'specific unsafe symptom observed + protected full-row audit + scenario assertions'}


def verify_result(value: dict, scenario: str, clients: int, seed: int) -> None:
    """Host-side re-audit of helper ROWS; a lone passed=true flag is insufficient.

    This is not authentication of a hostile helper or a second database read. It catches
    incomplete/truncated reports and discrepancies between saved rows and their audit.
    """
    options(scenario, clients, seed)
    if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('scenario') != scenario
            or value.get('plan') != plan(scenario, clients, seed)
            or value.get('status') not in {'passed', 'failed', 'inconclusive'}):
        raise ValueError('Missing or mismatched transaction evidence header')
    expected_cases = ([f'{label}-{stage}' for stage in FAILPOINTS for label in ('unsafe', 'protected')]
                      if scenario == 'checkout-rollback' else ['unsafe', 'protected'])
    rows = value.get('cases')
    if not isinstance(rows, list) or [r.get('case') for r in rows] != expected_cases:
        raise ValueError('Missing or duplicate comparison cases')
    if not isinstance(value.get('trace'), list) or not 1 <= len(value['trace']) <= 4096:
        raise ValueError('Missing or oversized transaction trace')
    for row in rows:
        claims = not row['case'].startswith('unsafe') or scenario in {'refund-race', 'idempotency-conflict'}
        computed = audit(row['snapshot'], require_claims=claims)
        if computed != row.get('audit'):
            raise ValueError('Saved transaction audit does not match underlying rows')
        inv = row['snapshot']['inventory'][0]
        if (inv['unit_price'] != 100 + seed % 101
                or inv['initial_qty'] != (1 if scenario == 'stock-race' else clients+4)):
            raise ValueError('Fixture inventory differs from planned inputs')
        balances = {w['owner']: w['initial_balance'] for w in row['snapshot']['wallets']}
        if balances != {'buyer': (100+seed%101)*(clients+10)*3, 'merchant': 0}:
            raise ValueError('Fixture funding differs from planned inputs')
        if not isinstance(row.get('outcomes'), list) or not row['outcomes']:
            raise ValueError('Missing operation outcomes')
        if row['case'].startswith('protected') and scenario == 'checkout-rollback':
            if audit(row['recovered_snapshot']) != row.get('recovered_audit'):
                raise ValueError('Recovery audit differs from recovered rows')
            if value['status'] == 'passed' and not (row['unchanged_after_failure'] is True
                    and row['retry']['outcome'] == 'created' and row['recovered_audit']['passed']):
                raise ValueError('Successful rollback requires zero residue and a validated retry')
        if value['status'] == 'passed' and row['case'].startswith('protected') and not computed['passed']:
            raise ValueError('Successful lesson contains a failed protected audit')
    if value.get('observed') is not (value['status'] == 'passed'):
        raise ValueError('Observation flag and status disagree')
    if value['status'] == 'passed' and not (value.get('negative_control_observed') is True
            and value.get('protected_passed') is True and value.get('unexpected_errors_absent') is True):
        raise ValueError('Incomplete successful lesson assertions')
