"""Actual HTTP/file tests + explicit DB doubles; no live engines."""
from email.message import Message
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from mvp_app.api import Application, make_handler
from mvp_app.core import Problem, create_input
from mvp_app.adapters import Search, Settings
from mvp_app.http_policy import BoundedHTTPServer, body_fields, decode_json, json_body_length, local_host
from tools import manage
from fakes import MemoryRepo, MemoryCache, MemorySearch, MemoryBroker


def headers(**values):
    result=Message()
    for k,v in values.items():result[k.replace('_','-')]=v
    return result


class JSONBoundaryTests(unittest.TestCase):
    def test_valid_korean(self):
        self.assertEqual(decode_json('{"item":"한글","nested":[1,true,null]}'.encode())['item'],'한글')
    def test_duplicate_top_key(self):
        with self.assertRaises(Problem):decode_json(b'{"quantity":1,"quantity":2}')
    def test_duplicate_nested_key(self):
        with self.assertRaises(Problem):decode_json(b'{"a":{"b":1,"b":2}}')
    def test_nonfinite_literals(self):
        for v in (b'NaN',b'Infinity',b'-Infinity'):
            with self.subTest(v=v),self.assertRaises(Problem):decode_json(v)
    def test_float_overflow(self):
        with self.assertRaises(Problem):decode_json(b'{"x":1e999}')
    def test_surrogate_value(self):
        with self.assertRaises(Problem):decode_json(b'{"item":"\\ud800"}')
    def test_surrogate_key(self):
        with self.assertRaises(Problem):decode_json(b'{"\\ud800":1}')
    def test_invalid_utf8(self):
        with self.assertRaises(Problem):decode_json(b'{"item":"\xff"}')
    def test_deep_input(self):
        with self.assertRaises(Problem):decode_json(b'['*20+b'1'+b']'*20)
    def test_business_validation_independent(self):
        with self.assertRaises(Problem):create_input({'item':'\ud800','quantity':1,'unit_price':1})
    def test_unknown_body_fields(self):
        with self.assertRaises(Problem):body_fields({'quantity':1,'quanity':2},('quantity',))
    def test_object_required(self):
        for v in ([],None,1):
            with self.subTest(v=v),self.assertRaises(Problem):body_fields(v,('item',))


class HeaderBoundaryTests(unittest.TestCase):
    def test_local_hosts(self):
        for host in ('127.0.0.1:18090','localhost','[::1]:8080'):self.assertEqual(local_host(headers(Host=host)),host)
    def test_missing_host(self):
        with self.assertRaises(Problem):local_host(Message())
    def test_duplicate_host(self):
        h=headers(Host='localhost');h['Host']='127.0.0.1'
        with self.assertRaises(Problem):local_host(h)
    def test_malformed_host(self):
        for host in ('localhost:abc','localhost:0','localhost:65536','user@localhost','localhost/',' localhost','evil.example'):
            with self.subTest(host=host),self.assertRaises(Problem):local_host(headers(Host=host))
    def test_duplicate_idempotency(self):
        h=headers(Host='localhost',Idempotency_Key='a');h['Idempotency-Key']='b'
        with self.assertRaises(Problem):local_host(h)
    def test_duplicate_length(self):
        h=headers(Content_Type='application/json',Content_Length='20');h['Content-Length']='1'
        with self.assertRaises(Problem):json_body_length(h)
    def test_transfer_encoding(self):
        with self.assertRaises(Problem):json_body_length(headers(Content_Type='application/json',Content_Length='20',Transfer_Encoding='chunked'))
    def test_invalid_lengths(self):
        for v in (None,'-1','+1','1,1','12x','１２'):
            h=headers(Content_Type='application/json')
            if v is not None:h['Content-Length']=v
            with self.subTest(v=v),self.assertRaises(Problem):json_body_length(h)
    def test_body_budget(self):
        for n in (0,16385):
            with self.subTest(n=n),self.assertRaises(Problem):json_body_length(headers(Content_Type='application/json',Content_Length=str(n)))
        self.assertEqual(json_body_length(headers(Content_Type='application/json; charset=utf-8',Content_Length='16384')),16384)


class LiveHTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.repo=MemoryRepo();self.app=Application(self.repo,MemoryCache(),MemorySearch(),MemoryBroker())
        self.server=BoundedHTTPServer(('127.0.0.1',0),make_handler(lambda:self.app))
        self.thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.01),daemon=True)
        self.thread.start();self.port=self.server.server_address[1];self.addCleanup(self.stop)
    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
    def request(self,raw,extra=(),path='/api/orders',method='POST'):
        c=HTTPConnection('127.0.0.1',self.port,timeout=3)
        try:
            c.putrequest(method,path);c.putheader('Content-Type','application/json');c.putheader('Content-Length',str(len(raw)))
            for k,v in extra:c.putheader(k,v)
            c.endheaders(raw);r=c.getresponse();return r.status,json.loads(r.read())
        finally:c.close()
    def test_duplicate_body_no_write(self):
        code,_=self.request(b'{"item":"x","quantity":1,"quantity":2,"unit_price":10}')
        self.assertEqual(code,400);self.assertEqual(len(self.repo.orders),0)
    def test_duplicate_length_no_write(self):
        code,_=self.request(b'{"item":"x","quantity":1,"unit_price":10}',[('Content-Length','1')])
        self.assertEqual(code,400);self.assertEqual(len(self.repo.orders),0)
    def test_unknown_post_field_no_write(self):
        code,_=self.request(b'{"item":"x","quantity":1,"unit_price":10,"quanity":4}')
        self.assertEqual(code,400);self.assertEqual(len(self.repo.orders),0)
    def test_query_limit(self):
        code,_=self.request(b'',path='/api/orders?'+'&'.join('x=1' for _ in range(21)),method='GET')
        self.assertEqual(code,400)
    def test_valid_and_idempotent(self):
        raw='{"item":"한글","quantity":1,"unit_price":10}'.encode()
        a=self.request(raw,[('Idempotency-Key','quality-one')]);b=self.request(raw,[('Idempotency-Key','quality-one')])
        self.assertEqual((a[0],b[0]),(201,200));self.assertEqual(a[1]['order'],b[1]['order'])
    def test_unknown_patch_does_not_change_version(self):
        _,created=self.request(b'{"item":"x","quantity":1,"unit_price":10}');oid=created['order']['id']
        code,_=self.request(b'{"status":"paid","expected_version":1,"force":true}',path='/api/orders/'+oid,method='PATCH')
        self.assertEqual(code,400);self.assertEqual(self.repo.orders[oid]['version'],1)


class CapacityTests(unittest.TestCase):
    def test_overload_rejection_and_slot_reuse(self):
        entered=threading.Event();release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                if self.path=='/hold':entered.set();release.wait(3)
                self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'OK')
        server=BoundedHTTPServer(('127.0.0.1',0),Handler,max_connections=1)
        thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
        first=HTTPConnection(*server.server_address,timeout=4);second=HTTPConnection(*server.server_address,timeout=4)
        try:
            first.request('GET','/hold');self.assertTrue(entered.wait(1))
            second.request('GET','/');resp=second.getresponse();self.assertEqual(resp.status,503)
            self.assertEqual(resp.getheader('Retry-After'),'1');self.assertIn(b'api_capacity_exceeded',resp.read())
            release.set();r=first.getresponse();self.assertEqual(r.status,200);r.read()
            for _ in range(100):
                if server._slots.acquire(blocking=False):server._slots.release();break
                threading.Event().wait(.01)
            else:self.fail('connection slot not released')
            third=HTTPConnection(*server.server_address,timeout=3)
            try:
                third.request('GET','/');r=third.getresponse();self.assertEqual(r.status,200);r.read()
            finally:third.close()
        finally:
            release.set();first.close();second.close();server.shutdown();server.server_close();thread.join(2)
    def test_invalid_capacity(self):
        for v in (0,65,True,'1'):
            with self.subTest(v=v),self.assertRaises(ValueError):BoundedHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler,max_connections=v)
    def test_thread_start_failure_releases_slot(self):
        server=BoundedHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler,max_connections=1)
        try:
            with patch('threading.Thread.start',side_effect=RuntimeError('test-only start failure')):
                with self.assertRaises(RuntimeError):server.process_request(Mock(),('127.0.0.1',1))
            self.assertTrue(server._slots.acquire(blocking=False));server._slots.release()
        finally:server.server_close()


class SearchCompletenessTests(unittest.TestCase):
    def search(self,data,status=200):
        response=Mock(status_code=status);response.json.return_value=data
        session=Mock();session.request.return_value=response
        return Search(Settings(),session=session),session
    def test_complete_empty_result(self):
        search,session=self.search({'timed_out':False,'_shards':{'failed':0},'hits':{'total':{'value':0},'hits':[]}})
        self.assertEqual(search.find('x')['orders'],[]);self.assertIs(session.request.call_args.kwargs['allow_redirects'],False)
    def test_timed_out(self):
        search,_=self.search({'timed_out':True,'_shards':{'failed':0},'hits':{'hits':[]}})
        with self.assertRaises(Problem):search.find('x')
    def test_failed_shard(self):
        search,_=self.search({'timed_out':False,'_shards':{'failed':1},'hits':{'hits':[]}})
        with self.assertRaises(Problem):search.find('x')
    def test_missing_metadata(self):
        search,_=self.search({'hits':{'hits':[]}})
        with self.assertRaises(Problem):search.find('x')
    def test_bool_is_not_shard_count(self):
        search,_=self.search({'timed_out':False,'_shards':{'failed':False},'hits':{'hits':[]}})
        with self.assertRaises(Problem):search.find('x')
    def test_redirect_refused(self):
        for status in (301,302,307,308):
            search,session=self.search({},status)
            with self.subTest(status=status),self.assertRaises(Problem):search.find('x')
            self.assertEqual(session.request.call_count,1)


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'project';self.root.mkdir();self.outside=Path(self.tmp.name)/'outside';self.outside.mkdir()
        p=patch.object(manage,'ROOT',self.root);p.start();self.addCleanup(p.stop)
    def test_state_symlink_no_external_write(self):
        (self.root/'.state').symlink_to(self.outside,target_is_directory=True)
        with self.assertRaises(ValueError):
            with manage.lock():pass
        self.assertEqual(list(self.outside.iterdir()),[])
    def test_dangling_reports(self):
        (self.root/'reports').symlink_to(self.outside/'missing',target_is_directory=True)
        with self.assertRaises(ValueError):manage.check_storage()
    def test_nested_report_path(self):
        (self.root/'reports').mkdir();(self.root/'reports/studies').symlink_to(self.outside,target_is_directory=True)
        with self.assertRaises(ValueError):manage.check_storage()
    def test_env_link_not_read(self):
        (self.outside/'secret').write_text('SQL_PASSWORD=private\n');(self.root/'.env').symlink_to(self.outside/'secret')
        with self.assertRaises(ValueError):manage.parse_env(self.root/'.env')
    def test_failed_atomic_write_preserves_existing(self):
        path=self.root/'state.json';path.write_text('{"old":true}')
        with self.assertRaises(ValueError):manage.atomic_json(path,{'v':float('nan')})
        self.assertEqual(path.read_text(),'{"old":true}');self.assertEqual([p.name for p in self.root.iterdir()],['state.json'])
    def test_atomic_permissions(self):
        path=self.root/'ok.json';manage.atomic_json(path,{'v':1})
        self.assertEqual(path.stat().st_mode & 0o777,0o600);self.assertEqual(json.loads(path.read_text()),{'v':1})
    def test_atomic_link_destination(self):
        (self.outside/'existing').write_text('original');path=self.root/'link.json';path.symlink_to(self.outside/'existing')
        with self.assertRaises(ValueError):manage.atomic_json(path,{'v':1})
        self.assertEqual((self.outside/'existing').read_text(),'original')
    def test_provider_detection_timeout(self):
        with patch('shutil.which',return_value='/bin/docker'),patch('subprocess.run',return_value=Mock(returncode=0)) as run:
            manage.provider({'MVP_ENGINE':'docker'})
        self.assertEqual(run.call_args.kwargs['timeout'],10)
