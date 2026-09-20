"""Policy/repair/fencing contracts with explicit doubles, NOT actual DB tests."""
import base64
import copy
import json
import types
import unittest
from unittest.mock import Mock
import requests
from mvp_app.adapters import Search, Settings
from mvp_app.core import Problem, envelope, new_order
from mvp_app.message_safety import *
from mvp_app.admin import rebuild
from fakes import MemoryRepo, MemoryCache, MemorySearch
from test_adapters import Response

SCOPE = Scope('db-lab-mvp', 'msg-' + 'a' * 24, 'b' * 32)
ORDER = new_order({'item': 'study', 'quantity': 1, 'unit_price': 4}, 'key')
POS = {'topic': SCOPE.topic, 'partition': 0, 'offset': 2}


def http_error(status, kind):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({'error': {'type': kind, 'reason': 'password=DO_NOT_PRINT'}}).encode()
    return requests.HTTPError('https://secret:password@host', response=response)


class ScopeTests(unittest.TestCase):
    def test_distinct_project_scopes(self):
        other = Scope('db-lab-mvp-other', SCOPE.run_id, SCOPE.token)
        self.assertNotEqual(other.topic, SCOPE.topic)
        self.assertNotEqual(other.index, SCOPE.index)
    def test_distinct_run_scopes(self):
        other = Scope(SCOPE.project, 'msg-' + 'c'*24, SCOPE.token)
        self.assertNotEqual(other.group, SCOPE.group)
    def test_no_normal_names(self):
        self.assertNotIn('mvp.orders.v1', SCOPE.public().values())
        self.assertNotIn('mvp.search.v1', SCOPE.public().values())
        self.assertNotEqual(SCOPE.cache_prefix, 'mvp:order:')
    def test_reject_wildcard_run(self):
        with self.assertRaises(ValueError): Scope(SCOPE.project, '*', SCOPE.token)
    def test_reject_path_project(self):
        with self.assertRaises(ValueError): Scope('../db-lab-mvp', SCOPE.run_id, SCOPE.token)
    def test_reject_short_token(self):
        with self.assertRaises(ValueError): Scope(SCOPE.project, SCOPE.run_id, 'a')
    def test_public_scope_excludes_token(self): self.assertNotIn('token', SCOPE.public())


class DecodeTests(unittest.TestCase):
    def test_valid(self): self.assertEqual(decode(canonical(envelope(ORDER)))['order'], ORDER)
    def test_duplicate_keys_rejected(self):
        with self.assertRaises(Problem): decode(b'{"schema_version":1,"schema_version":99}')
    def test_nan_rejected(self):
        with self.assertRaises(Problem): decode(b'{"x":NaN}')
    def test_invalid_utf8_rejected(self):
        with self.assertRaises(Problem): decode(b'\xff\xfe')
    def test_oversized_rejected(self):
        with self.assertRaises(Problem): decode(b' ' * (MAX_MESSAGE+1))
    def test_empty_rejected(self):
        with self.assertRaises(Problem): decode(b'')
    def test_unsupported_schema(self):
        value = envelope(ORDER); value['schema_version'] = 99
        with self.assertRaises(Problem) as error: decode(canonical(value))
        self.assertEqual(error.exception.code, 'unsupported_event_schema')
    def test_malformed_json(self):
        with self.assertRaises(Problem): decode(b'{')
    def test_bool_schema_not_integer(self):
        value = envelope(ORDER); value['schema_version'] = True
        with self.assertRaises(Problem): decode(canonical(value))


class MessagePolicyTests(unittest.TestCase):
    def setUp(self):
        self.search, self.cache, self.commit, self.dlq = MemorySearch(), MemoryCache(), Mock(), Mock(return_value={'offset': 0})
        self.raw = canonical(envelope(ORDER))
    def process(self, **extra):
        options = dict(scope=SCOPE, topic_id='topic-uuid', position=POS, key=ORDER['id'].encode(), raw=self.raw,
                       search=self.search, cache=self.cache, commit=self.commit, quarantine=self.dlq)
        options.update(extra)
        return process_record(**options)
    def poison(self):
        value = envelope(ORDER); value['schema_version'] = 99
        return canonical(value)
    def test_normal_commit_after_index(self):
        self.commit.side_effect = lambda: self.assertIn(ORDER['id'], self.search.docs)
        self.process(); self.commit.assert_called_once(); self.dlq.assert_not_called()
    def test_strict_never_skips_poison(self):
        with self.assertRaises(PoisonBlocked): self.process(raw=self.poison())
        self.commit.assert_not_called(); self.dlq.assert_not_called()
    def test_quarantine_ack_before_source_commit(self):
        order = []
        self.dlq.side_effect = lambda x: order.append('ack') or {'offset': 0}
        self.commit.side_effect = lambda: order.append('commit')
        result = self.process(raw=self.poison(), policy='quarantine')
        self.assertEqual(order, ['ack','commit']); self.assertEqual(result['outcome'], 'quarantined')
    def test_dlq_failure_does_not_commit(self):
        self.dlq.side_effect = OSError('no ack')
        with self.assertRaises(OSError): self.process(raw=self.poison(), policy='quarantine')
        self.commit.assert_not_called()
    def test_commit_failure_not_quarantined(self):
        self.commit.side_effect = OSError('commit')
        with self.assertRaises(OSError): self.process(policy='quarantine')
        self.dlq.assert_not_called()
    def test_transient_503_never_quarantined(self):
        self.search.index = Mock(side_effect=http_error(503, 'unavailable_shards_exception'))
        with self.assertRaises(requests.HTTPError): self.process(policy='quarantine')
        self.dlq.assert_not_called(); self.commit.assert_not_called()
    def test_429_never_quarantined(self):
        self.search.index = Mock(side_effect=http_error(429, 'es_rejected_execution_exception'))
        with self.assertRaises(requests.HTTPError): self.process(policy='quarantine')
        self.dlq.assert_not_called()
    def test_auth_401_never_quarantined(self):
        self.search.index = Mock(side_effect=http_error(401, 'security_exception'))
        with self.assertRaises(requests.HTTPError): self.process(policy='quarantine')
        self.dlq.assert_not_called()
    def test_unknown_400_never_quarantined(self):
        self.search.index = Mock(side_effect=http_error(400, 'illegal_argument_exception'))
        with self.assertRaises(requests.HTTPError): self.process(policy='quarantine')
        self.dlq.assert_not_called()
    def test_mapping_400_quarantined(self):
        self.search.index = Mock(side_effect=http_error(400, 'strict_dynamic_mapping_exception'))
        result = self.process(policy='quarantine')
        self.assertEqual(result['reason'], 'strict_dynamic_mapping_exception')
        self.commit.assert_called_once()
    def test_same_version_corruption_quarantined(self):
        self.search.index = Mock(side_effect=Problem(409, 'event_version_payload_conflict'))
        self.assertEqual(self.process(policy='quarantine')['outcome'], 'quarantined')
    def test_foreign_metadata_conflict_not_skipped(self):
        self.search.index = Mock(side_effect=Problem(409, 'projection_version_metadata_mismatch'))
        with self.assertRaises(Problem): self.process(policy='quarantine')
        self.dlq.assert_not_called()
    def test_projection_checkpoint_has_no_commit(self):
        def crash(_): raise InjectedBoundary()
        with self.assertRaises(InjectedBoundary): self.process(checkpoint=crash)
        self.assertIn(ORDER['id'], self.search.docs); self.commit.assert_not_called()
    def test_quarantine_checkpoint_has_no_commit(self):
        def crash(_): raise InjectedBoundary()
        with self.assertRaises(InjectedBoundary): self.process(raw=self.poison(), policy='quarantine', checkpoint=crash)
        self.dlq.assert_called_once(); self.commit.assert_not_called()
    def test_key_mismatch_is_poison(self):
        with self.assertRaises(PoisonBlocked): self.process(key=b'wrong')
        self.commit.assert_not_called()
    def test_invalid_cache_does_not_block_projected_order(self):
        self.cache.delete = Mock(side_effect=OSError())
        result = self.process(); self.assertIsNotNone(result['cache_error']); self.commit.assert_called_once()
    def test_oversized_poison_not_silently_truncated(self):
        with self.assertRaises(ValueError): self.process(raw=b'a'*(MAX_MESSAGE+1), policy='quarantine')
        self.commit.assert_not_called(); self.dlq.assert_not_called()
    def test_unknown_policy_rejected(self):
        with self.assertRaises(ValueError): self.process(policy='ignore')
    def test_raw_poison_preserved_exactly(self):
        self.process(raw=b'{invalid', policy='quarantine')
        record = self.dlq.call_args.args[0]
        self.assertEqual(base64.b64decode(record['raw_b64']), b'{invalid')
        self.assertNotIn('password', json.dumps(record))


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepo(); self.key = SCOPE.run_id+'-a'
        self.order, _ = self.repo.create({'item':'fixture', 'quantity':1, 'unit_price':1}, self.key)
        self.allowed = {self.order['id']: self.key}
        self.record = quarantine_record(SCOPE, 'topic-uuid', POS, self.order['id'].encode(), b'{malformed', 'invalid_event_json')
    def test_identity_stable_for_redelivery(self):
        other = quarantine_record(SCOPE, 'topic-uuid', POS, self.order['id'].encode(), b'{malformed', 'invalid_event_json')
        self.assertEqual(other, self.record)
    def test_position_changes_identity(self):
        other = quarantine_record(SCOPE, 'topic-uuid', {**POS,'offset':3}, self.order['id'].encode(), b'{malformed', 'invalid_event_json')
        self.assertNotEqual(other['dlq_id'], self.record['dlq_id'])
    def test_topic_uuid_changes_identity(self):
        other = quarantine_record(SCOPE, 'other-uuid', POS, self.order['id'].encode(), b'{malformed', 'invalid_event_json')
        self.assertNotEqual(other['dlq_id'], self.record['dlq_id'])
    def test_repair_uses_current_sql_not_poison_bytes(self):
        changed = self.repo.update(self.order['id'], {'expected_version':1, 'status':'paid'})
        event = repair_event(self.record, SCOPE, 'topic-uuid', self.allowed, self.repo)
        self.assertEqual(event['order'], changed); self.assertEqual(event['repair']['basis'], 'current_sql')
    def test_repeated_repair_has_deterministic_event_id(self):
        event = repair_event(self.record,SCOPE,'topic-uuid',self.allowed,self.repo)
        self.assertEqual(event, repair_event(self.record,SCOPE,'topic-uuid',self.allowed,self.repo))
    def test_missing_sql_does_not_create(self):
        self.repo.orders.clear()
        with self.assertRaises(ValueError): repair_event(self.record,SCOPE,'topic-uuid',self.allowed,self.repo)
        self.assertEqual(len(self.repo.orders), 0)
    def test_foreign_order_refused(self):
        with self.assertRaises(ValueError): repair_event(self.record,SCOPE,'topic-uuid',{},self.repo)
    def test_foreign_owner_refused(self):
        record = {**self.record,'owner':'c'*32}
        with self.assertRaises(ValueError): checked_quarantine(record,SCOPE,'topic-uuid')
    def test_changed_payload_refused(self):
        record = {**self.record,'raw_b64':base64.b64encode(b'changed').decode()}
        with self.assertRaises(ValueError): checked_quarantine(record,SCOPE,'topic-uuid')
    def test_changed_hash_refused(self):
        with self.assertRaises(ValueError): checked_quarantine({**self.record,'raw_sha256':'0'*64},SCOPE,'topic-uuid')
    def test_changed_topic_uuid_refused(self):
        with self.assertRaises(ValueError): checked_quarantine(self.record,SCOPE,'other-uuid')
    def test_negative_source_offset_refused(self):
        with self.assertRaises(ValueError): quarantine_record(SCOPE,'uuid',{**POS,'offset':-1},b'x',b'{}','invalid')


class VersionFenceTests(unittest.TestCase):
    def search(self, stored, version=1):
        session = Mock(); session.request.side_effect = [Response(200),Response(409,{'error':{'type':'version_conflict_engine_exception'}}),
                                                      Response(200,{'_version':version,'_source':stored})]
        return Search(Settings(),session),session
    def test_identical_redelivery_is_noop(self):
        search,_ = self.search(dict(ORDER))
        self.assertEqual(search.index(ORDER),'duplicate_ignored')
    def test_conflicting_same_version_is_blocked(self):
        search,_ = self.search({**ORDER,'item':'corrupt'})
        with self.assertRaises(Problem) as exc: search.index(ORDER)
        self.assertEqual(exc.exception.code,'event_version_payload_conflict')
    def test_newer_stored_version_wins(self):
        search,_=self.search({**ORDER,'version':2},2)
        self.assertEqual(search.index(ORDER),'older_version_ignored')
    def test_metadata_version_mismatch_blocks(self):
        search,_=self.search(ORDER,9)
        with self.assertRaises(Problem):search.index(ORDER)
    def test_foreign_doc_id_blocks(self):
        search,_=self.search({**ORDER,'id':'foreign'})
        with self.assertRaises(Problem):search.index(ORDER)
    def test_false_vs_zero_not_identical(self):
        search,_=self.search({**ORDER,'unit_price':False,'total':False})
        order={**ORDER,'unit_price':0,'total':0}
        with self.assertRaises(Problem): search.index(order)
    def test_realtime_get_failure_blocks_commit_path(self):
        search, session=self.search(ORDER)
        session.request.side_effect=[Response(200),Response(409,{'error':{'type':'version_conflict_engine_exception'}}),Response(503)]
        with self.assertRaises(OSError):search.index(ORDER)
    def test_no_second_put_on_collision(self):
        search,session=self.search({**ORDER,'item':'corrupt'})
        with self.assertRaises(Problem):search.index(ORDER)
        self.assertEqual([call.args[0] for call in session.request.call_args_list],['HEAD','PUT','GET'])
    def test_rebuild_counts_duplicates_separately(self):
        search=Mock();search.index.return_value='duplicate_ignored'
        repo=Mock();repo.scan.return_value=[ORDER]
        self.assertEqual(rebuild(repo,search),{'attempted':1,'indexed':0,'duplicates':1,'skipped_older_version':0,'errors':0})
