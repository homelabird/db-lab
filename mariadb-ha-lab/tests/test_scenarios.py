"""Incident command tests with explicit fake DB/container responses; NOT integration tests."""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'scripts'))
import lab
import scenarios as sc

# Load pure worker logic without installing a DB driver on the host.
stub = types.ModuleType('pymysql')
stub.MySQLError = type('FakeMySQLError', (Exception,), {})
with patch.dict(sys.modules, {'pymysql':stub}):
    spec = importlib.util.spec_from_file_location('worker_for_tests', ROOT/'scripts/scenario_worker.py')
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)


def healthy():
    return {n:dict(node=n,ready=True,container='running',mode='galera',wsrep_cluster_size='3',
        wsrep_cluster_status='Primary',wsrep_cluster_state_uuid='synthetic-test-uuid') for n in sc.NODES}


class CliTests(unittest.TestCase):
    def parse(self,*args): return lab.parser().parse_args(['scenario',*args])
    def test_default_rows(self): self.assertEqual(self.parse('slow').rows,50000)
    def test_default_payload(self): self.assertEqual(self.parse('fragmentation').payload_bytes,1024)
    def test_leave_broken(self): self.assertTrue(self.parse('slow','--leave-broken').leave_broken)
    def test_fault_default_node(self): self.assertEqual(self.parse('node-failure').node,'galera1')
    def test_fault_default_hold(self): self.assertEqual(self.parse('node-hang').hold,20)
    def test_quorum_not_approved_by_default(self): self.assertFalse(self.parse('quorum').confirm_quorum)
    def test_quorum_approved(self): self.assertTrue(self.parse('quorum','--confirm-quorum').confirm_quorum)
    def test_cleanup_not_approved(self): self.assertFalse(self.parse('cleanup').confirm_cleanup)
    def test_fix_target(self): self.assertEqual(self.parse('fix','fragmentation').target,'fragmentation')
    def test_bad_node_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): self.parse('node-failure','--node','other')
    def test_bad_rows_rejected(self):
        for value in ['-1','0','999','500001','abc','1;DROP DATABASE']:
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): self.parse('slow','--rows',value)
    def test_lock_hold_limit(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): self.parse('lock','--hold','999')
    def test_help_formats_percent(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught: self.parse('--help')
        self.assertEqual(caught.exception.code,0)
    def test_every_help(self):
        for name in list(sc.ACTIONS)+['fix','cleanup','inspect','repair']:
            with self.subTest(name=name), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught: self.parse(name,'--help')
            self.assertEqual(caught.exception.code,0)
    def test_list_needs_no_db(self):
        with contextlib.redirect_stdout(io.StringIO()): self.assertTrue(sc.no_runtime(self.parse('list')))
    def test_report_without_database(self):
        with tempfile.TemporaryDirectory() as d, patch.object(sc,'ROOT',Path(d)), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(sc.no_runtime(self.parse('report')))


class InputAndHealthTests(unittest.TestCase):
    def test_healthy(self): sc.check_health(healthy())
    def test_missing_node(self):
        s=healthy();s.pop('galera3')
        with self.assertRaises(sc.ScenarioError): sc.check_health(s)
    def test_unready(self):
        s=healthy();s['galera3']['ready']=False
        with self.assertRaises(sc.ScenarioError): sc.check_health(s)
    def test_wrong_size(self):
        s=healthy();s['galera2']['wsrep_cluster_size']='2'
        with self.assertRaises(sc.ScenarioError): sc.check_health(s)
    def test_mixed_uuid(self):
        s=healthy();s['galera2']['wsrep_cluster_state_uuid']='different'
        with self.assertRaises(sc.ScenarioError): sc.check_health(s)
    def test_integer_size(self):
        s=healthy()
        for v in s.values(): v['wsrep_cluster_size']=3
        sc.check_health(s)
    def test_wrong_name(self):
        s=healthy();s['galera1']['node']='production'
        with self.assertRaises(sc.ScenarioError): sc.check_health(s)
    def test_worker_bounds(self):
        self.assertEqual(worker.bounds(1000,1000,500000,'rows'),1000)
        for x in [True,'1000',999,500001]:
            with self.subTest(x=x), self.assertRaises(ValueError): worker.bounds(x,1000,500000,'rows')
    def test_worker_request(self): worker.validate_request({'owner':'mariadb-ha','token':'a'*24})
    def test_worker_token_rejects_path(self):
        with self.assertRaises(ValueError): worker.validate_request({'owner':'mariadb-ha','token':'../../var'})
    def test_worker_owner_rejects_sql(self):
        with self.assertRaises(ValueError): worker.validate_request({'owner':"x';DROP",'token':'a'*24})
    def test_batches_cover_all_rows(self):
        b=list(worker.batches(1001));self.assertEqual(b,[(1,500),(501,1000),(1001,1001)])
    def test_private_json(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.json';sc.private_json(p,{'a':1})
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(p.read_text()),{'a':1})
            self.assertFalse(p.with_suffix('.json.tmp').exists())


class WorkerLogicTests(unittest.TestCase):
    def test_schema_wrong_owner_refused(self):
        with patch.object(worker,'scalar',return_value=1), patch.object(worker,'query',return_value=[{'signature':worker.SIGNATURE,'owner':'other'}]), self.assertRaises(ValueError):
            worker.ensure_schema(object(),'mariadb-ha')
    def test_schema_unmarked_refused(self):
        with patch.object(worker,'scalar',return_value=1), patch.object(worker,'query',return_value=[]), self.assertRaises(ValueError):
            worker.ensure_schema(object(),'mariadb-ha',create=False)
    def test_wsrep_off_refused(self):
        with patch.object(worker,'scalar',return_value=0), self.assertRaises(ValueError): worker.ensure_schema(object(),'mariadb-ha')
    def test_restore_logging_allowlist(self):
        with self.assertRaises(ValueError): worker.restore_logging(object(),{'slow_query_log':1,'log_output':"FILE';DROP"})
    def test_restore_logging_rejects_extra_keys(self):
        with self.assertRaises(ValueError): worker.restore_logging(object(),{'slow_query_log':1,'log_output':'FILE','max_connections':3})
    def test_restore_logging_order(self):
        saved={'slow_query_log':0,'log_output':'FILE'}
        with patch.object(worker,'query') as query,patch.object(worker,'log_state',return_value=saved):
            self.assertEqual(worker.restore_logging(object(),saved),saved)
            self.assertEqual([c.args[1] for c in query.call_args_list],['SET GLOBAL log_output=%s','SET GLOBAL slow_query_log=%s'])
    def test_slow_restores_on_error(self):
        saved={'slow_query_log':0,'log_output':'FILE'}
        with patch.object(worker,'log_state',return_value=saved),patch.object(worker,'query',side_effect=RuntimeError('query failed')),patch.object(worker,'restore_logging') as restore:
            with self.assertRaises(RuntimeError): worker.slow_work(object(),{'token':'a'*24},create=False)
            restore.assert_called_once()
    def test_fragmentation_requires_file_per_table(self):
        with patch.object(worker,'scalar',return_value=0), self.assertRaises(ValueError): worker.frag_work(object(),{},create=False)
    def test_fragmentation_kept_without_rebuild(self):
        snapshot={'exact_rows':200,'payload_bytes':2000,'file':{'logical_bytes':10000}}
        with patch.object(worker,'scalar',return_value=1),patch.object(worker,'frag_snapshot',return_value=snapshot),patch.object(worker,'query') as query:
            result=worker.frag_work(object(),{'leave_broken':True},create=False)
        self.assertTrue(result['left_fragmented']);query.assert_not_called()
    def test_fragmentation_rebuild_preserves_counts(self):
        a={'exact_rows':200,'payload_bytes':2000,'file':{'logical_bytes':10000}}
        b={'exact_rows':200,'payload_bytes':2000,'file':{'logical_bytes':3000}}
        with patch.object(worker,'scalar',return_value=1),patch.object(worker,'frag_snapshot',side_effect=[a,b]),patch.object(worker,'query'):
            result=worker.frag_work(object(),{},create=False)
        self.assertEqual(result['file_bytes_reclaimed'],7000);self.assertTrue(result['data_unchanged_by_rebuild'])
    def test_fragmentation_data_change_fails(self):
        a={'exact_rows':200,'payload_bytes':2000,'file':{'logical_bytes':10000}}
        b={'exact_rows':199,'payload_bytes':2000,'file':{'logical_bytes':3000}}
        with patch.object(worker,'scalar',return_value=1),patch.object(worker,'frag_snapshot',side_effect=[a,b]),patch.object(worker,'query'),self.assertRaises(RuntimeError):
            worker.frag_work(object(),{},create=False)
    def test_no_obsolete_defragment_setting(self):
        source=(ROOT/'scripts/scenario_worker.py').read_text()
        self.assertNotIn('SET GLOBAL innodb_defragment',source)
        self.assertNotIn('TRUNCATE',source.replace('not TRUNCATE',''))
    def test_worker_cleanup_requires_confirm(self):
        fake=MagicMock(); fake.__enter__.return_value=fake
        with patch.object(worker,'connection',return_value=fake),patch.object(worker,'ensure_schema'),self.assertRaises(ValueError):
            worker.execute({'action':'cleanup','owner':'mariadb-ha'})


class FakeLab:
    """Explicit model, not a real MariaDB server or container runtime."""
    def __init__(self):
        self.project='mariadb-ha';self.engine='podman';self.settings={}
        self.states={n:'running' for n in sc.NODES};self.calls=[];self.receipts=[];self.fail_action=None
    def name(self,n): return self.project+'-'+n
    def state(self,n): return self.states[n]
    def health(self,n):
        h=healthy()[n]; size=sum(x=='running' for x in self.states.values())
        h['wsrep_cluster_size']=str(size);h['ready']=self.states[n]=='running' and size>=2
        h['container']=self.states[n];h['wsrep_cluster_status']='Primary' if size>=2 else 'non-Primary'
        return h
    def healthy_node(self):
        for n in sc.NODES:
            if self.health(n)['ready']: return n
        raise sc.ScenarioError('no live primary')
    def wait(self,n,size):
        self.calls.append(('wait',n,size))
        if not self.health(n)['ready']: raise sc.ScenarioError('not ready')
    def run(self,args,**kwargs):
        self.calls.append(tuple(args[:5]))
        if 'python3' in args:
            req=json.loads(kwargs['input']); action=req['action']
            if action==self.fail_action: raise subprocess.TimeoutExpired(args,1)
            result={}
            if action=='probe':
                available=[n for n in sc.NODES if self.health(n)['ready']]
                result={'acknowledged':bool(available),'request_id':req['request_id']}
                if available:
                    result['backend']=available[0]
                    self.receipts.append({'request_id':req['request_id'],'handled_by':available[0]})
                else: result['error_code']=2013
            elif action=='reconcile': result=self.receipts[:]
            elif action=='logging-state': result={'slow_query_log':1,'log_output':'FILE'}
            return subprocess.CompletedProcess(args,0,json.dumps({'ok':True,'result':result}),'')
        n=next((n for n in sc.NODES if self.name(n)==args[-1]),None)
        if args[1]=='kill': self.states[n]='stopped'
        if args[1]=='pause': self.states[n]='paused'
        if args[1]=='unpause': self.states[n]='running'
        return subprocess.CompletedProcess(args,0,'','')
    def comp(self,*args,**kwargs):
        self.calls.append(('compose',)+args);self.states[args[-1]]='running'
        return subprocess.CompletedProcess(args,0,'','')
    def status(self): pass


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        (self.path/'scripts').mkdir();(self.path/'scripts/scenario_worker.py').write_text('# explicit fake worker')
        (self.path/'.state').mkdir()
        self.root_patch=patch.object(sc,'ROOT',self.path);self.root_patch.start()
        self.lab=FakeLab();self.runner=sc.Runner(self.lab)
        self.stdout=contextlib.redirect_stdout(io.StringIO());self.stdout.__enter__()
    def tearDown(self):
        self.stdout.__exit__(None,None,None);self.root_patch.stop();self.temp.cleanup()
    def begin(self): self.runner.begin('test')
    def test_pending_blocks_new_run(self):
        self.begin()
        with self.assertRaises(sc.ScenarioError): self.runner.begin('again')
    def test_restore_unpauses_all_before_wait(self):
        self.begin();self.runner.journal['nodes']=['galera2','galera3'];self.runner.save_journal()
        self.lab.states.update(galera2='paused',galera3='paused')
        self.runner.recover_pending()
        commands=[c[1] for c in self.lab.calls if c[0]=='podman']
        self.assertEqual(commands,['unpause','unpause'])
        self.assertFalse(self.runner.journal_path.exists())
    def test_crash_restarts_without_bootstrap(self):
        self.begin();self.runner.journal['nodes']=['galera1'];self.runner.save_journal();self.lab.states['galera1']='stopped'
        self.runner.recover_pending()
        self.assertEqual(self.lab.states['galera1'],'running')
        self.assertNotIn('bootstrap',str(self.lab.calls))
    def test_no_surviving_primary_refuses_restart(self):
        self.begin();self.runner.journal['nodes']=['galera1'];self.runner.save_journal()
        self.lab.states={n:'stopped' for n in sc.NODES}
        with self.assertRaises(sc.ScenarioError): self.runner.recover_pending()
        self.assertTrue(self.runner.journal_path.exists())
        self.assertFalse(any(c[0]=='compose' for c in self.lab.calls))
    def test_recovery_identity_mismatch(self):
        self.begin();self.runner.journal['project']='production';self.runner.save_journal()
        with self.assertRaises(sc.ScenarioError): self.runner.recover_pending()
    def test_restore_globals_from_journal(self):
        self.begin();self.runner.remember_logging('galera1');self.runner.recover_pending()
        self.assertFalse(self.runner.journal_path.exists())
    def test_cancel_active_worker(self):
        self.begin();self.runner.journal['active_worker']={'node':'galera1','token':self.runner.journal['token'],'action':'slow'};self.runner.save_journal()
        self.runner.recover_pending();self.assertFalse(self.runner.journal_path.exists())
    def test_worker_timeout_preserves_lease(self):
        self.begin();self.lab.fail_action='slow'
        with self.assertRaises(subprocess.TimeoutExpired): self.runner.worker('galera1','slow')
        self.assertEqual(json.loads(self.runner.journal_path.read_text())['active_worker']['action'],'slow')
    def test_quorum_approval_precedes_any_mutation(self):
        with self.assertRaises(sc.ScenarioError): self.runner.execute(argparse.Namespace(scenario='quorum',confirm_quorum=False))
        self.assertFalse(self.runner.journal_path.exists());self.assertFalse(self.lab.calls)
    def test_cleanup_approval_precedes_any_mutation(self):
        with self.assertRaises(sc.ScenarioError): self.runner.execute(argparse.Namespace(scenario='cleanup',confirm_cleanup=False))
        self.assertFalse(self.runner.journal_path.exists())
    def test_unhealthy_prevents_begin(self):
        self.lab.states['galera3']='stopped'
        with self.assertRaises(sc.ScenarioError): self.runner.execute(argparse.Namespace(scenario='slow'))
        self.assertFalse(self.runner.journal_path.exists())
    def test_payload_limit_before_begin(self):
        with self.assertRaises(sc.ScenarioError): self.runner.execute(argparse.Namespace(scenario='fragmentation',rows=500000,payload_bytes=4096))
        self.assertFalse(self.runner.journal_path.exists())
    def test_crash_roundtrip_model(self):
        args=lab.parser().parse_args(['scenario','node-failure','--hold','5'])
        clock=[0.0]
        def monotonic(): clock[0]+=0.2;return clock[0]
        with patch.object(sc.time,'monotonic',side_effect=monotonic),patch.object(sc.time,'sleep'):
            self.runner.execute(args)
        result=self.runner.report
        self.assertEqual(result['status'],'passed')
        self.assertTrue(result['results']['node-failure']['acknowledged_requests_present'])
        self.assertTrue(result['results']['node-failure']['all_nodes_same_probe_ids'])
        self.assertFalse(self.runner.journal_path.exists())
    def test_quorum_roundtrip_model(self):
        args=lab.parser().parse_args(['scenario','quorum','--hold','30','--confirm-quorum'])
        clock=[0.0]
        def monotonic(): clock[0]+=3;return clock[0]
        with patch.object(sc.time,'monotonic',side_effect=monotonic),patch.object(sc.time,'sleep'):
            self.runner.execute(args)
        self.assertEqual(self.runner.report['status'],'passed')
        self.assertTrue(self.runner.report['results']['quorum']['non_primary_observed'])
    def test_failed_backend_ack_is_not_failover_success(self):
        args=lab.parser().parse_args(['scenario','node-failure','--hold','5'])
        original=self.runner.fault_probe
        def bad_probe(host,token,number,phase):
            result=original(host,token,number,phase)
            if phase=='during' and result['acknowledged']:result['backend']='galera1'
            return result
        clock=[0.0]
        def monotonic():clock[0]+=0.2;return clock[0]
        with patch.object(self.runner,'fault_probe',side_effect=bad_probe),patch.object(sc.time,'monotonic',side_effect=monotonic),patch.object(sc.time,'sleep'):
            with self.assertRaises(sc.ScenarioError):self.runner.execute(args)
        self.assertFalse(self.runner.report['results']['node-failure']['surviving_backend_ack_observed'])
    def test_non_primary_without_writer_rejection_fails(self):
        args=lab.parser().parse_args(['scenario','quorum','--hold','30','--confirm-quorum'])
        original=self.runner.fault_probe
        def bad_probe(host,token,number,phase):
            result=original(host,token,number,phase)
            if phase=='during' and not result['acknowledged']:
                result.update(acknowledged=True,backend='galera1')
                self.lab.receipts.append({'request_id':result['request_id'],'handled_by':'galera1'})
            return result
        clock=[0.0]
        def monotonic():clock[0]+=3;return clock[0]
        with patch.object(self.runner,'fault_probe',side_effect=bad_probe),patch.object(sc.time,'monotonic',side_effect=monotonic),patch.object(sc.time,'sleep'):
            with self.assertRaises(sc.ScenarioError):self.runner.execute(args)
        self.assertFalse(self.runner.report['results']['quorum']['write_rejection_observed'])
    def test_failure_report_and_recovery(self):
        args=lab.parser().parse_args(['scenario','slow']);self.lab.fail_action='slow'
        with self.assertRaises(subprocess.TimeoutExpired): self.runner.execute(args)
        self.assertEqual(self.runner.report['status'],'failed')
        self.assertFalse(self.runner.journal_path.exists())
        self.assertTrue((self.runner.directory/'summary.json').exists())


if __name__=='__main__': unittest.main()
