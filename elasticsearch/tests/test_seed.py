"""Offline tests: generator, schema, safe writes, and actual HTTP requests to a stub.
The stub is deliberately NOT an Elasticsearch engine and cannot validate Query DSL execution.
"""
import copy
import hashlib
import io
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'scripts'))
import generate_and_load as seed
from lablib import APIError, ESClient, INDICES, LAYOUT, bulk_send, compact, load_catalog
from pit_pagination import paginate


class Stub(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()
    def do_PUT(self): self.handle_request()
    def do_DELETE(self): self.handle_request()
    def handle_request(self):
        state=self.server.state
        path=self.path.split('?')[0]
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
        state['requests'].append((self.command,self.path,raw))
        code=200; value={}
        if path=='/': value={'cluster_name':state['cluster_name'],'cluster_uuid':'unit-test-cluster'}
        elif path=='/_cluster/health': value={'number_of_nodes':5,'status':'green','timed_out':False}
        elif path=='/_bulk':
            state['bulk_calls']+=1
            if state.get('http_once'):
                code=state.pop('http_once'); value={'error':'injected HTTP failure'}
            else:
                assert self.headers['Content-Type']=='application/x-ndjson'
                assert raw.endswith(b'\n')
                lines=raw.splitlines(); assert len(lines)%2==0
                value={'errors':False,'items':[]}
                for n in range(0,len(lines),2):
                    meta=json.loads(lines[n])['index']; document=json.loads(lines[n+1])
                    status=201; error=None
                    if state.get('item_once') and n==0:
                        status=state.pop('item_once'); error={'type':'injected','reason':'test partial failure'}
                        value['errors']=True
                    else:
                        state['docs'][meta['_index']][meta['_id']]=document
                    op={'status':status,'_index':meta['_index'],'_id':meta['_id']}
                    if error:op['error']=error
                    value['items'].append({'index':op})
        elif path.endswith('/_stats/store'):
            value={'_all':{'primaries':{'store':{'size_in_bytes':123}},'total':{'store':{'size_in_bytes':246}}}}
        else:
            index=path.strip('/').split('/')[0]
            if self.command=='GET' and path==f'/{index}':
                if index in state['indices']: value={index:state['indices'][index]}
                else: code=404; value={'error':'index_not_found_exception'}
            elif self.command=='DELETE' and path==f'/{index}':
                state['indices'].pop(index,None); state['docs'].pop(index,None); value={'acknowledged':True}
            elif self.command=='PUT' and path==f'/{index}':
                body=json.loads(raw); settings={k:str(v) for k,v in body['settings'].items()}
                state['indices'][index]={'mappings':body['mappings'],'settings':{'index':settings}}
                state['docs'][index]={}; value={'acknowledged':True}
            elif path.endswith('/_settings'):
                if self.command=='GET': value={index:{'settings':state['indices'][index]['settings']}}
                else:
                    state['indices'][index]['settings']['index'].update(json.loads(raw).get('index',{})); value={'acknowledged':True}
            elif path.endswith('/_refresh'): value={'_shards':{'failed':0}}
            elif path.endswith('/_count'): value={'count':len(state['docs'][index])}
            else: code=500; value={'error':f'unimplemented test route {self.command} {path}'}
        output=compact(value)
        self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(output))); self.end_headers(); self.wfile.write(output)


class GeneratorTests(unittest.TestCase):
    def cfg(self,*extra):
        return seed.config_from_args(seed.parser().parse_args(['--size-mb','0.15',*extra]))
    def test_deterministic_and_seed_changes_data(self):
        c=self.cfg()
        a=list(seed.documents(c,INDICES[0])); b=list(seed.documents(c,INDICES[0]))
        self.assertEqual(a,b)
        self.assertNotEqual(a,list(seed.documents(self.cfg('--seed','43'),INDICES[0])))
    def test_batch_size_does_not_change_data(self):
        self.assertEqual(list(seed.documents(self.cfg('--batch-size','1'),INDICES[0])),list(seed.documents(self.cfg('--batch-size','999'),INDICES[0])))
    def test_size_bounds_ids_and_all_mapped_types(self):
        c=self.cfg()
        for index in INDICES:
            props=seed.definition(c,index)['mappings']['properties']
            records=list(seed.documents(c,index)); total=sum(len(b)+1 for a,b in records)
            self.assertGreaterEqual(total,seed.budget_for(c,index))
            self.assertLess(total,seed.budget_for(c,index)+max(len(b)+1 for a,b in records))
            ids=set()
            for action,source in records:
                metadata=json.loads(action)['index']; doc=json.loads(source)
                self.assertNotIn(metadata['_id'],ids);ids.add(metadata['_id'])
                self.assertEqual(metadata['_id'],doc['document_id'])
                self.assertEqual(metadata['_index'],index)
                def check_fields(values, definitions):
                    for field,value in values.items():
                        self.assertIn(field, definitions)
                        mapping=definitions[field]
                        if mapping.get('type') in ('object','nested') or 'properties' in mapping:
                            children=value if isinstance(value, list) else [value]
                            for child in children:
                                check_fields(child, mapping.get('properties', {}))
                            continue
                        typ=mapping.get('type')
                        if typ=='ip': ipaddress.ip_address(value)
                        elif typ=='geo_point':
                            self.assertIsInstance(value,dict)
                            self.assertIn('lat',value); self.assertIn('lon',value)
                        elif typ=='date': datetime.fromisoformat(value.replace('Z','+00:00'))
                        elif typ=='boolean': self.assertIsInstance(value,bool)
                        elif typ in ('integer','long'): self.assertIs(type(value),int)
                        elif typ=='double':
                            numbers=value if isinstance(value,list) else [value]
                            self.assertTrue(all(isinstance(item,(float,int)) for item in numbers))
                        elif typ in ('keyword','text'): self.assertTrue(value is None or isinstance(value,str) or isinstance(value,list))
                check_fields(doc, props)
    def test_known_incidents_exist(self):
        docs={index:json.loads(next(seed.documents(self.cfg(),index))[1]) for index in INDICES}
        self.assertTrue(docs[INDICES[0]]['is_fraud']);self.assertGreaterEqual(docs[INDICES[0]]['risk_score'],850)
        self.assertEqual(docs[INDICES[1]]['status'],502)
        self.assertEqual(docs[INDICES[2]]['action'],'LOGIN');self.assertEqual(docs[INDICES[2]]['result'],'FAIL')
    def test_no_side_effects_on_import(self):
        proc=subprocess.run([sys.executable,'-c','import generate_and_load'],env={**os.environ,'PYTHONPATH':str(ROOT/'scripts'),'ES_URL':'http://127.0.0.1:1'},capture_output=True,text=True)
        self.assertEqual(proc.returncode,0);self.assertEqual(proc.stdout,'')
    def test_bad_size_and_date_rejected(self):
        for value in ['0','-1','nan','inf']:
            with self.assertRaises(ValueError):self.cfg('--size-mb',value)
        with self.assertRaises(ValueError):self.cfg('--start-date','2026-08-01')
    def test_deletion_requires_yes(self):
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):seed.main(['--recreate'])
    def test_catalog_and_json_load(self):
        catalog=load_catalog();self.assertEqual(len(catalog),26)
        for entry in catalog:
            self.assertTrue(entry['path'].startswith('/lab-'))
            self.assertIn(entry['method'],['POST','GET'])
            if entry.get('file'): json.loads((ROOT/'queries'/entry['file']).read_text())
        for file in (ROOT/'mappings').glob('*.json'):json.loads(file.read_text())
    def test_generate_only_no_cluster_required(self):
        with tempfile.TemporaryDirectory() as temp,redirect_stdout(io.StringIO()):
            with patch.dict(os.environ,{'ES_URL':'http://127.0.0.1:1'}):
                self.assertEqual(seed.main(['--generate-only','--size-mb','0.05','--output-dir',temp]),0)
            manifest=json.loads((Path(temp)/'manifest.json').read_text())
            self.assertEqual(manifest['status'],'generated')
            for index in INDICES:
                raw=(Path(temp)/(index+'.bulk.ndjson')).read_bytes()
                self.assertTrue(raw.endswith(b'\n'))
                self.assertEqual(len(raw.splitlines())//2,manifest['indices'][index]['documents'])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Stub)
        self.server.state={'requests':[],'indices':{},'docs':{},'cluster_name':'cerebro-shard-lab','bulk_calls':0}
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.client=ESClient(self.url,timeout=5)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def run_seed(self,report,*extra):
        with patch.dict(os.environ,{'ES_URL':self.url}),redirect_stdout(io.StringIO()):
            return seed.main(['--size-mb','0.1','--batch-size','7','--report',str(report),*extra])
    def test_full_http_load_and_repeat_no_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            report=Path(temp)/'manifest.json'
            self.run_seed(report)
            before=copy.deepcopy(self.server.state['docs'])
            self.run_seed(report)
            self.assertEqual(before,self.server.state['docs'])
            manifest=json.loads(report.read_text())
            self.assertEqual(manifest['status'],'loaded')
            for index in INDICES:
                self.assertEqual(len(before[index]),manifest['indices'][index]['documents'])
                self.assertEqual(self.server.state['indices'][index]['settings']['index']['refresh_interval'],'1s')
            self.assertFalse(any(method=='DELETE' for method,path,data in self.server.state['requests']))
    def test_mismatched_config_does_not_delete_anything(self):
        with tempfile.TemporaryDirectory() as temp:
            report=Path(temp)/'manifest.json';self.run_seed(report)
            offset=len(self.server.state['requests'])
            with self.assertRaisesRegex(RuntimeError,'older/different'):self.run_seed(report,'--seed','99')
            self.assertTrue(all(method=='GET' for method,path,data in self.server.state['requests'][offset:]))
    def test_recreate_deletes_only_three_exact_indices(self):
        with tempfile.TemporaryDirectory() as temp:
            report=Path(temp)/'manifest.json';self.run_seed(report)
            self.run_seed(report,'--recreate','--yes','--seed','99')
            deleted=[path for method,path,data in self.server.state['requests'] if method=='DELETE']
            self.assertEqual(deleted,['/'+i for i in INDICES])
    def test_wrong_cluster_no_writes(self):
        self.server.state['cluster_name']='production'
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError,'Wrong cluster'):self.run_seed(Path(temp)/'manifest.json')
        self.assertTrue(all(method=='GET' for method,path,data in self.server.state['requests']))
    def records(self):
        c=seed.config_from_args(seed.parser().parse_args(['--size-mb','0.01']))
        seed.prepare_indices(self.client,c)
        return list(seed.documents(c,INDICES[0]))[:3]
    def test_partial_429_retries_only_failed_item(self):
        records=self.records();self.server.state['item_once']=429
        self.assertEqual(bulk_send(self.client,records,backoff=0),len(records))
        bodies=[data for method,path,data in self.server.state['requests'] if path=='/_bulk']
        self.assertEqual(len(bodies),2);self.assertEqual(len(bodies[1].splitlines()),2)
        self.assertEqual(len(self.server.state['docs'][INDICES[0]]),len(records))
    def test_http_503_retries_with_stable_ids(self):
        records=self.records();self.server.state['http_once']=503
        bulk_send(self.client,records,backoff=0)
        self.assertEqual(self.server.state['bulk_calls'],2)
        self.assertEqual(len(self.server.state['docs'][INDICES[0]]),len(records))
    def test_nonretryable_item_error_is_not_success(self):
        records=self.records();self.server.state['item_once']=400
        with self.assertRaisesRegex(RuntimeError,'Non-retryable'):bulk_send(self.client,records,backoff=0)
        self.assertEqual(self.server.state['bulk_calls'],1)
    def test_http_error_body_retained(self):
        records=self.records();self.server.state['http_once']=400
        with self.assertRaisesRegex(APIError,'injected HTTP failure'):bulk_send(self.client,records,backoff=0)
    def test_refresh_restored_on_bulk_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            self.server.state['item_once']=400
            report=Path(temp)/'manifest.json'
            with self.assertRaises(RuntimeError):self.run_seed(report)
            self.assertEqual(self.server.state['indices'][INDICES[0]]['settings']['index']['refresh_interval'],'1s')
            self.assertEqual(json.loads(report.read_text())['status'],'failed')
    def test_existing_extra_docs_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            report=Path(temp)/'manifest.json';self.run_seed(report)
            self.server.state['docs'][INDICES[0]]['external']={'message':'extra'}
            with self.assertRaisesRegex(RuntimeError,'expected .* documents, found'):self.run_seed(report)


class PaginationTests(unittest.TestCase):
    def test_pit_latest_id_cursor_and_close(self):
        class Fake:
            def __init__(self):self.calls=[];self.page=0
            def request(self,method,path,body=None):
                self.calls.append((method,path,body))
                if '/_pit?' in path:return {'id':'pit1'}
                if method=='DELETE':return {'succeeded':True}
                self.page+=1
                return {'pit_id':'pit'+str(self.page+1),'hits':{'hits':[{'_index':INDICES[0],'_id':str(self.page),'sort':[self.page,self.page]}]}}
        f=Fake()
        with redirect_stdout(io.StringIO()):self.assertEqual(paginate(f,INDICES[0],1,2),2)
        self.assertEqual(f.calls[2][2]['pit']['id'],'pit2')
        self.assertEqual(f.calls[2][2]['search_after'],[1,1])
        self.assertEqual(f.calls[-1],('DELETE','/_pit',{'id':'pit3'}))
    def test_pit_closed_on_search_error(self):
        class Fake:
            def __init__(self):self.closed=False
            def request(self,method,path,body=None):
                if '/_pit?' in path:return {'id':'pit1'}
                if method=='DELETE':self.closed=True;return {'succeeded':True}
                raise RuntimeError('injected query failure')
        f=Fake()
        with self.assertRaises(RuntimeError):paginate(f,INDICES[0])
        self.assertTrue(f.closed)


if __name__=='__main__':unittest.main(verbosity=2)
