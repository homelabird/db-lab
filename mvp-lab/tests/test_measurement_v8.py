"""Real loopback HTTP timing; all DB/engine state is explicitly simulated."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile,threading,time,types,unittest
from unittest.mock import Mock,patch
from tools.measurement import capture_context,comparable_context
from tools.simulation import HTTP,Journal,Plan,Runner,Response
from fakes import MemoryRepo
from study_fixtures import context

@contextmanager
def server(body=b'{"ok":true}',delay=0):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(delay);self.send_response(200);self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    srv=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=lambda:srv.serve_forever(poll_interval=.01),daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(srv.server_port)
    finally:srv.shutdown();srv.server_close();thread.join(2)

class TimingTests(unittest.TestCase):
    def test_real_http_components_add_up(self):
        with server(delay=.02) as url:r=HTTP(url,'sim-'+'1'*12,1).call('GET','/')
        self.assertEqual(r.status,200);self.assertTrue(r.transport_attempted);self.assertGreater(r.transport_ms,10)
        self.assertAlmostEqual(r.client_queue_ms+r.transport_ms,r.milliseconds,places=5)
    def test_slot_timeout_has_no_transport_sample(self):
        http=HTTP('http://127.0.0.1:1','sim-'+'1'*12,1);http.slots.acquire()
        try:r=http.call('GET','/',timeout=.025)
        finally:http.slots.release()
        self.assertEqual(r.status,0);self.assertFalse(r.transport_attempted);self.assertIsNone(r.transport_ms)
        self.assertEqual(r.client_queue_ms,r.milliseconds)
    def test_contended_slot_records_client_wait(self):
        with server() as url:
            http=HTTP(url,'sim-'+'1'*12,1);http.slots.acquire()
            t=threading.Thread(target=lambda:(time.sleep(.025),http.slots.release()));t.start()
            r=http.call('GET','/');t.join()
        self.assertGreater(r.client_queue_ms,10);self.assertEqual(r.status,200)
    def test_duplicate_json_body_not_success(self):
        with server(b'{"passed":false,"passed":true}') as url:r=HTTP(url,'sim-'+'1'*12,1).call('GET','/')
        self.assertEqual(r.status,0);self.assertEqual(r.payload['error'],'ValueError')
    def test_nonfinite_json_body_refused(self):
        with server(b'{"lag":NaN}') as url:self.assertEqual(HTTP(url,'sim-'+'1'*12,1).call('GET','/').status,0)
    def test_response_defaults_do_not_invent_zero_queue_metrics(self):
        r=Response(200,{},1);self.assertIsNone(r.client_queue_ms);self.assertIsNone(r.transport_ms)
    def test_runner_logs_start_finish_workflow_and_phase_change(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'run');http=Mock();r=Runner(Plan(),http,Mock(),j,'sim-'+'1'*12,'db-lab-mvp',evidence_kind='TEST-ONLY')
            r.phase='baseline';r.workflow_context.number=12
            def call(*_):r.phase='fault';return Response(200,{},0,client_queue_ms=0,transport_ms=0,transport_attempted=True)
            http.call.side_effect=call;r.call('GET','/');row=j.rows['requests'][0];j.close()
            self.assertEqual(row['workflow_number'],12);self.assertEqual(row['phase'],'baseline');self.assertEqual(row['phase_finished'],'fault')
            self.assertLessEqual(row['request_started_seconds'],row['request_finished_seconds'])
    def test_nested_race_requests_keep_parent_workflow_number(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'run');repo=MemoryRepo();run_id='sim-'+'1'*12;lock=threading.Lock()
            class FakeHTTP:
                def call(self,method,path,body,key,timeout):
                    with lock:
                        if method=='POST':
                            order,_=repo.create(body,key);return Response(201,{'order':order},0)
                        order=repo.orders[path.split('/')[-1]]
                        if order['version']!=body['expected_version']:return Response(409,{'error':'version_conflict'},0)
                        return Response(200,{'order':repo.update(order['id'],body)},0)
            r=Runner(Plan(),FakeHTTP(),Mock(),j,run_id,'db-lab-mvp',evidence_kind='TEST-ONLY')
            r.register(run_id+'-fixture-0',{'item':'fixture','quantity':1,'unit_price':1});r.workflow({'target':0,'kind':'race','number':7})
            self.assertEqual({x['workflow_number'] for x in j.rows['requests']},{7});self.assertEqual(r.races,[[200,409]])
            self.assertEqual(len([x for x in j.rows['timeline'] if x.get('stage')=='workflow_finished']),1)
            self.assertIsNone(getattr(r.workflow_context,'number'));j.close()
    def test_failed_workflow_has_finished_failure_record(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'run');r=Runner(Plan(),Mock(),Mock(),j,'sim-'+'1'*12,'db-lab-mvp')
            with patch.object(r,'_workflow',side_effect=RuntimeError()),self.assertRaises(RuntimeError):r.workflow({'number':1,'kind':'read'})
            self.assertEqual(j.rows['timeline'][-1]['outcome'],'failed');j.close()

class EmptyWorkloadTests(unittest.TestCase):
    def test_fixture_success_alone_is_not_a_workload_pass(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(Path(d)/'run');r=Runner(Plan(),Mock(),Mock(),j,'sim-'+'1'*12,'db-lab-mvp',evidence_kind='TEST-ONLY')
            r.lab.preflight.return_value=[]
            r.call=Mock(return_value=Response(200,{'application':'db-lab-mvp','study_api':2,'project':'db-lab-mvp'},0))
            r.create=Mock(return_value=Response(201,{'order':{'id':'fixture'}},0))
            r.converge=Mock(return_value={'consistent':True,'rows':[]})
            r.sample=Mock(return_value={'dependencies_reachable':True})
            r.observe=lambda:None;r.fault=lambda:None
            def no_work(_):r.skipped=len(r.plan.operations());return 0
            r.schedule=no_work
            result=r.run();j.close()
            self.assertEqual(result['status'],'failed')
            self.assertIn('no_workload_observed',[x['code'] for x in result['contract_errors']])

class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        for name in ('Containerfile','.dockerignore','requirements.txt','compose.yaml','mvp_app/core.py','tools/simulation.py'):
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('# source\n')
        self.states=context()[1];self.c=types.SimpleNamespace(config={'SQL_PASSWORD':'do-not-copy-this','CACHE_TTL':'30'},guard_engine=lambda:{'local_docker':True,'fingerprint':'test-engine'})
    def test_no_password_in_recorded_context(self):self.assertNotIn('do-not-copy-this',json.dumps(capture_context(self.root,self.c,self.states)))
    def test_same_inputs_same_fingerprint(self):self.assertEqual(capture_context(self.root,self.c,self.states),capture_context(self.root,self.c,self.states))
    def test_ttl_change_changes_context(self):
        a=capture_context(self.root,self.c,self.states);self.c.config['CACHE_TTL']='60'
        self.assertNotEqual(a['config_sha256'],capture_context(self.root,self.c,self.states)['config_sha256'])
    def test_source_change_changes_context(self):
        a=capture_context(self.root,self.c,self.states);(self.root/'tools/simulation.py').write_text('changed')
        self.assertNotEqual(a['source_sha256'],capture_context(self.root,self.c,self.states)['source_sha256'])
    def test_only_container_id_change_preserves_recreate_comparison(self):
        a=capture_context(self.root,self.c,self.states);self.states[0]['id']='new-container'
        self.assertEqual(comparable_context(a),comparable_context(capture_context(self.root,self.c,self.states)))
    def test_new_volume_changes_context(self):
        a=capture_context(self.root,self.c,self.states);self.states[0]['volumes']={'/data':'new-volume'}
        self.assertNotEqual(a['topology_sha256'],capture_context(self.root,self.c,self.states)['topology_sha256'])
    def test_incomplete_topology_refused(self):
        with self.assertRaisesRegex(RuntimeError,'incomplete'):capture_context(self.root,self.c,self.states[:-1])
    def test_remote_engine_refused(self):
        self.c.guard_engine=lambda:{'local_docker':False}
        with self.assertRaisesRegex(RuntimeError,'local_docker'):capture_context(self.root,self.c,self.states)
    def test_symlink_source_refused(self):
        p=self.root/'tools/simulation.py';p.unlink();p.symlink_to(self.root/'Containerfile')
        with self.assertRaisesRegex(RuntimeError,'bounded'):capture_context(self.root,self.c,self.states)
