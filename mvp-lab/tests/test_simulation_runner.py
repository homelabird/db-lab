"""Harness tests: explicit dependency doubles, plus one real loopback HTTP drill (not DB engines)."""
from http.server import ThreadingHTTPServer
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from mvp_app.api import Application, make_handler
from mvp_app.core import relay_once, project_one
from tools.simulation import Plan, Runner, HTTP, Journal, Response
from tools.probe import probe
from fakes import MemoryRepo, MemoryCache, MemorySearch, MemoryBroker
RUN='sim-0123456789ab'


class RunnerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.j=Journal(Path(self.temp.name)/'run');self.addCleanup(self.j.close)
        self.http=Mock();self.lab=Mock()
        self.runner=Runner(Plan(),self.http,self.lab,self.j,RUN,'db-lab-mvp',evidence_kind='TEST-DOUBLE')
        payload={'item':RUN+'-fixture-0','quantity':1,'unit_price':1}
        self.repo=MemoryRepo();order,_=self.repo.create(payload,RUN+'-fixture-0');self.order=order
        self.runner.register(RUN+'-fixture-0',payload)
        self.runner.acknowledge(RUN+'-fixture-0',Response(201,{'order':order},0))

    def test_read_workflow_uses_cached_path_without_first_querying_sql(self):
        self.http.call.return_value=Response(200,{'order':self.order,'source':'redis','cache':'hit'},1)
        self.runner.workflow({'target':0,'kind':'read','number':0})
        self.assertEqual(self.http.call.call_count,1)
        self.assertEqual(self.http.call.call_args.args[1],'/api/orders/'+self.order['id'])

    def test_acknowledging_two_ids_for_same_key_is_contract_failure(self):
        changed={**self.order,'id':'11111111-1111-1111-1111-111111111111'}
        self.runner.acknowledge(RUN+'-fixture-0',Response(200,{'order':changed},0))
        self.assertEqual(self.runner.contract_errors[0]['code'],'same_key_multiple_order_ids')

    def test_missing_order_body_is_not_a_successful_ack(self):
        self.runner.acknowledge(RUN+'-fixture-0',Response(200,{},0))
        self.assertEqual(self.runner.contract_errors[0]['code'],'invalid_order_acknowledgement')

    def test_row_lock_effect_requires_specific_observed_error(self):
        self.runner.plan=Plan(scenario='row-lock')
        self.assertFalse(self.runner.effect_observed())
        self.j.rows['requests'].append({'error':'mariadb_lock_wait_timeout'})
        self.assertTrue(self.runner.effect_observed())

    def test_worker_freeze_requires_write_to_be_acknowledged_but_not_searchable(self):
        self.runner.plan=Plan(scenario='worker-freeze')
        self.runner.projection_gaps.append('synthetic-order')
        self.runner.baseline_diagnostics={'mariadb':{'outbox_pending':0},'kafka':{'lag':0}}
        self.j.rows['timeline'].append({'stage':'diagnostics','phase':'fault','data':{'dependencies':{
            'mariadb':{'reachable':True,'outbox_pending':1},
            'kafka':{'reachable':True,'lag':0}}}})
        self.assertTrue(self.runner.effect_observed())

    def test_outage_without_search_gap_is_not_a_confirmed_customer_symptom(self):
        self.runner.plan=Plan(scenario='kafka-outage')
        self.runner.baseline_diagnostics={'mariadb':{'outbox_pending':0}}
        self.j.rows['timeline'].append({'stage':'diagnostics','phase':'fault','data':{'dependencies':{
            'mariadb':{'reachable':True,'outbox_pending':1},'kafka':{'reachable':False}}}})
        self.assertFalse(self.runner.effect_observed())

    def test_old_generic_error_is_not_a_confirmed_lock_timeout(self):
        self.runner.plan=Plan(scenario='row-lock')
        self.j.rows['requests'].append({'error':'mariadb_unavailable'})
        self.assertFalse(self.runner.effect_observed())

    def test_cancel_before_fault_never_touches_engine(self):
        self.runner.plan=Plan(scenario='kafka-outage')
        self.runner.cancel.set();self.runner.fault()
        self.lab.apply.assert_not_called();self.lab.restore.assert_not_called()

    def test_partial_fault_failure_still_calls_restore(self):
        self.runner.plan=Plan(scenario='kafka-outage')
        self.runner.cancel=Mock();self.runner.cancel.wait.return_value=False
        self.lab.apply.side_effect=RuntimeError('partial failure')
        self.lab.restore.return_value={'restored':True,'action':'stop'}
        self.runner.fault()
        self.lab.restore.assert_called_once();self.assertTrue(self.runner.fault_completed)
        self.assertIsNotNone(self.runner.fault_error)

    def test_restore_failure_is_not_marked_success(self):
        self.runner.plan=Plan(scenario='kafka-outage')
        self.runner.cancel=Mock();self.runner.cancel.wait.return_value=False
        self.lab.apply.return_value={'status':'applied'}
        self.lab.restore.side_effect=RuntimeError('start failed')
        self.runner.fault()
        self.assertFalse(self.runner.fault_completed);self.assertIsNotNone(self.runner.fault_error)

    def test_overload_is_skipped_not_queued(self):
        # Use a slow workflow with four planned slots and one worker. The clock is real.
        self.runner.plan=Plan(seconds=15,rate=10,workers=1)
        self.runner.workflow=Mock(side_effect=lambda _:time.sleep(.08))
        ops=[{'number':i,'at_seconds':i*.001,'kind':'read'} for i in range(4)]
        with patch.object(self.runner.cancel,'wait',return_value=False):
            self.runner.schedule(ops)
        self.assertEqual(self.runner.admitted,1);self.assertEqual(self.runner.skipped,3)

    def test_workload_budget_fails_before_an_unbounded_request(self):
        self.runner.workload_requests=2400
        with self.assertRaises(RuntimeError):self.runner.call('GET','/health/live')
        self.http.call.assert_not_called()

    def test_deadline_without_observation_is_not_success(self):
        result=self.runner.audit(time.monotonic()-1)
        self.assertFalse(result['consistent']);self.http.call.assert_not_called()


class LoopbackScenarioTests(unittest.TestCase):
    def test_kafka_outage_harness_over_loopback_with_explicit_memory_backends(self):
        repo,cache,search,broker=MemoryRepo(),MemoryCache(),MemorySearch(),MemoryBroker()
        lock=threading.RLock();state={'worker_down':False}
        class App(Application):
            def route(self,*args,**kwargs):
                with lock:
                    if not state['worker_down']:
                        try:
                            while relay_once(repo,broker):
                                project_one(broker.events[-1],search,cache,lambda:None)
                        except OSError:pass
                    return super().route(*args,**kwargs)
        app=App(repo,cache,search,broker)
        class FakeLab:
            def preflight(self):return [{'evidence':'TEST DOUBLE; no Docker or real DB'}]
            def apply(self,action,service,*args,**kwargs):
                self.action=(action,service)
                with lock:broker.down=True
                return {'action':action,'service':service,'evidence':'TEST DOUBLE'}
            def restore(self):
                with lock:broker.down=False
                return {'restored':True,'evidence':'TEST DOUBLE'}
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'STUDY_PROJECT':'db-lab-mvp'}):
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(lambda:app));server.daemon_threads=True
            thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
            try:
                journal=Journal(Path(d)/'run')
                plan=Plan(scenario='kafka-outage',seconds=15,rate=1,fault_at=2,fault_for=5,
                          recovery_timeout=8,workload='write-heavy')
                runner=Runner(plan,HTTP('http://127.0.0.1:'+str(server.server_port),RUN),FakeLab(),journal,RUN,
                              'db-lab-mvp',evidence_kind='TEST-DOUBLE-DBS-WITH-REAL-LOOPBACK-HTTP')
                result=runner.run()
                self.assertEqual(result['status'],'passed',json.dumps(result,ensure_ascii=False))
                self.assertTrue(result['fault_applied']);self.assertTrue(result['fault_restored'])
                self.assertTrue(result['fault_effect_observed']);self.assertTrue(result['final_consistency']['consistent'])
                self.assertTrue(any(r.get('stage')=='write_not_yet_searchable' for r in journal.rows['timeline']))
                self.assertEqual(repo.orders,search.docs)
                self.assertGreater(result['workload_http_requests'],result['admitted_workflows'])
                self.assertEqual(result['evidence_kind'],'TEST-DOUBLE-DBS-WITH-REAL-LOOPBACK-HTTP')
                # Optional evidence directory used by the artifact verification run.
                evidence=os.environ.get('SIM_TEST_EVIDENCE')
                if evidence:
                    import shutil
                    shutil.copytree(Path(d)/'run',Path(evidence)/'loopback-test-double-example',dirs_exist_ok=True)
            finally:
                server.shutdown();server.server_close();thread.join(2)

    def test_smoke_rejects_corrupt_full_payload_over_real_http(self):
        repo,cache,broker=MemoryRepo(),MemoryCache(),MemoryBroker()
        class BadSearch(MemorySearch):
            def find(self,q):return {'orders':[{**o,'quantity':999} for o in repo.orders.values() if o['id']==q]}
        app=Application(repo,cache,BadSearch(),broker)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(lambda:app));server.daemon_threads=True
        thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
        try:
            with patch.dict(os.environ, {'STUDY_PROJECT':'db-lab-mvp'}), self.assertRaisesRegex(RuntimeError, 'Search content'):
                probe('http://127.0.0.1:'+str(server.server_port),2)
        finally:
            server.shutdown();server.server_close();thread.join(2)
