"""Transport/admin safety tests use mock clients; they do not certify Kafka/ES."""
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch
from mvp_app.message_runtime import MessageRuntime, ScopedCache
from mvp_app.adapters import Broker, Settings
from mvp_app.message_safety import Scope
from test_adapters import Response

SCOPE=Scope('db-lab-mvp','msg-'+'a'*24,'b'*32)


def bare():
    rt=MessageRuntime.__new__(MessageRuntime)
    rt.scope=SCOPE;rt.search=Mock();rt.admin=Mock();rt.broker=Broker(Settings());rt.producer=None;rt.sent=0
    return rt


def ledger():return {'topics':{SCOPE.topic:'t1',SCOPE.dlq:'t2'},'index':{'name':SCOPE.index,'index_uuid':'i1'}}


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.rt=bare();self.rt.topic_names=Mock(return_value={SCOPE.topic,SCOPE.dlq})
        self.rt.describe=Mock(side_effect=lambda name:{SCOPE.topic:'t1',SCOPE.dlq:'t2'}[name])
        self.rt.index_identity=Mock(return_value='i1')
        self.rt.search.request.return_value=Response(200)
    def test_complete_verified(self):
        self.assertEqual(self.rt.verify_resources(ledger()),([SCOPE.topic,SCOPE.dlq],True))
    def test_no_uuid_no_adoption(self):
        with self.assertRaises(RuntimeError):self.rt.verify_resources({'topics':{}},allow_missing=True)
    def test_changed_uuid_blocks_all_removal(self):
        self.rt.describe=Mock(return_value='different')
        with self.assertRaises(RuntimeError):self.rt.cleanup(ledger())
        self.rt.admin.delete_topics.assert_not_called()
        self.assertNotIn('DELETE',[c.args[0] for c in self.rt.search.request.call_args_list])
    def test_foreign_topic_refused(self):
        data=ledger();data['topics']['mvp.orders.v1']='foreign'
        with self.assertRaises(ValueError):self.rt.cleanup(data)
        self.rt.admin.delete_topics.assert_not_called()
    def test_foreign_index_refused_before_deletion(self):
        data=ledger();data['index']['name']='mvp-orders-v1'
        with self.assertRaises(ValueError):self.rt.cleanup(data)
        self.rt.admin.delete_topics.assert_not_called()
    def test_index_uuid_change_blocks_topic_deletion(self):
        self.rt.index_identity.return_value='different'
        with self.assertRaises(RuntimeError):self.rt.cleanup(ledger())
        self.rt.admin.delete_topics.assert_not_called()
    def test_index_settings_http_error_not_missing(self):
        self.rt.search.request.return_value=Response(503)
        with self.assertRaises(OSError):self.rt.cleanup(ledger())
    def test_cleanup_may_continue_after_partial_deletion(self):
        self.rt.topic_names.return_value=set();self.rt.search.request.return_value=Response(404)
        self.assertTrue(self.rt.cleanup(ledger())['cleaned'])
        self.rt.admin.delete_topics.assert_not_called()
    def test_verified_cleanup_deletes_exact_names(self):
        self.rt.topic_names.side_effect=[{SCOPE.topic,SCOPE.dlq},set()]
        self.rt.admin.delete_topics.side_effect=lambda names,**kw:{names[0]:Mock()}
        result=self.rt.cleanup(ledger())
        self.assertTrue(result['cleaned'])
        self.assertEqual([c.args[0] for c in self.rt.admin.delete_topics.call_args_list],[[SCOPE.topic],[SCOPE.dlq]])
        self.rt.admin.delete_consumer_groups.assert_not_called()
    def test_existing_topic_not_adopted(self):
        with self.assertRaises(RuntimeError):self.rt.create_topic(SCOPE.topic)
        self.rt.admin.create_topics.assert_not_called()
    def test_foreign_create_topic_rejected(self):
        with self.assertRaises(ValueError):self.rt.create_topic('mvp.orders.v1')
    def test_new_index_conflict_refused(self):
        with self.assertRaises(RuntimeError):self.rt.create_index()
        self.assertEqual(self.rt.search.request.call_count,1)
    def test_missing_expected_topic_not_empty_success(self):
        self.rt.topic_names.return_value={SCOPE.dlq}
        with self.assertRaises(RuntimeError):self.rt.verify_resources(ledger())


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.rt=bare();self.consumer=Mock()
        self.consumer.get_watermark_offsets.return_value=(0,5)
        self.consumer.committed.return_value=[types.SimpleNamespace(offset=-1001,error=None)]
        class TP:
            def __init__(self,topic,partition,offset=None):self.topic,self.partition,self.offset=topic,partition,offset
        self.module=types.SimpleNamespace(Consumer=Mock(return_value=self.consumer),TopicPartition=TP)
    def read(self,**kwargs):
        with patch.dict(sys.modules,{'confluent_kafka':self.module}):return self.rt.reader(**kwargs)
    def test_manual_and_no_silent_reset(self):
        self.read()
        config=self.module.Consumer.call_args.args[0]
        self.assertFalse(config['enable.auto.commit']);self.assertFalse(config['enable.auto.offset.store'])
        self.assertEqual(config['auto.offset.reset'],'error');self.assertEqual(config['group.id'],SCOPE.group)
        self.assertEqual(self.consumer.assign.call_args.args[0][0].offset,0)
        self.consumer.subscribe.assert_not_called()
    def test_resume_from_committed(self):
        self.consumer.committed.return_value=[types.SimpleNamespace(offset=3,error=None)]
        self.read();self.assertEqual(self.consumer.assign.call_args.args[0][0].offset,3)
    def test_retention_gap_not_rewound(self):
        self.consumer.get_watermark_offsets.return_value=(3,5)
        self.consumer.committed.return_value=[types.SimpleNamespace(offset=1,error=None)]
        with self.assertRaises(RuntimeError):self.read()
        self.consumer.assign.assert_not_called();self.consumer.close.assert_called_once()
    def test_uninitialized_missing_prefix_not_rewound(self):
        self.consumer.get_watermark_offsets.return_value=(2,5)
        with self.assertRaises(RuntimeError):self.read()
    def test_ahead_of_log_rejected(self):
        self.consumer.committed.return_value=[types.SimpleNamespace(offset=9,error=None)]
        with self.assertRaises(RuntimeError):self.read()
    def test_commit_query_error_rejected(self):
        self.consumer.committed.return_value=[types.SimpleNamespace(offset=0,error=OSError())]
        with self.assertRaises(RuntimeError):self.read()
    def test_foreign_reader_topic_rejected(self):
        with self.assertRaises(ValueError):self.read(topic='mvp.orders.v1')
        self.module.Consumer.assert_not_called()
    def test_dlq_read_explicit_offset_does_not_commit(self):
        self.read(topic=SCOPE.dlq,offset=0)
        self.consumer.committed.assert_not_called();self.consumer.commit.assert_not_called()


class ProducerTests(unittest.TestCase):
    def setUp(self):self.rt=bare()
    def test_publish_waits_ack_and_returns_real_position(self):
        producer=Mock();sent={}
        producer.produce.side_effect=lambda *args,**kw:sent.update(kw)
        msg=types.SimpleNamespace(topic=lambda:SCOPE.dlq,partition=lambda:0,offset=lambda:7)
        producer.flush.side_effect=lambda seconds: sent['on_delivery'](None,msg) or 0
        with patch.dict(sys.modules,{'confluent_kafka':types.SimpleNamespace(Producer=Mock(return_value=producer))}):
            result=self.rt.publish(SCOPE.dlq,b'key',b'{}')
        self.assertEqual(result['offset'],7)
        self.assertEqual(producer.produce.call_args.args[0],SCOPE.dlq)
    def test_enqueue_without_callback_is_not_success(self):
        producer=Mock();producer.flush.return_value=0
        with patch.dict(sys.modules,{'confluent_kafka':types.SimpleNamespace(Producer=Mock(return_value=producer))}):
            with self.assertRaises(RuntimeError):self.rt.publish(SCOPE.topic,b'k',b'{}')
    def test_producer_failure_no_ack(self):
        producer=Mock();producer.flush.return_value=1
        with patch.dict(sys.modules,{'confluent_kafka':types.SimpleNamespace(Producer=Mock(return_value=producer))}):
            with self.assertRaises(RuntimeError):self.rt.publish(SCOPE.topic,b'k',b'{}')
    def test_wrong_topic_not_sent(self):
        with patch.dict(sys.modules,{'confluent_kafka':types.SimpleNamespace(Producer=Mock())}):
            with self.assertRaises(ValueError):self.rt.publish('mvp.orders.v1',b'k',b'{}')
    def test_publish_budget(self):
        self.rt.sent=100
        with patch.dict(sys.modules,{'confluent_kafka':types.SimpleNamespace(Producer=Mock())}):
            with self.assertRaises(RuntimeError):self.rt.publish(SCOPE.topic,b'k',b'{}')


class CacheTests(unittest.TestCase):
    def test_only_scoped_key_and_30_second_ttl(self):
        client=Mock();cache=ScopedCache(Settings(),SCOPE,client)
        order={'id':'order-id'}
        cache.put(order);cache.delete('order-id');client.get.return_value=None;cache.get('order-id')
        expected=SCOPE.cache_prefix+'order-id'
        self.assertEqual(client.set.call_args.args[0],expected)
        self.assertEqual(client.set.call_args.kwargs['ex'],30)
        client.delete.assert_called_once_with(expected);client.get.assert_called_once_with(expected)
        client.flushdb.assert_not_called()
