"""Loop health integration with explicit driver doubles, plus real process receipts."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch, call
from mvp_app import worker, worker_health
from mvp_app.core import new_order, envelope
from fakes import MemorySearch, MemoryCache

class LoopHealthTests(unittest.TestCase):
    def relay(self, failure=False):
        stop=threading.Event();health=Mock()
        def run(*_):
            stop.set()
            if failure:raise OSError('fixture failure')
            return False
        with patch.object(worker,'relay_once',side_effect=run),redirect_stdout(io.StringIO()):
            worker.relay_loop(stop,Mock(),Mock(),health)
        return health

    def test_empty_outbox_is_successful_relay_progress(self):
        self.relay().mark.assert_called_once_with('relay',True)

    def test_relay_error_immediately_marks_failure(self):
        self.relay(True).mark.assert_called_once_with('relay',False)

    def consumer(self, assigned=None, message=False, commit_error=False):
        stop=threading.Event();health=Mock();consumer=Mock()
        broker=Mock();broker.consumer.return_value=consumer;broker.s.kafka_topic='mvp.orders.v1'
        consumer.assignment.return_value=assigned or []
        consumer.get_watermark_offsets.return_value=(0,0)
        msg=Mock();msg.error.return_value=None;msg.partition.return_value=0;msg.offset.return_value=0
        msg.topic.return_value='mvp.orders.v1'
        msg.value.return_value=json.dumps(envelope(new_order({'item':'health','quantity':1,'unit_price':10},'health-test'))).encode()
        def poll(_):
            stop.set()
            return msg if message else None
        consumer.poll.side_effect=poll
        consumer.commit.return_value=[SimpleNamespace(topic='mvp.orders.v1',partition=0,offset=1,
                                                       error=OSError('fixture') if commit_error else None)]
        with redirect_stdout(io.StringIO()):
            worker.consume_loop(stop,broker,MemorySearch(),MemoryCache(),health)
        consumer.close.assert_called_once()
        return health,consumer

    def test_unassigned_idle_consumer_not_ready(self):
        health,consumer=self.consumer()
        self.assertNotIn(call('consumer',True),health.mark.call_args_list)
        consumer.commit.assert_not_called()

    def test_expected_partition_assignment_is_progress_but_shutdown_not_ready(self):
        health,_=self.consumer([SimpleNamespace(topic='mvp.orders.v1',partition=0,error=None)])
        self.assertEqual(health.mark.call_args_list,[call('consumer',True),call('consumer',False)])

    def test_wrong_assignment_never_reports_progress(self):
        for assignment in ([SimpleNamespace(topic='other',partition=0,error=None)],
                           [SimpleNamespace(topic='mvp.orders.v1',partition=1,error=None)]):
            with self.subTest(assignment=assignment):
                health,_=self.consumer(assignment)
                self.assertNotIn(call('consumer',True),health.mark.call_args_list)

    def test_successful_commit_precedes_success_receipt(self):
        health,consumer=self.consumer(message=True)
        consumer.commit.assert_called_once()
        self.assertIn(call('consumer',True),health.mark.call_args_list)
        self.assertEqual(health.mark.call_args_list[-1],call('consumer',False))

    def test_commit_failure_never_reports_success(self):
        health,_=self.consumer(message=True,commit_error=True)
        self.assertNotIn(call('consumer',True),health.mark.call_args_list)

class ProcessReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'health.json'

    def test_terminated_real_process_receipt_is_not_reused(self):
        script='''from pathlib import Path
import sys
from mvp_app.worker_health import WorkerHealth
h=WorkerHealth(Path(sys.argv[1]));h.mark('relay', True);h.mark('consumer', True)
print('receipt-written',flush=True)
sys.stdin.read(1)
'''
        child=subprocess.Popen([sys.executable,'-B','-c',script,str(self.path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        def finish():
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
        self.addCleanup(finish)
        # Avoid an unbounded readline: poll for the atomically created receipt.
        import time
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and not worker_health.is_ready(self.path):time.sleep(.01)
        self.assertTrue(worker_health.is_ready(self.path))
        child.communicate('x',timeout=5)
        self.assertEqual(child.returncode,0)
        self.assertFalse(worker_health.is_ready(self.path))

    def test_readiness_reader_does_not_modify_receipt(self):
        h=worker_health.WorkerHealth(self.path);h.mark('relay',True);h.mark('consumer',True)
        before=self.path.read_bytes();modified=self.path.stat().st_mtime_ns
        self.assertTrue(worker_health.is_ready(self.path))
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(self.path.stat().st_mtime_ns,modified)

    def test_close_invalidates_success(self):
        h=worker_health.WorkerHealth(self.path);h.mark('relay',True);h.mark('consumer',True)
        h.close();self.assertFalse(worker_health.is_ready(self.path))

    def test_restart_constructor_clears_previous_success(self):
        h=worker_health.WorkerHealth(self.path);h.mark('relay',True);h.mark('consumer',True)
        worker_health.WorkerHealth(self.path)
        self.assertFalse(worker_health.is_ready(self.path))

    def test_large_file_and_wrong_schema_refused(self):
        self.path.write_bytes(b'x'*4097);self.assertFalse(worker_health.is_ready(self.path))
        h=worker_health.WorkerHealth(self.path);h.mark('relay',True);h.mark('consumer',True)
        data=json.loads(self.path.read_text());data['schema']=True;self.path.write_text(json.dumps(data))
        self.assertFalse(worker_health.is_ready(self.path))


class IdleBrokerHealthTests(unittest.TestCase):
    def client(self):
        consumer=Mock()
        consumer.assignment.return_value=[SimpleNamespace(topic='mvp.orders.v1',partition=0,error=None)]
        consumer.get_watermark_offsets.return_value=(0,0)
        return consumer

    def test_idle_empty_topic_queries_broker_not_cached_assignment(self):
        consumer=self.client()
        self.assertTrue(worker.idle_consumer_ready(consumer,'mvp.orders.v1'))
        consumer.get_watermark_offsets.assert_called_once_with(consumer.assignment.return_value[0],timeout=2,cached=False)

    def test_missing_assignment_does_not_probe_unowned_partition(self):
        consumer=self.client();consumer.assignment.return_value=[]
        self.assertFalse(worker.idle_consumer_ready(consumer,'mvp.orders.v1'))
        consumer.get_watermark_offsets.assert_not_called()

    def test_timeout_or_invalid_offsets_not_ready(self):
        for offsets in (None,(0,-1),(2,1),(True,2),[0,1]):
            consumer=self.client();consumer.get_watermark_offsets.return_value=offsets
            with self.subTest(offsets=offsets),self.assertRaisesRegex(RuntimeError,'not confirmed'):
                worker.idle_consumer_ready(consumer,'mvp.orders.v1')

    def test_broker_error_is_not_masked_by_existing_assignment(self):
        consumer=self.client();consumer.get_watermark_offsets.side_effect=OSError('fixture')
        with self.assertRaises(OSError):worker.idle_consumer_ready(consumer,'mvp.orders.v1')
