from __future__ import annotations
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import random
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client'))
import client
import dataset


class DataTests(unittest.TestCase):
    def event(self, kind='payments', payload=32, hot=False, n=3):
        return dataset.make_event(kind, n, random.Random(42), 'test-run',
                                  datetime(2026, 1, 1, tzinfo=timezone.utc), payload, hot)
    def test_three_schemas(self):
        expected = {'payments': 'amount_krw', 'access': 'client_ip', 'metrics': 'temperature_c'}
        for kind, key in expected.items():
            with self.subTest(kind=kind):
                k, v = self.event(kind)
                self.assertTrue(k)
                self.assertIn(key, json.loads(v))
                self.assertTrue(json.loads(v)['synthetic'])
    def test_reproducible_with_fixed_seed_and_time(self):
        self.assertEqual(self.event(), self.event())
    def test_payload_exact_size(self):
        for size in [0, 1, 1024]:
            self.assertEqual(len(json.loads(self.event(payload=size)[1])['payload'].encode()), size)
    def test_hot_key(self):
        self.assertEqual(self.event(hot=True)[0], self.event('access', hot=True)[0])
    def test_event_ids_unique(self):
        self.assertNotEqual(json.loads(self.event(n=1)[1])['event_id'], json.loads(self.event(n=2)[1])['event_id'])
    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError): self.event('unknown')
    def test_negative_padding_rejected(self):
        with self.assertRaises(ValueError): self.event(payload=-1)
    def test_negative_sequence_rejected(self):
        with self.assertRaises(ValueError): self.event(n=-1)


def snapshot():
    return {'brokers': [1,2,3], 'controller': 1, 'topics': {
        'lab.t': {'error': None, 'partitions': [
            {'partition': 0, 'leader': 1, 'replicas': [1,2,3], 'isr': [1,2,3], 'error': None}]}}}


class StateTests(unittest.TestCase):
    def test_healthy(self): self.assertEqual(client.check_state(snapshot(), 3), [])
    def test_missing_broker(self): self.assertTrue(client.check_state(snapshot(), 2))
    def test_full_isr_required(self):
        s=snapshot(); s['topics']['lab.t']['partitions'][0]['isr']=[1,2]
        self.assertTrue(client.check_state(s, 3))
    def test_explicit_degraded_isr(self):
        s=snapshot(); s['brokers']=[1,2]; s['topics']['lab.t']['partitions'][0]['isr']=[1,2]
        self.assertEqual(client.check_state(s,2,2,'lab.t'), [])
    def test_offline_partition(self):
        s=snapshot(); s['topics']['lab.t']['partitions'][0]['leader']=-1
        self.assertTrue(client.check_state(s))
    def test_controller_failover_not_observed(self):
        self.assertTrue(client.check_state(snapshot(), controller_not=1))
    def test_leader_failover_not_observed(self):
        self.assertTrue(client.check_state(snapshot(), leader_not=1))
    def test_missing_topic_not_vacuous_success(self):
        self.assertTrue(client.check_state(snapshot(), topic='lab.missing'))
    def test_invalid_isr_member(self):
        s=snapshot(); s['topics']['lab.t']['partitions'][0]['isr']=[1,2,9]
        self.assertTrue(client.check_state(s))
    def test_invalid_controller(self):
        s=snapshot(); s['controller']=-1
        self.assertTrue(client.check_state(s))
    def test_topic_error_not_healthy(self):
        s=snapshot(); s['topics']['lab.t']['error']='unavailable'
        self.assertTrue(client.check_state(s))
    def test_topic_guard(self):
        self.assertEqual(client.checked_topic('lab.ok-1.x'), 'lab.ok-1.x')
        for value in ['production', 'lab.', '../lab.x', 'lab.x;rm -rf /', '__consumer_offsets']:
            with self.subTest(value=value), self.assertRaises(client.LabError):
                client.checked_topic(value)
    def test_config_parser(self):
        self.assertEqual(client.parse_configs(['min.insync.replicas=2']), {'min.insync.replicas':'2'})
        for value in ['bad', '=bad', 'bad=']:
            with self.assertRaises(client.LabError): client.parse_configs([value])


class FakeMessage:
    def __init__(self, value=b'{}', partition=0, offset=0, error=None):
        self._value, self._partition, self._offset, self._error=value, partition, offset, error
    def latency(self): return .001
    def value(self): return self._value
    def partition(self): return self._partition
    def offset(self): return self._offset
    def error(self): return self._error
    def topic(self): return 'lab.t'
    def key(self): return b'k'


class FakeError:
    def __init__(self, name, code=19): self._name, self._code=name, code
    def name(self): return self._name
    def code(self): return self._code
    def __str__(self): return self._name


class FakeProducer:
    def __init__(self, err=None, pending=0): self.err, self.pending, self.callbacks=err, pending, []
    def produce(self, topic, key=None, value=None, on_delivery=None, partition=None):
        self.callbacks.append((on_delivery, FakeMessage(value)))
    def poll(self, timeout): pass
    def flush(self, timeout):
        if not self.pending:
            for cb, msg in self.callbacks: cb(self.err, msg)
        return self.pending


class ProducerTests(unittest.TestCase):
    def args(self, **kwargs):
        a=dict(bootstrap='fake',topic='lab.t',kind='payments',count=7,mib=None,duration=None,
               rate=0,payload_bytes=16,hot_key=False,seed=42,run_id='test')
        a.update(kwargs); return argparse.Namespace(**a)
    def publish(self, producer=None, **kwargs):
        with patch.object(client,'metadata',return_value=snapshot()), \
             patch.object(client,'make_producer',return_value=producer or FakeProducer()), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return client.publish(self.args(**kwargs))
    def test_all_delivery_callbacks_required(self):
        out=self.publish(); self.assertEqual(out['queued'],7); self.assertEqual(out['delivered'],7)
    def test_delivery_error_is_failure(self):
        with self.assertRaises(client.LabError): self.publish(FakeProducer(FakeError('NOT_ENOUGH_REPLICAS')))
    def test_flush_pending_is_failure(self):
        with self.assertRaises(client.LabError): self.publish(FakeProducer(pending=7))
    def test_size_target_streaming(self):
        out=self.publish(count=None,mib=.002)
        self.assertGreaterEqual(out['logical_bytes'], .002*1048576)
        self.assertLess(out['logical_bytes'], .002*1048576+1000)
    def test_negative_rate_rejected(self):
        with self.assertRaises(client.LabError): self.publish(rate=-1)
    def test_nonfinite_rate_rejected(self):
        for rate in [float("nan"), float("inf"), float("-inf")]:
            with self.subTest(rate=rate), self.assertRaises(client.LabError):
                self.publish(rate=rate)
    def test_low_rate_respects_duration_deadline(self):
        clock=[0.0]
        p=FakeProducer()
        def poll(timeout): clock[0] += timeout
        p.poll=poll
        with patch.object(client.time,"monotonic",side_effect=lambda:clock[0]):
            out=self.publish(p,count=None,duration=2,rate=.00001)
        self.assertEqual(out["queued"],1)
        self.assertEqual(out["delivered"],1)
        self.assertLessEqual(clock[0],2.001)
    def test_large_mib_rejected(self):
        with self.assertRaises(client.LabError): self.publish(count=None,mib=513)
    def test_large_count_rejected(self):
        with self.assertRaises(client.LabError): self.publish(count=2_000_001)
    def test_large_padding_rejected(self):
        with self.assertRaises(client.LabError): self.publish(payload_bytes=2_097_153)


class FakeResource:
    def __init__(self, *_args): pass


class ProbeTests(unittest.TestCase):
    def run_probe(self, err, kind='isr', isr=1, cfg=None):
        s=snapshot(); s['topics']['lab.t']['partitions'][0]['isr']=[1,2,3][:isr]
        config=cfg or {'min.insync.replicas':types.SimpleNamespace(value='2'),
                       'max.message.bytes':types.SimpleNamespace(value='1024')}
        class Admin:
            def describe_configs(self, resources, **kwargs):
                return {resources[0]:types.SimpleNamespace(result=lambda timeout:config)}
        ka=types.SimpleNamespace(ConfigResource=FakeResource,ResourceType=types.SimpleNamespace(TOPIC=1))
        with patch.object(client,'kafka_modules',return_value=(None,ka)), \
             patch.object(client,'metadata',return_value=s), \
             patch.object(client,'admin_client',return_value=Admin()), \
             patch.object(client,'make_producer',return_value=FakeProducer(err)), \
             redirect_stdout(io.StringIO()):
            client.probe_failure(argparse.Namespace(bootstrap='fake',topic='lab.t',kind=kind))
    def test_specific_isr_error(self): self.run_probe(FakeError('NOT_ENOUGH_REPLICAS'))
    def test_timeout_not_accepted_as_isr_error(self):
        with self.assertRaises(client.LabError): self.run_probe(FakeError('_MSG_TIMED_OUT'))
    def test_success_not_accepted_as_failure(self):
        with self.assertRaises(client.LabError): self.run_probe(None)
    def test_isr_precondition_enforced(self):
        with self.assertRaises(client.LabError): self.run_probe(FakeError('NOT_ENOUGH_REPLICAS'),isr=3)
    def test_specific_oversize_error(self): self.run_probe(FakeError('MSG_SIZE_TOO_LARGE'),kind='oversize',isr=3)


class ScannerTests(unittest.TestCase):
    def run_scan(self, values, rows, limit=None):
        class TP:
            def __init__(self, topic, partition, offset): self.topic,self.partition,self.offset=topic,partition,offset
        class C:
            def __init__(self): self.closed=False; self.values=iter(values)
            def assign(self, parts): self.parts=parts
            def poll(self, timeout): return next(self.values)
            def close(self): self.closed=True
        c=C()
        ck=types.SimpleNamespace(TopicPartition=TP,KafkaError=types.SimpleNamespace(_PARTITION_EOF=-191))
        with patch.object(client,'kafka_modules',return_value=(ck,None)), \
             patch.object(client,'consumer',return_value=c), \
             patch.object(client,'topic_watermarks',return_value=rows):
            out=list(client.scan_records('fake','lab.t',limit,5))
        self.assertTrue(c.closed)
        return out
    def test_reads_snapshot(self):
        out=self.run_scan([FakeMessage(offset=i) for i in range(3)], [{'partition':0,'low':0,'high':3}])
        self.assertEqual([m.offset() for m in out],[0,1,2])
    def test_read_limit(self):
        out=self.run_scan([FakeMessage(offset=i) for i in range(3)], [{'partition':0,'low':0,'high':3}],1)
        self.assertEqual(len(out),1)
    def test_empty_snapshot(self):
        self.assertEqual(self.run_scan([], [{'partition':0,'low':3,'high':3}]),[])
    def test_offset_holes_not_message_count(self):
        out=self.run_scan([FakeMessage(offset=0),FakeMessage(offset=4)], [{'partition':0,'low':0,'high':5}])
        self.assertEqual(len(out),2)
    def test_eof_after_compaction_holes(self):
        eof=FakeMessage(offset=5,error=FakeError('EOF',-191))
        out=self.run_scan([FakeMessage(offset=0),eof], [{'partition':0,'low':0,'high':5}])
        self.assertEqual(len(out),1)


class ZooKeeperTests(unittest.TestCase):
    def role(self, data):
        class Sock:
            def __enter__(self): return self
            def __exit__(self,*_a): pass
            def settimeout(self,*_a): pass
            def sendall(self,data): assert data==b'srvr'
            def recv(self,_n): return chunks.pop(0)
        chunks=[data,b'']
        with patch('socket.create_connection', return_value=Sock()):
            return client.zk_role('zk1:2181')
    def test_leader(self): self.assertTrue(self.role(b'ZooKeeper version: test\nMode: leader\n')['serving'])
    def test_follower(self): self.assertEqual(self.role(b'Mode: follower\n')['role'],'follower')
    def test_ruok_not_proof_of_quorum(self): self.assertFalse(self.role(b'imok')['serving'])
    def test_not_serving(self): self.assertFalse(self.role(b'This ZooKeeper instance is not currently serving requests')['serving'])
    def test_unreachable(self):
        with patch('socket.create_connection',side_effect=OSError('refused')):
            self.assertEqual(client.zk_role('zk1:2181')['role'],'unreachable')


if __name__=='__main__': unittest.main()
