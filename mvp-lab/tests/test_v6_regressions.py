"""Explicit HTTP/Docker/Kafka doubles for defects reproduced in v5."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import sys
import threading
import types
import unittest
import uuid
from unittest.mock import Mock, patch
from tools import probe as p
from mvp_app.adapters import Broker, Settings
import test_simulation_engine as fixtures

ID='00000000-0000-4000-8000-000000000001'
ORDER={'id':ID,'item':'smoke-00000000','quantity':1,'unit_price':100,'total':100,'status':'created','version':1,'created_at':'2026-09-19T00:00:00Z'}

class SmokeRegressions(unittest.TestCase):
    def invoke(self,created=None,replayed=None,search=None):
        responses=[{'order':created or ORDER,'created':True},{'order':replayed or ORDER,'created':False},
                   {'orders':[search or ORDER]},{'order':ORDER}]
        opener=Mock();opener.open.side_effect=lambda *a,**k:io.BytesIO(json.dumps(responses.pop(0)).encode())
        with patch.object(p,'build_opener',return_value=opener),patch.object(p.uuid,'uuid4',return_value=uuid.UUID(ID)):
            return p.probe('http://127.0.0.1:18090',1)

    def test_consistent_but_wrong_submitted_data_fails(self):
        wrong={**ORDER,'quantity':5,'total':500}
        with self.assertRaisesRegex(RuntimeError,'submitted'):self.invoke(created=wrong,replayed=wrong,search=wrong)

    def test_same_id_with_changed_replay_content_fails(self):
        with self.assertRaisesRegex(RuntimeError,'Idempotency'):self.invoke(replayed={**ORDER,'item':'another'})

    def test_correct_full_path_still_passes(self):
        value=self.invoke();self.assertTrue(value['submitted_payload_checked']);self.assertTrue(value['full_retry_payload_checked'])

    def test_search_content_mismatch_fails(self):
        with self.assertRaisesRegex(RuntimeError,'Search content'):self.invoke(search={**ORDER,'item':'wrong'})

    def test_boolean_numeric_field_rejected(self):
        with self.assertRaises(RuntimeError):p.check_order({**ORDER,'quantity':True})

    def test_extra_or_missing_order_field_rejected(self):
        with self.assertRaises(RuntimeError):p.check_order({**ORDER,'extra':1})
        value=dict(ORDER);value.pop('total')
        with self.assertRaises(RuntimeError):p.check_order(value)

    def test_noncanonical_identifier_rejected(self):
        with self.assertRaises(RuntimeError):p.check_order({**ORDER,'id':'not-a-uuid'})

    def test_nonlocal_or_credential_url_refused_before_http(self):
        for base in ('https://127.0.0.1:18090','http://user:pw@localhost:18090','http://localhost:18090/x','http://localhost:18090?q=1','http://example.invalid'):
            with self.subTest(base=base),patch.object(p,'build_opener') as opener,self.assertRaises(ValueError):p.probe(base)
            opener.assert_not_called()

    def test_large_http_body_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError,'1MiB'):p.read_object(io.BytesIO(b' '*1048577))

    def test_redirect_is_not_followed_even_to_another_loopback_port(self):
        hits=[]
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):hits.append(self.path);self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            def log_message(self,*args):pass
        target=ThreadingHTTPServer(('127.0.0.1',0),Destination)
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{target.server_port}/unexpected');self.end_headers()
            def log_message(self,*args):pass
        origin=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        threads=[]
        try:
            for s in (target,origin):
                t=threading.Thread(target=lambda server=s:server.serve_forever(poll_interval=.01),daemon=True);t.start();threads.append(t)
            with self.assertRaises(Exception):p.probe(f'http://127.0.0.1:{origin.server_port}',1)
            self.assertEqual(hits,[])
        finally:
            for s in (origin,target):s.shutdown();s.server_close()
            for t in threads:t.join(2)

class TargetRegressions(unittest.TestCase):
    def fixture(self):
        case=fixtures.FaultSafetyTests();case.setUp();self.addCleanup(case.doCleanups);return case

    def test_loopback_plus_public_binding_refused(self):
        case=self.fixture();case.state['api']['published_ports']['8080/tcp'].append({'HostIp':'0.0.0.0','HostPort':'18090'})
        with self.assertRaises(RuntimeError):case.lab.preflight()
        case.lab.docker.assert_not_called()

    def test_extra_api_published_port_refused(self):
        case=self.fixture();case.state['api']['published_ports']['9000/tcp']=[{'HostIp':'127.0.0.1','HostPort':'19000'}]
        with self.assertRaises(RuntimeError):case.lab.preflight()

    def test_loopback_only_accepted(self):
        self.assertEqual(len(self.fixture().lab.preflight()),6)

class KafkaMetadataRegressions(unittest.TestCase):
    def fixture(self, error=None, low=0, high=0, offset=0):
        broker=Broker(Settings());client=Mock()
        client.get_watermark_offsets.return_value=(low,high)
        client.committed.return_value=[types.SimpleNamespace(error=error,offset=offset)]
        broker.consumer=Mock(return_value=client)
        admin=Mock();admin.list_topics.return_value=types.SimpleNamespace(topics={'mvp.orders.v1':types.SimpleNamespace(error=None,partitions={0:None})})
        fake={'confluent_kafka':types.SimpleNamespace(TopicPartition=lambda *a:object()),
              'confluent_kafka.admin':types.SimpleNamespace(AdminClient=lambda *a:admin)}
        return broker,client,fake

    def test_committed_lookup_error_is_not_lag_zero(self):
        broker,client,fake=self.fixture(error=object(),offset=-1001)
        with patch.dict(sys.modules,fake),self.assertRaisesRegex(RuntimeError,'lookup failed'):broker.status()
        client.close.assert_called_once()

    def test_invalid_watermark_range_fails(self):
        broker,client,fake=self.fixture(low=10,high=1)
        with patch.dict(sys.modules,fake),self.assertRaisesRegex(RuntimeError,'invalid watermark'):broker.status()

    def test_valid_lag_still_reported(self):
        broker,client,fake=self.fixture(high=10,offset=4)
        with patch.dict(sys.modules,fake):self.assertEqual(broker.status()['lag'],6)
