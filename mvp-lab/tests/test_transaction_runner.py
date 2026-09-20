"""Real runner and repository, SQLite schedule/SQL adapter. NOT MariaDB engine tests."""
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from mvp_app.adapters import Settings
from mvp_app.transaction_model import SCENARIOS, Rejected
from mvp_app.transaction_sql import Repository
from mvp_app.transaction_drill import run, report, parallel
from transaction_sqlite import DeferredWriteConnection


class RunnerTests(TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'MVP_DISPOSABLE':'drill-0123456789ab'}); env.start(); self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.counter = 0

    def repo(self):
        self.counter += 1
        path = str(Path(self.tmp.name) / (str(self.counter)+'.db'))
        return Repository(Settings(sql_host='127.0.0.1',sql_database='txlab',sql_user='txlab'),
                          connect=lambda: DeferredWriteConnection(path))

    def scenario(self, name):
        result = run(self.repo(), name)
        self.assertEqual(result['status'], 'passed', result)
        self.assertTrue(result['negative_control_observed'])
        self.assertTrue(result['protected_passed'])
        self.assertTrue(result['unexpected_errors_absent'])
        return result

    def test_stock_race(self):
        result = self.scenario('stock-race')
        self.assertEqual(result['cases'][0]['audit']['counts']['orders'], 4)
        self.assertEqual(result['cases'][1]['audit']['counts']['orders'], 1)

    def test_duplicate_checkout(self): self.scenario('duplicate-checkout')
    def test_commit_ambiguity(self): self.scenario('commit-ambiguity')
    def test_idempotency_conflict(self): self.scenario('idempotency-conflict')
    def test_refund_race(self): self.scenario('refund-race')

    def test_all_partial_failure_boundaries(self):
        result = self.scenario('checkout-rollback')
        self.assertEqual(len(result['cases']), 12)
        for case in result['cases']:
            if case['case'].startswith('protected'):
                self.assertTrue(case['unchanged_after_failure'])
                self.assertTrue(case['recovered_audit']['passed'])

    def test_boundary_clients_and_seed(self):
        for n in (2, 8):
            with self.subTest(clients=n):
                value = run(self.repo(), 'stock-race', clients=n, seed=2**31-1)
                self.assertEqual(value['status'], 'passed', value)
                self.assertEqual(value['plan']['unit_price_minor'], 100+(2**31-1)%101)

    def test_negative_control_no_effect_is_inconclusive(self):
        repo = self.repo(); original = repo.purchase
        def fixed(req, **kwargs):
            if kwargs.get('policy') == 'stale-stock':
                kwargs.update(policy='protected', after_read=None)
            return original(req, **kwargs)
        with patch.object(repo, 'purchase', side_effect=fixed):
            value = run(repo, 'stock-race')
        self.assertEqual(value['status'], 'inconclusive')
        self.assertFalse(value['negative_control_observed'])
        self.assertTrue(value['protected_passed'])

    def test_protected_damaged_must_fail(self):
        repo = self.repo(); original = repo.snapshot
        def corrupt(case):
            snapshot = original(case)
            if case == 'protected': snapshot['wallets'][0]['balance'] += 1
            return snapshot
        with patch.object(repo, 'snapshot', side_effect=corrupt):
            value = run(repo, 'duplicate-checkout')
        self.assertEqual(value['status'], 'failed')
        self.assertFalse(value['protected_passed'])

    def test_missing_outbox_must_fail_even_when_money_and_stock_match(self):
        repo = self.repo(); original = repo.snapshot
        def corrupt(case):
            snapshot = original(case)
            if case == 'protected': snapshot['events'] = []
            return snapshot
        with patch.object(repo, 'snapshot', side_effect=corrupt):
            value = run(repo, 'duplicate-checkout')
        self.assertEqual(value['status'], 'failed')

    def test_wrong_payload_return_not_proof_of_rejection(self):
        repo = self.repo(); original = repo.purchase
        def broken(req, **kwargs):
            if req.case_id == 'protected' and req.quantity == 2:
                kwargs['policy'] = 'unchecked-replay'
            return original(req, **kwargs)
        with patch.object(repo, 'purchase', side_effect=broken): value = run(repo,'idempotency-conflict')
        self.assertEqual(value['status'], 'failed')

    def test_unexpected_parallel_errors_not_swallowed(self):
        value = parallel(4, lambda i,h: (_ for _ in ()).throw(OSError('secret connection string')))
        self.assertTrue(all(r['outcome'] == 'unexpected_error' for r in value))
        self.assertNotIn('secret connection string', str(value))

    def test_report_contains_both_controls_and_limitations(self):
        result = self.scenario('commit-ambiguity')
        text = report(result)
        self.assertIn('unsafe', text)
        self.assertIn('protected', text)
        self.assertIn('실제 네트워크 단절은 아닙니다', text)
        self.assertIn('transaction.json', text)

    def test_unknown_case_rejected_before_initialization(self):
        repo = self.repo()
        with patch.object(repo, 'initialize') as init:
            with self.assertRaises(ValueError): run(repo, 'nonsense')
            init.assert_not_called()
