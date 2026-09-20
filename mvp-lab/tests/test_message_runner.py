"""Real workflow; TEST-DOUBLE SQL/Kafka/Redis. Optional real loopback HTTP to fake ES.

None of these tests is a running database, Kafka broker, or durability test.
"""
import copy
from dataclasses import replace
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import types
import unittest
from unittest.mock import patch
from mvp_app.adapters import Search, Settings
from mvp_app.core import Problem
from mvp_app.message_safety import canonical, Scope
from mvp_app.message_runtime import MessageRuntime
from mvp_app.message_drill import Runner
from fakes import MemoryRepo, MemoryCache, MemorySearch
from test_message_safety import http_error


class FencedMemorySearch(MemorySearch):
    def index(self, order):
        self.check()
        if 'unexpected_study_field' in order:
            raise http_error(400,'strict_dynamic_mapping_exception')
        old=self.docs.get(order['id'])
        if old and old['version'] == order['version']:
            if canonical(old)!=canonical(order):raise Problem(409,'event_version_payload_conflict')
            return 'duplicate_ignored'
        return super().index(order)


class Message:
    def __init__(self,topic,offset,key,value): self.t,self.o,self.k,self.v=topic,offset,key,value
    def topic(self):return self.t
    def offset(self):return self.o
    def partition(self):return 0
    def key(self):return self.k
    def value(self):return self.v


class Reader:
    def __init__(self,rt,topic,offset):self.rt,self.topic,self.offset,self.closed=rt,topic,offset,False
    def take(self):
        if self.offset>=len(self.rt.logs[self.topic]):raise RuntimeError('fake log exhausted')
        key,value=self.rt.logs[self.topic][self.offset]
        message=Message(self.topic,self.offset,key,value);self.offset+=1
        return message
    def commit(self,message,asynchronous):
        if asynchronous:raise AssertionError('requires sync')
        self.rt.checkpoints[self.topic]=message.offset()+1
        return [types.SimpleNamespace(topic=message.topic(), partition=message.partition(),
                                      offset=message.offset()+1, error=None)]
    def close(self):self.closed=True


class MemoryTransport(MessageRuntime):
    evidence_kind = "TEST-DOUBLE-SQL-KAFKA-REDIS-AND-TEST-ES"
    def __init__(self,scope,search=None):
        self.scope=scope;self.search=search or FencedMemorySearch();self.cache=MemoryCache()
        self.logs={scope.topic:[],scope.dlq:[]};self.checkpoints={}
    def verify_resources(self,resources,allow_missing=False):return list(self.logs),True
    def publish(self,topic,key,value):
        if topic not in self.logs:raise ValueError('foreign topic')
        self.logs[topic].append((key,value))
        return {'topic':topic,'partition':0,'offset':len(self.logs[topic])-1}
    def reader(self,topic=None,offset=None):
        topic=topic or self.scope.topic
        if offset is None:offset=self.checkpoints.get(topic,0)
        return Reader(self,topic,offset)
    def watermark(self,topic=None):
        topic=topic or self.scope.topic
        return {'low':0,'high':len(self.logs[topic]),'committed':self.checkpoints.get(topic)}
    def next_message(self,reader,seconds=10):return reader.take()
    def read_dlq(self):return [json.loads(v) for _,v in self.logs[self.scope.dlq]]
    def exact(self,oid):
        if hasattr(self.search,'docs'):return copy.deepcopy(self.search.docs.get(oid))
        return super().exact(oid)


class RunnerTests(unittest.TestCase):
    def execute(self,scenario,search=None):
        scope=Scope('db-lab-mvp','msg-'+'d'*24,'e'*32)
        rt=MemoryTransport(scope,search)
        repo=MemoryRepo();events=[]
        resources={'topics':{scope.topic:'source-uuid',scope.dlq:'dlq-uuid'},'index':{'name':scope.index,'index_uuid':'index-uuid'}}
        runner=Runner(rt,repo,scope,resources,log=lambda kind,**data:events.append({'kind':kind,**data}))
        # The real two-final-observation condition is preserved; avoid test-only wait.
        with patch('mvp_app.message_drill.time.sleep'):
            result=runner.run(scenario)
        self.assertEqual(result['status'],'passed')
        self.assertEqual(len(repo.orders),3)
        self.assertEqual(len(result['audits']),2)
        self.assertTrue(all(a['matched'] for a in result['audits']))
        return result,rt,events
    def test_schema_poison_block_quarantine_repair(self):
        result,rt,events=self.execute('poison-schema')
        self.assertEqual(len(result['observation']['records']),1)
        self.assertTrue(any(e.get('stage')=='head_of_line_block_observed' for e in events))
    def test_mapping_rejection_is_distinct(self):
        result,_,_=self.execute('mapping-reject')
        self.assertEqual(result['observation']['records'][0]['reason'],'strict_dynamic_mapping_exception')
    def test_es_commit_gap_redelivers_same_offset(self):
        result,_,_=self.execute('projection-commit-gap')
        self.assertEqual(result['observation']['redelivery_offsets'][:2],[0,0])
    def test_dlq_commit_gap_deduplicates_repair(self):
        result,_,_=self.execute('dlq-commit-gap')
        self.assertEqual(len(result['observation']['records']),2)
        self.assertEqual(len(result['observation']['repair_receipts']),1)
    def test_reverse_duplicate_versions_match_source(self):
        result,_,_=self.execute('replay-ordering')
        self.assertEqual(result['observation']['outcomes'].count('older_version_ignored'),3)
    def test_same_version_corrupt_payload_not_overwritten(self):
        result,_,_=self.execute('version-collision')
        self.assertEqual(result['observation']['records'][0]['reason'],'event_version_payload_conflict')
    def test_zero_fixture_create_success_is_not_accepted(self):
        scope=Scope('db-lab-mvp','msg-'+'d'*24,'e'*32);rt=MemoryTransport(scope);repo=MemoryRepo()
        repo.create({'item':'existing','quantity':1,'unit_price':1},scope.run_id+'-a')
        runner=Runner(rt,repo,scope,{'topics':{scope.topic:'uuid'}},log=lambda *_a,**_k:None)
        with self.assertRaises(Exception):runner.run('poison-schema')
    def test_fault_not_observed_does_not_pass(self):
        # A deliberately wrong ES implementation swallowing mapping errors must fail.
        with self.assertRaises(RuntimeError):self.execute('mapping-reject',search=MemorySearch())


class FakeESHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.0'
    def log_message(self,*args):pass
    def respond(self,status,data=None):
        raw=json.dumps(data or {}).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)
    def do_HEAD(self):self.respond(200)
    def do_GET(self):
        oid=self.path.split('/_doc/')[-1]
        doc=self.server.docs.get(oid)
        self.respond(200,{'_source':doc,'_version':doc['version']}) if doc else self.respond(404)
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length',0)))
        self.respond(200,{'timed_out':False,'_shards':{'failed':0},'hits':{'total':{'value':len(self.server.docs),'relation':'eq'},
                                 'hits':[{'_source':o} for o in self.server.docs.values()]}})
    def do_PUT(self):
        from urllib.parse import urlsplit,parse_qs
        url=urlsplit(self.path);params=parse_qs(url.query)
        doc=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
        if 'unexpected_study_field' in doc:
            self.respond(400,{'error':{'type':'strict_dynamic_mapping_exception'}});return
        oid=doc['id'];old=self.server.docs.get(oid);kind=params.get('version_type',[''])[0]
        conflict=old and (old['version']>doc['version'] or (kind=='external' and old['version']==doc['version']))
        if conflict:self.respond(409,{'error':{'type':'version_conflict_engine_exception'}});return
        self.server.docs[oid]=doc;self.respond(201,{'result':'created'})


class RealLoopbackSearchTests(RunnerTests):
    # Override unittest discovery of inherited tests: this class deliberately runs
    # all six scenarios through the actual requests adapter and fake HTTP ES server.
    def setUp(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',0),FakeESHandler);self.server.docs={}
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def execute(self,scenario,search=None):
        if search is not None:return super().execute(scenario,search)
        s=Search(replace(Settings(),es_url='http://127.0.0.1:'+str(self.server.server_port)))
        try:return super().execute(scenario,s)
        finally:s.close()
