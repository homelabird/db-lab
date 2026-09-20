"""Actual new repository DML through a TEST SQLite adapter, never an engine acceptance."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch
from mvp_app.adapters import Settings
from mvp_app.transaction_sql import Repository, guard, Trace
from mvp_app.transaction_model import Purchase, Rejected, InjectedFailure, CommitUnknown, audit, FAILPOINTS
from mvp_app.transaction_drill import parallel
from transaction_sqlite import Connection, EngineError

SETTINGS = Settings(sql_host='127.0.0.1', sql_database='txlab', sql_user='txlab', sql_password='secret')
RUN = 'drill-0123456789ab'


class SQLTests(TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'MVP_DISPOSABLE': RUN}); env.start(); self.addCleanup(env.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'test.db')
        self.calls = []
        self.repo = Repository(SETTINGS, connect=lambda: Connection(self.path, self.calls))
        self.repo.initialize()
        self.repo.seed('case', stock=10, balance=10000, price=123)
        self.req = Purchase('case', 'request', 2)

    def test_atomic_checkout_creates_balanced_linked_rows(self):
        result = self.repo.purchase(self.req)
        value = audit(self.repo.snapshot('case'))
        self.assertEqual(result['outcome'], 'created')
        self.assertTrue(value['passed'], value)
        self.assertEqual(value['counts'], {'inventory': 1, 'wallets': 2, 'requests': 1, 'orders': 1, 'ledger': 2, 'events': 1})
        self.assertEqual(value['stock']['remaining'], 8)
        self.assertEqual(value['balances_minor_units'], {'buyer': 9754, 'merchant': 246})

    def test_same_key_same_payload_is_one_charge(self):
        first = self.repo.purchase(self.req)
        before = self.repo.snapshot('case')
        second = self.repo.purchase(self.req)
        self.assertEqual(second['outcome'], 'replayed')
        self.assertEqual(second['order'], first['order'])
        self.assertEqual(before, self.repo.snapshot('case'))

    def test_same_key_other_payload_rejected_without_writes(self):
        self.repo.purchase(self.req)
        before = self.repo.snapshot('case')
        with self.assertRaisesRegex(Rejected, 'idempotency_conflict'):
            self.repo.purchase(Purchase('case', 'request', 1))
        self.assertEqual(before, self.repo.snapshot('case'))

    def test_different_keys_are_independent(self):
        self.repo.purchase(self.req)
        self.repo.purchase(Purchase('case', 'other', 2))
        result = audit(self.repo.snapshot('case'))
        self.assertTrue(result['passed'])
        self.assertEqual(result['stock']['remaining'], 6)

    def test_sold_out_rolls_back_request_claim(self):
        with self.assertRaisesRegex(Rejected, 'sold_out'):
            self.repo.purchase(Purchase('case', 'too-many', 20))
        self.assertEqual(self.repo.snapshot('case')['requests'], [])
        self.assertTrue(audit(self.repo.snapshot('case'))['passed'])

    def test_insufficient_funds_rolls_back_stock_and_claim(self):
        self.repo.seed('poor', stock=4, balance=1, price=123)
        before = self.repo.snapshot('poor')
        with self.assertRaisesRegex(Rejected, 'insufficient_funds'):
            self.repo.purchase(Purchase('poor', 'k'))
        self.assertEqual(before, self.repo.snapshot('poor'))

    def test_all_six_failure_boundaries_leave_zero_residue(self):
        before = self.repo.snapshot('case')
        for boundary in FAILPOINTS:
            with self.subTest(boundary=boundary):
                with self.assertRaises(InjectedFailure):
                    self.repo.purchase(self.req, fail_at=boundary)
                self.assertEqual(before, self.repo.snapshot('case'))
        self.assertEqual(self.repo.purchase(self.req)['outcome'], 'created')

    def test_split_commit_leaves_residue_at_every_boundary(self):
        for stage in FAILPOINTS:
            with self.subTest(stage=stage):
                self.repo.seed(stage, stock=4, balance=1000, price=10)
                before = self.repo.snapshot(stage)
                with self.assertRaises(InjectedFailure):
                    self.repo.purchase(Purchase(stage, 'key'), policy='split-commit', fail_at=stage)
                self.assertNotEqual(before, self.repo.snapshot(stage))

    def test_response_loss_read_reconciliation_and_retry(self):
        with self.assertRaises(CommitUnknown):
            self.repo.purchase(self.req, lose_response=True)
        before = self.repo.snapshot('case')
        actual = self.repo.lookup(self.req)
        self.assertIsNotNone(actual)
        self.assertEqual(before, self.repo.snapshot('case'))
        replay = self.repo.purchase(self.req)
        self.assertEqual(replay['outcome'], 'replayed')
        self.assertEqual(replay['order'], actual)
        self.assertTrue(audit(self.repo.snapshot('case'))['passed'])
        stages = [x['stage'] for x in self.repo.trace.events]
        self.assertLess(stages.index('commit_confirmed'), stages.index('commit_response_unknown'))

    def test_transport_error_during_commit_never_automatically_retried(self):
        raw = Connection(self.path, self.calls)
        real_commit = raw.commit
        def disconnected():
            real_commit()
            raise EngineError(2013, 'lost response')
        raw.commit = disconnected
        with patch.object(self.repo, 'connect', return_value=raw) as c:
            with self.assertRaises(CommitUnknown):
                self.repo.purchase(self.req)
            self.assertEqual(c.call_count, 1)
        self.assertEqual(self.repo.purchase(self.req)['outcome'], 'replayed')

    def test_unknown_commit_without_actual_commit_can_retry_same_key(self):
        raw = Connection(self.path, self.calls)
        raw.commit = Mock(side_effect=EngineError(2013, 'commit never reached database'))
        with patch.object(self.repo, 'connect', return_value=raw):
            with self.assertRaises(CommitUnknown):
                self.repo.purchase(self.req)
        self.assertIsNone(self.repo.lookup(self.req))
        self.assertEqual(self.repo.purchase(self.req)['outcome'], 'created')

    def test_only_lock_failures_use_bounded_retry(self):
        with patch.object(self.repo, '_purchase', side_effect=[EngineError(1213, 'victim'), {'outcome': 'created'}]) as f:
            self.assertEqual(self.repo.purchase(self.req)['outcome'], 'created')
            self.assertEqual(f.call_count, 2)
        with patch.object(self.repo, '_purchase', side_effect=EngineError(1205, 'wait')) as f:
            with self.assertRaises(EngineError): self.repo.purchase(self.req)
            self.assertEqual(f.call_count, 3)

    def test_auth_failure_not_retried(self):
        with patch.object(self.repo, '_purchase', side_effect=EngineError(1045, 'auth')) as f:
            with self.assertRaises(EngineError): self.repo.purchase(self.req)
            self.assertEqual(f.call_count, 1)

    def test_unchecked_replay_has_request_contract_error_even_with_balanced_rows(self):
        self.repo.purchase(self.req)
        result = self.repo.purchase(Purchase('case', 'request', 3), policy='unchecked-replay')
        self.assertEqual(result['order']['quantity'], 2)
        self.assertTrue(audit(self.repo.snapshot('case'))['passed'])

    def test_missing_idempotency_guard_double_charges(self):
        self.repo.purchase(self.req, policy='no-claim')
        self.repo.purchase(self.req, policy='no-claim')
        value = audit(self.repo.snapshot('case'), require_claims=False)
        self.assertIn('one_order_per_request', [v['check'] for v in value['violations']])
        self.assertEqual(value['balances_minor_units']['merchant'], 492)

    def test_refund_restores_stock_money_and_exact_events(self):
        order = self.repo.purchase(self.req)['order']
        self.repo.refund('case', order['id'])
        row = self.repo.snapshot('case')
        value = audit(row)
        self.assertTrue(value['passed'], value)
        self.assertEqual(value['counts']['ledger'], 4)
        self.assertEqual(value['counts']['events'], 2)
        self.assertEqual(value['stock']['remaining'], 10)
        self.assertEqual(value['balances_minor_units'], {'buyer': 10000, 'merchant': 0})

    def test_refund_repeat_is_readonly(self):
        order = self.repo.purchase(self.req)['order']
        self.repo.refund('case', order['id'])
        before = self.repo.snapshot('case')
        result = self.repo.refund('case', order['id'])
        self.assertEqual(result['outcome'], 'replayed')
        self.assertEqual(before, self.repo.snapshot('case'))

    def test_other_case_cannot_refund_an_order(self):
        order = self.repo.purchase(self.req)['order']
        with self.assertRaisesRegex(Rejected, 'order_missing'):
            self.repo.refund('other-case', order['id'])
        self.assertEqual(self.repo.lookup(self.req)['state'], 'paid')

    def test_missing_order_not_silently_refunded(self):
        with self.assertRaisesRegex(Rejected, 'order_missing'):
            self.repo.refund('case', 'a'*32)

    def test_safe_concurrent_duplicate_sqlite_coarse_lock(self):
        rows = parallel(4, lambda i, hook: self.repo.purchase(self.req))
        self.assertEqual(sum(r['outcome'] == 'created' for r in rows), 1, rows)
        self.assertEqual(sum(r['outcome'] == 'replayed' for r in rows), 3, rows)
        self.assertTrue(audit(self.repo.snapshot('case'))['passed'])

    def test_safe_concurrent_final_item_sqlite_coarse_lock(self):
        self.repo.seed('last-item', stock=1, balance=1000, price=10)
        rows = parallel(4, lambda i, hook: self.repo.purchase(Purchase('last-item', 'key'+str(i))))
        self.assertEqual(sum(r['outcome'] == 'created' for r in rows), 1, rows)
        self.assertEqual(sum(r.get('reason') == 'sold_out' for r in rows), 3, rows)
        self.assertTrue(audit(self.repo.snapshot('last-item'))['passed'])

    def test_safe_concurrent_refund_sqlite_coarse_lock(self):
        order = self.repo.purchase(self.req)['order']
        rows = parallel(4, lambda i, hook: self.repo.refund('case', order['id']))
        self.assertEqual(sum(r['outcome'] == 'refunded' for r in rows), 1, rows)
        self.assertTrue(audit(self.repo.snapshot('case'))['passed'])

    def test_existing_schema_is_not_overwritten(self):
        before = self.repo.snapshot('case')
        with self.assertRaisesRegex(ValueError, 'EMPTY'):
            self.repo.initialize()
        self.assertEqual(before, self.repo.snapshot('case'))

    def test_snapshot_is_repeatable_read_and_no_mutation_queries(self):
        self.calls.clear()
        self.repo.snapshot('case')
        self.assertIn('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY', self.calls)
        self.assertFalse(any(s.startswith(('INSERT', 'UPDATE', 'DELETE')) for s in self.calls))

    def test_missing_case_is_not_empty_success(self):
        with self.assertRaises(ValueError):
            audit(self.repo.snapshot('missing'))

    def test_corrupt_committed_claim_fails_replay(self):
        order = self.repo.purchase(self.req)['order']
        with sqlite3.connect(self.path) as c:
            c.execute('DELETE FROM orders WHERE id=?', (order['id'],))
        with self.assertRaisesRegex(RuntimeError, 'matching order'):
            self.repo.purchase(self.req)

    def test_invalid_policy_or_stage_does_not_connect(self):
        with patch.object(self.repo, 'connect') as connect:
            with self.assertRaises(ValueError): self.repo.purchase(self.req, policy='arbitrary')
            with self.assertRaises(ValueError): self.repo.purchase(self.req, fail_at='DROP TABLE')
            connect.assert_not_called()

    def test_fixture_cannot_be_reseeded(self):
        before = self.repo.snapshot('case')
        with self.assertRaises(EngineError): self.repo.seed('case', stock=99, balance=12, price=1)
        self.assertEqual(before, self.repo.snapshot('case'))


class GuardTests(TestCase):
    def test_source_mvp_settings_rejected_before_connection(self):
        with patch.dict('os.environ', {'MVP_DISPOSABLE': RUN}):
            connect = Mock()
            with self.assertRaises(ValueError): Repository(Settings(), connect=connect)
            connect.assert_not_called()

    def test_marker_required(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError): Repository(SETTINGS, connect=Mock())

    def test_host_port_database_and_user_are_exact(self):
        with patch.dict('os.environ', {'MVP_DISPOSABLE': RUN}):
            for change in ({'sql_host':'mariadb'}, {'sql_host':'localhost'}, {'sql_port':3307}, {'sql_database':'mvp'}, {'sql_user':'root'}):
                with self.subTest(change=change), self.assertRaises(ValueError): guard(replace(SETTINGS, **change))

    def test_marker_cannot_be_path_or_other_type(self):
        for value in ('../drill-0123456789ab', 'msg-0123456789ab', RUN+'extra', ''):
            with patch.dict('os.environ', {'MVP_DISPOSABLE': value}), self.assertRaises(ValueError):
                guard(SETTINGS)

    def test_trace_is_bounded_and_thread_safe(self):
        trace = Trace()
        rows = parallel(8, lambda i,h: (trace.add('check', number=i), {'outcome':'ok'})[1])
        self.assertEqual(len(trace.events), 8)
        self.assertEqual([x['seq'] for x in trace.events], list(range(8)))
        for i in range(4088): trace.add('check')
        with self.assertRaises(RuntimeError): trace.add('too-many')
