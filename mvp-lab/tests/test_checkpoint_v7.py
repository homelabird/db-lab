"""Synchronous commit response contract; explicit Kafka doubles, not broker evidence."""
import types
import unittest
from unittest.mock import Mock
from mvp_app.worker import acknowledge, CheckpointError
from mvp_app.observability import safe_error

class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.consumer=Mock();self.message=Mock()
        self.message.topic.return_value='mvp.orders.v1';self.message.partition.return_value=0;self.message.offset.return_value=7
    def check(self,**changes):
        row=dict(topic='mvp.orders.v1',partition=0,offset=8,error=None);row.update(changes)
        self.consumer.commit.return_value=[types.SimpleNamespace(**row)]
        acknowledge(self.consumer,self.message)
    def test_exact_next_offset_is_confirmed(self):
        self.check();self.consumer.commit.assert_called_once_with(message=self.message,asynchronous=False)
    def test_none_not_a_confirmation(self):
        self.consumer.commit.return_value=None
        with self.assertRaises(CheckpointError):acknowledge(self.consumer,self.message)
    def test_empty_result_not_a_confirmation(self):
        self.consumer.commit.return_value=[]
        with self.assertRaises(CheckpointError):acknowledge(self.consumer,self.message)
    def test_multiple_positions_refused(self):
        self.consumer.commit.return_value=[Mock(),Mock()]
        with self.assertRaises(CheckpointError):acknowledge(self.consumer,self.message)
    def test_other_topic_refused(self):
        with self.assertRaises(CheckpointError):self.check(topic='other')
    def test_other_partition_refused(self):
        with self.assertRaises(CheckpointError):self.check(partition=1)
    def test_old_offset_refused(self):
        with self.assertRaises(CheckpointError):self.check(offset=7)
    def test_future_offset_refused(self):
        with self.assertRaises(CheckpointError):self.check(offset=9)
    def test_boolean_partition_not_integer(self):
        with self.assertRaises(CheckpointError):self.check(partition=False)
    def test_offset_string_refused(self):
        with self.assertRaises(CheckpointError):self.check(offset='8')
    def test_partition_error_refused(self):
        with self.assertRaises(CheckpointError):self.check(error=OSError('private endpoint'))
    def test_log_has_constant_checkpoint_reason(self):
        result=safe_error(CheckpointError('private endpoint'))
        self.assertEqual(result['code'],'kafka_checkpoint_unconfirmed');self.assertNotIn('private',str(result))
    def test_commit_exception_propagates(self):
        self.consumer.commit.side_effect=OSError()
        with self.assertRaises(OSError):acknowledge(self.consumer,self.message)
