"""Two real HTTP runs, in-memory DB/engine doubles. NOT real Redis/MariaDB/Kafka/ES timings."""
from http.server import ThreadingHTTPServer
import json,os,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from mvp_app.api import Application,make_handler
from mvp_app.core import relay_once,project_one
from tools.simulation import Plan,HTTP,Journal,Runner
from tools.study_analysis import load_run,compare,render
from fakes import MemoryRepo,MemoryCache,MemorySearch,MemoryBroker
from study_fixtures import context

class PairedLoopbackTests(unittest.TestCase):
    def test_real_loopback_reports_load_and_compare_without_pretending_live_databases(self):
        repo,cache,search,broker=MemoryRepo(),MemoryCache(),MemorySearch(),MemoryBroker();lock=threading.RLock()
        class App(Application):
            def route(self,*args,**kwargs):
                with lock:
                    try:
                        while relay_once(repo,broker):project_one(broker.events[-1],search,cache,lambda:None)
                    except OSError:pass
                    return super().route(*args,**kwargs)
        app=App(repo,cache,search,broker)
        class FakeLab:
            def preflight(self):return context()[1]
            def apply(self,action,service,*args,**kwargs):
                with lock:cache.down=True
                return {'action':action,'service':service,'evidence':'EXPLICIT MEMORY DOUBLE, NOT DOCKER'}
            def restore(self):
                with lock:cache.down=False
                return {'restored':True,'evidence':'EXPLICIT MEMORY DOUBLE, NOT DOCKER'}
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'STUDY_PROJECT':'db-lab-mvp'}):
            root=Path(d)
            srv=ThreadingHTTPServer(('127.0.0.1',0),make_handler(lambda:app));srv.daemon_threads=True
            thread=threading.Thread(target=lambda:srv.serve_forever(poll_interval=.01),daemon=True);thread.start()
            try:
                runs=[]
                for scenario,rid in [('baseline','sim-'+'1'*12),('redis-outage','sim-'+'2'*12)]:
                    journal=Journal(root/'reports/simulations'/rid)
                    plan=Plan(scenario=scenario,seconds=15,rate=1,workers=4,fault_at=2,fault_for=5,recovery_timeout=8,workload='read-heavy')
                    journal.json('plan.json',plan.document())
                    result=Runner(plan,HTTP('http://127.0.0.1:'+str(srv.server_port),rid),FakeLab(),journal,rid,'db-lab-mvp',
                                  evidence_kind='TEST-DOUBLE-DBS-WITH-REAL-LOOPBACK-HTTP',context_provider=lambda states:context()[0]['before']).run()
                    self.assertEqual(result['status'],'passed',json.dumps(result,ensure_ascii=False))
                    runs.append(load_run(root,rid))
                result=compare(*runs);self.assertEqual(result['comparison_status'],'test_only');self.assertTrue(result['conditions_match'])
                render(root/'comparison',result)
                self.assertEqual(result['fault']['error_or_unknown_percent'],0)
                self.assertTrue(result['fault']['fault_restored']);self.assertTrue(result['fault']['final_consistency'])
                out=os.environ.get('STUDY_TEST_EVIDENCE')
                if out:
                    import shutil
                    shutil.copytree(root,Path(out)/'real-http-memory-backends',dirs_exist_ok=True)
            finally:srv.shutdown();srv.server_close();thread.join(2)
