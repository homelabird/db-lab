"""Offline controls/measurements tests. Explicit doubles, never real DB/engine certification."""
from dataclasses import replace
from http.server import ThreadingHTTPServer
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch
import uuid
from mvp_app.api import Application, make_handler, backend
from mvp_app.core import Problem, envelope, relay_once, project_one
from mvp_app.inspection import inspect_order, differences
from mvp_app.observability import safe_error
from mvp_app.adapters import Broker, SQL, Settings
from mvp_app.admin import rebuild
from mvp_app.lock_drill import hold
from tools import manage
from tools.probe import probe
from tools.simulation import Plan, HTTP, Response, Journal, Runner, SCENARIOS, percentiles, request_summary, inspect_intent
from tools.sim_engine import DockerLab
from fakes import MemoryRepo, MemoryCache, MemorySearch, MemoryBroker

RUN = 'sim-0123456789ab'
PROJECT = 'db-lab-mvp'
DATA = {'item': RUN+'-fixture-0', 'quantity': 2, 'unit_price': 31}


class PlanTests(unittest.TestCase):
    def test_repeatable_seed_and_plan(self):
        self.assertEqual(Plan(seed=7).document(), Plan(seed=7).document())
        self.assertNotEqual(Plan(seed=7).operations(), Plan(seed=8).operations())

    def test_every_scenario_has_a_valid_bounded_plan(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                plan = Plan(scenario=name).validate()
                self.assertEqual(len(plan.operations()), 80)

    def test_nonfinite_rate_rejected(self):
        for value in (float('nan'), float('inf'), -float('inf'), -1, 0, 10.01, True, '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Plan(rate=value).validate()

    def test_time_worker_seed_and_size_limits(self):
        for changes in ({'seconds': 0}, {'seconds': 181}, {'workers': 9}, {'workers': 0},
                        {'fault_for': 31}, {'fault_for': 0}, {'fault_at': 40},
                        {'seed': -1}, {'seed': True}, {'recovery_timeout': 301},
                        {'rate': 10, 'seconds': 180}, {'scenario': 'version-race', 'workers': 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(Plan(), **changes).validate()

    def test_leave_recovery_window(self):
        with self.assertRaises(ValueError):
            Plan(scenario='kafka-outage', fault_at=35).validate()

    def test_no_arbitrary_fault_or_workload(self):
        for changes in ({'scenario': 'rm-volumes'}, {'workload': 'shell'}):
            with self.assertRaises(ValueError): replace(Plan(), **changes).validate()

    def test_rate_is_explicitly_workflow_not_tps(self):
        self.assertIn('workflows', Plan().document()['rate_unit'])
        self.assertIn('skip', Plan().document()['overload_policy'])

    def test_hot_key_plan_targets_only_own_fixture_zero(self):
        self.assertEqual({x['target'] for x in Plan(workload='hot-key').operations()}, {0})

    def test_version_race_plan_is_explicit(self):
        self.assertEqual({x['kind'] for x in Plan(scenario='version-race').operations()}, {'race'})

    def test_list_and_plan_do_not_require_engine(self):
        with patch.object(manage, 'provider', side_effect=AssertionError('engine must not run')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(manage.main(['simulate', 'list']), 0)
                self.assertEqual(manage.main(['simulate', 'plan', 'row-lock']), 0)

    def test_confirmation_required(self):
        for args in (['simulate', 'run', 'baseline'], ['simulate', 'recover']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                manage.parser().parse_args(args)


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.cache, self.search, self.broker = MemoryRepo(), MemoryCache(), MemorySearch(), MemoryBroker()
        self.order, _ = self.repo.create(DATA, RUN+'-fixture-0')
        self.search.index(self.order)

    def test_inspection_does_not_fill_or_delete_or_refresh(self):
        self.cache.put = Mock(side_effect=AssertionError('no fill'))
        self.cache.delete = Mock(side_effect=AssertionError('no invalidate'))
        self.search.refresh = Mock(side_effect=AssertionError('no refresh'))
        result = inspect_order(self.repo, self.cache, self.search, self.order['id'])
        self.assertEqual(result['redis']['comparison'], 'missing')
        self.assertEqual(result['elasticsearch']['comparison'], 'match')
        self.assertTrue(result['read_only'])
        self.assertTrue(result['authoritative_valid'])

    def test_stale_cache_is_observed_not_repaired(self):
        self.cache.put(self.order)
        latest = self.repo.update(self.order['id'], {'status': 'paid', 'expected_version': 1})
        self.search.index(latest)
        result = inspect_order(self.repo, self.cache, self.search, self.order['id'])
        self.assertEqual(result['redis']['comparison'], 'mismatch')
        self.assertEqual(set(result['redis']['different_fields']), {'status', 'version'})
        self.assertEqual(self.cache.get(self.order['id'])['version'], 1)

    def test_wrong_search_payload_same_id_version_is_mismatch(self):
        self.search.docs[self.order['id']]['item'] = 'wrong'
        result = inspect_order(self.repo, self.cache, self.search, self.order['id'])
        self.assertEqual(result['elasticsearch']['different_fields'], ['item'])

    def test_missing_source_is_not_a_match(self):
        other = str(uuid.uuid4())
        result = inspect_order(self.repo, self.cache, self.search, other)
        self.assertFalse(result['authoritative_valid'])
        self.assertEqual(result['elasticsearch']['comparison'], 'unknown')

    def test_source_changes_during_inspection_are_unknown(self):
        before = copy.deepcopy(self.order)
        after = {**before, 'version': 2, 'status': 'paid'}
        self.repo.get = Mock(side_effect=[before, after])
        result = inspect_order(self.repo, self.cache, self.search, before['id'])
        self.assertFalse(result['sql_stable_during_observation'])
        self.assertEqual(result['redis']['comparison'], 'unknown')

    def test_backend_outage_is_unknown_not_missing(self):
        self.search.down = True
        result = inspect_order(self.repo, self.cache, self.search, self.order['id'])
        self.assertFalse(result['elasticsearch']['reachable'])
        self.assertEqual(result['elasticsearch']['comparison'], 'unknown')

    def test_types_are_part_of_field_comparison(self):
        self.assertIn('quantity', differences(self.order, {**self.order, 'quantity': True}))

    def test_rebuild_separates_skipped_old_version(self):
        self.search.docs[self.order['id']]['version'] = 8
        result = rebuild(self.repo, self.search)
        self.assertEqual(result, {'attempted': 1, 'indexed': 0, 'duplicates': 0, 'skipped_older_version': 1, 'errors': 0})

    def test_lookup_key_does_not_create_an_order(self):
        app = Application(self.repo, self.cache, self.search, self.broker)
        status, result = app.route('GET', '/api/study/key/'+RUN+'-absent', {})
        self.assertEqual(status, 200); self.assertIsNone(result['order'])
        self.assertEqual(len(self.repo.orders), 1)

    def test_lookup_restricts_to_synthetic_key_namespace(self):
        app = Application(self.repo, self.cache, self.search, self.broker)
        with self.assertRaises(Problem): app.route('GET', '/api/study/key/production-key', {})

    def test_app_info_binds_project(self):
        app = Application(self.repo, self.cache, self.search, self.broker)
        with patch.dict(os.environ, {'STUDY_PROJECT': PROJECT}):
            status, result = app.route('GET', '/api/study/info', {})
        self.assertEqual(result, {'application': 'db-lab-mvp', 'study_api': 2, 'project': PROJECT})


class SafeErrorTests(unittest.TestCase):
    def test_false_http_response_keeps_status_and_type(self):
        import requests
        for status, kind in [(400, 'mapper_parsing_exception'), (503, 'unavailable_shards_exception')]:
            response = requests.Response(); response.status_code = status
            response._content = json.dumps({'error': {'type': kind, 'reason': 'password=secret'}}).encode()
            result = safe_error(requests.HTTPError('dsn=secret', response=response))
            self.assertEqual(result['http_status'], status)
            self.assertEqual(result['error_type'], kind)
            self.assertNotIn('secret', json.dumps(result))

    def test_unknown_http_error_type_is_not_logged_verbatim(self):
        import requests
        response = requests.Response(); response.status_code = 400
        response._content = b'{"error":{"type":"password_secret"}}'
        result = safe_error(requests.HTTPError('secret', response=response))
        self.assertEqual(result['error_type'], 'unclassified_http_error')

    def test_sql_codes_are_classified_without_messages(self):
        for code, category in [(1205, 'lock_wait_timeout'), (1213, 'deadlock'), (1045, 'authentication')]:
            result = safe_error(Exception(code, 'password-secret'))
            self.assertEqual(result['category'], category)
            self.assertNotIn('password', json.dumps(result))

    def test_api_exposes_lock_category_not_raw_sql(self):
        with self.assertRaises(Problem) as raised:
            backend('mariadb', Mock(side_effect=Exception(1205, 'secret')))
        self.assertEqual(raised.exception.code, 'mariadb_lock_wait_timeout')

    def test_non_json_error_body_is_safe(self):
        import requests
        response = requests.Response(); response.status_code = 502; response._content = b'password-secret'
        result = safe_error(requests.HTTPError(response=response))
        self.assertEqual(result['http_status'], 502)
        self.assertNotIn('password', json.dumps(result))


class ReconciliationTests(unittest.TestCase):
    setUp = ObservationTests.setUp
    def intent(self, acknowledged=True):
        return {'payload': DATA, 'ids': [self.order['id']] if acknowledged else [],
                'acknowledged': {'1': self.order} if acknowledged else {}, 'attempted_updates': []}

    def test_acknowledged_missing_is_failure(self):
        result = inspect_intent(self.intent(), Response(200, {'order': None}, 0), None)
        self.assertEqual(result['state'], 'acknowledged_missing'); self.assertFalse(result['consistent'])

    def test_unknown_write_missing_is_not_claimed_data_loss(self):
        result = inspect_intent(self.intent(False), Response(200, {'order': None}, 0), None)
        self.assertEqual(result['state'], 'unacknowledged_absent'); self.assertTrue(result['consistent'])

    def test_timeout_cannot_be_counted_as_absent(self):
        result = inspect_intent(self.intent(), Response(0, {}, 5000), None)
        self.assertEqual(result['state'], 'unresolved'); self.assertFalse(result['consistent'])

    def test_committed_without_acknowledgement_is_identified(self):
        obs = inspect_order(self.repo, self.cache, self.search, self.order['id'])
        result = inspect_intent(self.intent(False), Response(200, {'order': self.order}, 0), Response(200, obs, 0))
        self.assertEqual(result['state'], 'present_without_ack'); self.assertTrue(result['consistent'])

    def test_latest_acknowledged_full_content_must_match(self):
        intent = self.intent(); intent['acknowledged']['1'] = {**self.order, 'status': 'paid'}
        obs = inspect_order(self.repo, self.cache, self.search, self.order['id'])
        result = inspect_intent(intent, Response(200, {'order': self.order}, 0), Response(200, obs, 0))
        self.assertIn('acknowledged_content_mismatch', result['errors'])

    def test_multiple_returned_ids_are_failure(self):
        intent = self.intent(); intent['ids'].append(str(uuid.uuid4()))
        obs = inspect_order(self.repo, self.cache, self.search, self.order['id'])
        result = inspect_intent(intent, Response(200, {'order': self.order}, 0), Response(200, obs, 0))
        self.assertIn('idempotency_identity_mismatch', result['errors'])

    def test_pending_projection_is_not_success(self):
        self.search.docs.clear()
        obs = inspect_order(self.repo, self.cache, self.search, self.order['id'])
        result = inspect_intent(self.intent(), Response(200, {'order': self.order}, 0), Response(200, obs, 0))
        self.assertIn('search_missing', result['errors'])

    def test_stale_cache_blocks_a_clean_convergence_verdict(self):
        self.cache.put({**self.order, 'item': 'stale'})
        obs = inspect_order(self.repo, self.cache, self.search, self.order['id'])
        result = inspect_intent(self.intent(), Response(200, {'order': self.order}, 0), Response(200, obs, 0))
        self.assertIn('cache_mismatch', result['errors'])



class MetricTests(unittest.TestCase):
    def test_no_sample_is_null_not_zero_latency(self):
        self.assertEqual(percentiles([]), {'count': 0, 'p50_ms': None, 'p95_ms': None, 'p99_ms': None})

    def test_nearest_rank_percentiles(self):
        self.assertEqual(percentiles(list(range(1, 101)))['p95_ms'], 95)

    def test_success_error_and_phase_stats_are_separate(self):
        rows = [{'scope': 'workload', 'phase': 'fault', 'operation': 'create', 'outcome': x, 'latency_ms': n}
                for x, n in [('success', 1), ('error', 2000)]]
        result = request_summary(rows)
        self.assertEqual(len(result), 2)
        self.assertEqual({r['p50_ms'] for r in result}, {1, 2000})

    def test_journal_is_private_and_contains_no_inherited_env(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root)/'run')
            journal.write('timeline', stage='test'); journal.json('plan.json', Plan().document()); journal.close()
            self.assertEqual((Path(root)/'run/timeline.jsonl').stat().st_mode & 0o777, 0o600)
            self.assertNotIn('PASSWORD', (Path(root)/'run/plan.json').read_text())

    def test_existing_output_directory_never_overwritten(self):
        with tempfile.TemporaryDirectory() as root, self.assertRaises(FileExistsError): Journal(root)

    def test_http_rejects_nonlocal_or_credentialed_endpoints(self):
        for url in ['https://example.com', 'http://example.com', 'http://localhost@evil.invalid',
                    'http://127.0.0.1/path', 'http://127.0.0.1#frag', 'http://x:y@127.0.0.1']:
            with self.subTest(url=url), self.assertRaises(ValueError): HTTP(url, RUN)

    def test_http_does_not_accept_absolute_request_paths(self):
        client = HTTP('http://127.0.0.1:1', RUN)
        with self.assertRaises(ValueError): client.call('GET', '//elsewhere/api')


class LockTests(unittest.TestCase):
    def connection(self, item=DATA['item']):
        conn = Mock(); cur = Mock(); conn.cursor.return_value.__enter__ = Mock(return_value=cur)
        conn.cursor.return_value.__exit__ = Mock(return_value=False)
        cur.fetchone.return_value = {'item': item}
        return conn, cur, Mock(connect=Mock(return_value=conn))

    def test_lock_is_owned_and_rollback_always_releases(self):
        conn, cur, repo = self.connection(); stop = Mock()
        hold(repo, str(uuid.uuid4()), RUN, 5, stop)
        self.assertTrue(any('FOR UPDATE' in x.args[0] for x in cur.execute.call_args_list))
        stop.wait.assert_called_once_with(5)
        conn.rollback.assert_called_once(); conn.close.assert_called_once()
        conn.commit.assert_not_called()

    def test_unrelated_order_not_locked(self):
        conn, cur, repo = self.connection('not-this-run')
        with self.assertRaises(ValueError): hold(repo, str(uuid.uuid4()), RUN, 2)
        self.assertFalse(any('FOR UPDATE' in x.args[0] for x in cur.execute.call_args_list))
        conn.rollback.assert_called_once(); conn.close.assert_called_once()

    def test_exception_after_acquisition_rolls_back(self):
        conn, cur, repo = self.connection(); stop = Mock(); stop.wait.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): hold(repo, str(uuid.uuid4()), RUN, 1, stop)
        conn.rollback.assert_called_once(); conn.close.assert_called_once()

    def test_lock_duration_and_identifier_bounds(self):
        for rid, seconds in [('prod', 1), (RUN, -1), (RUN, 31)]:
            repo = Mock()
            with self.assertRaises(ValueError): hold(repo, str(uuid.uuid4()), rid, seconds)
            repo.connect.assert_not_called()

    def test_db_session_uses_explicit_short_lock_wait_timeout(self):
        module = types.SimpleNamespace(connect=Mock(return_value=Mock()), cursors=types.SimpleNamespace(DictCursor=object))
        with patch.dict('sys.modules', {'pymysql': module}): SQL(Settings()).connect()
        self.assertEqual(module.connect.call_args.kwargs['init_command'], 'SET SESSION innodb_lock_wait_timeout=2')
