"""Logical backup contract tests. DBs are explicit doubles, not MariaDB durability tests."""
import copy
from datetime import datetime
import os
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch
from contextlib import contextmanager
from mvp_app import snapshot
from mvp_app.adapters import Settings
from mvp_app.core import new_order

RUN = 'drill-0123456789ab'
ORDER = {**new_order({'item': '한글', 'quantity': 2, 'unit_price': 7}, 'key'), 'idempotency_key': 'key'}
OUT = {'seq': 7, 'event_id': 'event', 'payload': '{"version":1}', 'created_at': '2026-09-19 01:02:03.123000', 'sent_at': None}

def bundle():
    return snapshot.seal({'orders': [copy.deepcopy(ORDER)], 'outbox': [dict(OUT)]}, '11.8-test-double')

def connection():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    return conn, cur

class SnapshotTests(TestCase):
    def test_unicode_roundtrip_and_stable_hash(self):
        self.assertEqual(snapshot.validate(bundle()), bundle())
        self.assertIn('한글', snapshot.canonical(bundle()).decode())

    def test_corrupt_order_is_rejected(self):
        value = bundle(); value['tables']['orders'][0]['total'] = 999
        with self.assertRaises(ValueError): snapshot.validate(value)

    def test_corrupt_outbox_is_rejected(self):
        value = bundle(); value['tables']['outbox'][0]['sent_at'] = 'other'
        with self.assertRaises(ValueError): snapshot.validate(value)

    def test_source_server_version_not_part_of_row_hash(self):
        value = bundle(); value['server_version'] = 'different engine'
        self.assertEqual(snapshot.validate(value)['sha256'], bundle()['sha256'])

    def test_extra_table_rejected(self):
        value = bundle(); value['tables']['accounts'] = []
        with self.assertRaises(ValueError): snapshot.validate(value)

    def test_extra_column_rejected_before_sql(self):
        value = bundle(); value['tables']['orders'][0]['anything);DROP TABLE orders'] = 1
        with self.assertRaises(ValueError): snapshot.validate(value)

    def test_boolean_not_an_integer_row_value(self):
        value = bundle(); value['tables']['orders'][0]['quantity'] = True
        with self.assertRaises(ValueError): snapshot.validate(value)

    def test_duplicate_primary_keys_rejected(self):
        value = bundle(); value['tables']['orders'] *= 2
        with self.assertRaises(ValueError): snapshot.validate(value)

    def test_row_limit_enforced(self):
        with patch.object(snapshot, 'MAX_ROWS', 0), self.assertRaises(ValueError): snapshot.validate(bundle())

    def test_byte_limit_enforced(self):
        with patch.object(snapshot, 'MAX_BYTES', 20), self.assertRaises(ValueError): bundle()

    def test_no_mutation_of_input_during_validation(self):
        value = bundle(); old = copy.deepcopy(value)
        snapshot.validate(value); self.assertEqual(value, old)

    def test_export_uses_one_read_only_snapshot_and_utc(self):
        repo = Mock(); conn, cur = connection(); repo.connect.return_value = conn
        cur.fetchone.side_effect = [{'server_version': 'test'}, {'row_count': 1, 'payload_bytes': 20}]
        cur.fetchall.side_effect = [[{'name': 'orders', 'engine': 'InnoDB'}, {'name': 'outbox', 'engine': 'InnoDB'}],
                                   [ORDER], [{**OUT, 'created_at': datetime(2026, 9, 19, 1, 2, 3, 123000)}]]
        result = snapshot.export_snapshot(repo)
        statements = [c.args[0] for c in cur.execute.call_args_list]
        self.assertIn('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY', statements)
        self.assertIn("SET SESSION time_zone='+00:00'", statements)
        self.assertFalse(any(s.startswith(('UPDATE', 'INSERT', 'DELETE', 'DROP')) for s in statements))
        self.assertEqual(result['tables']['outbox'][0]['created_at'], OUT['created_at'])
        conn.rollback.assert_called_once(); conn.close.assert_called_once(); conn.commit.assert_not_called()

    def test_export_unknown_nontransactional_table_is_not_partial_success(self):
        repo = Mock(); conn, cur = connection(); repo.connect.return_value = conn
        cur.fetchone.side_effect = [{'server_version': 'test'}, {'row_count': 1, 'payload_bytes': 20}]
        cur.fetchall.return_value = [{'name': 'orders', 'engine': 'MyISAM'}]
        with self.assertRaises(ValueError): snapshot.export_snapshot(repo)
        conn.close.assert_called_once(); conn.rollback.assert_called_once()

    def test_restore_refuses_normal_application_database(self):
        repo = Mock(s=Settings())
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError): snapshot.restore_snapshot(repo, bundle())
        repo.initialize.assert_not_called(); repo.transaction.assert_not_called()

    def test_restore_refuses_non_loopback_even_with_marker(self):
        with patch.dict(os.environ, {'MVP_DISPOSABLE': RUN}), self.assertRaises(ValueError):
            snapshot.disposable(Settings(sql_host='mariadb'))

    def repo(self, existing=False):
        conn, cur = connection()
        cur.fetchall.return_value = [{'TABLE_NAME': 'orders'}] if existing else []
        repo = Mock(s=Settings(sql_host='127.0.0.1'))
        @contextmanager
        def tx(): yield conn
        repo.transaction = tx
        return repo, cur

    def test_restore_refuses_existing_schema(self):
        repo, _ = self.repo(existing=True)
        with patch.dict(os.environ, {'MVP_DISPOSABLE': RUN}), self.assertRaises(ValueError):
            snapshot.restore_snapshot(repo, bundle())
        repo.initialize.assert_not_called()

    def test_restore_binds_values_and_checks_complete_hash_and_canary(self):
        repo, cur = self.repo()
        order = {k:v for k,v in ORDER.items() if k != 'idempotency_key'}
        changed = {**order, 'version': 2, 'status': 'paid'}
        repo.create.side_effect = [(order, True), (order, False)]
        repo.update.return_value = changed; repo.get.return_value = changed
        with patch.dict(os.environ, {'MVP_DISPOSABLE': RUN}), patch.object(snapshot, 'export_snapshot', return_value=bundle()):
            result = snapshot.restore_snapshot(repo, bundle())
        self.assertTrue(result['matched']); self.assertTrue(result['canary_passed'])
        inserts = [c for c in cur.execute.call_args_list if c.args[0].startswith('INSERT')]
        self.assertEqual(len(inserts), 2)
        self.assertNotIn(ORDER['item'], inserts[0].args[0])
        self.assertIn(ORDER['item'], inserts[0].args[1])

    def test_hash_mismatch_never_runs_canary(self):
        repo, _ = self.repo()
        wrong = snapshot.seal({'orders': [], 'outbox': []}, 'test')
        with patch.dict(os.environ, {'MVP_DISPOSABLE': RUN}), patch.object(snapshot, 'export_snapshot', return_value=wrong):
            result = snapshot.restore_snapshot(repo, bundle())
        self.assertFalse(result['matched']); self.assertFalse(result['canary_passed']); repo.create.assert_not_called()

    def test_bad_backup_rejected_before_initialize(self):
        repo, _ = self.repo(); value = bundle(); value['sha256'] = 'bad'
        with patch.dict(os.environ, {'MVP_DISPOSABLE': RUN}), self.assertRaises(ValueError):
            snapshot.restore_snapshot(repo, value)
        repo.initialize.assert_not_called()
