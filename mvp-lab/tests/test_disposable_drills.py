"""Controller safety tests. Commands are simulated; no resource exhaustion or real Docker."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import types
from unittest import TestCase
from unittest.mock import Mock, patch
from tools.advanced import Disposable, candidate, DRILLS
from tools import manage
from mvp_app import deadlock_drill
from mvp_app.adapters import Settings
from mvp_app.redis_pressure import pressure

PIN = {'fingerprint': 'a'*64, 'engine':'docker', 'local_docker': True}
RUN = 'drill-0123456789ab'
SIM = 'sim-0123456789ab'
IID = 'sha256:'+'b'*64
CID = 'c'*64

class DisposableTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root/'.state').mkdir()
        self.m = Mock(ROOT=self.root); self.m.atomic_json = manage.atomic_json
        self.lab = Mock(); self.lab.active_path = self.root/'.state/simulation-active.json'
        self.lab.binding.return_value = PIN; self.lab.c.config = {'MVP_PROJECT': 'db-lab-mvp'}
        self.store = Disposable(self.lab, self.m)
        self.store.call = Mock(); self.store.begin(RUN, 'backup-restore')

    def resource(self):
        spec = {'role': 'database', 'name': 'db-lab-mvp-'+RUN+'-database', 'image_id':IID,
                'network':'none','tmpfs':['/var/lib/mysql'],'memory_bytes':768*1024**2, 'id':CID}
        self.store.record['resources'] = [spec]; self.store.save()
        row = {'Id':CID, 'Image':IID, 'State': {'Running':True},
               'Config':{'Labels': {'io.db-lab.project':'db-lab-mvp','io.db-lab.run':RUN,
                                   'io.db-lab.kind':'disposable','io.db-lab.role':'database'}},
               'HostConfig':{'NetworkMode':'none', 'Memory':768*1024**2,'MemorySwap':768*1024**2,
                             'Privileged':False, 'Tmpfs':{'/var/lib/mysql':'size=512m'}},
               'Mounts':[{'Type':'tmpfs','Destination':'/var/lib/mysql'}]}
        return spec,row

    def response(self, stdout='', rc=0, stderr=''):
        return types.SimpleNamespace(stdout=stdout,stderr=stderr,returncode=rc)

    def test_begin_persists_target_before_any_create(self):
        self.assertEqual(json.loads(self.store.path.read_text())['engine'], PIN)
        self.store.call.assert_not_called()

    def test_second_run_cannot_overwrite_active_record(self):
        before = self.store.path.read_bytes()
        with self.assertRaises(RuntimeError): self.store.begin('drill-111111111111','redis-oom')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_wrong_engine_cannot_clean_up(self):
        self.lab.binding.return_value = {**PIN,'fingerprint':'other'}
        with self.assertRaises(RuntimeError): self.store.cleanup()
        self.store.call.assert_not_called(); self.assertTrue(self.store.path.exists())

    def test_unknown_kind_cannot_clean_up(self):
        self.store.record['kind'] = 'arbitrary'; self.store.save()
        with self.assertRaises(RuntimeError): self.store.cleanup()
        self.store.call.assert_not_called()

    def test_no_volume_deletion_and_only_owned_container_removed(self):
        spec,row = self.resource()
        def call(*args, **kwargs):
            if args[0]=='ps': return self.response(CID)
            if args[0]=='inspect': return self.response(json.dumps([row]))
            if args[0]=='stop': row['State']['Running']=False
            return self.response()
        self.store.call.side_effect = call
        result = self.store.cleanup()
        self.assertTrue(result['cleaned']); self.assertFalse(self.store.path.exists())
        args = [c.args for c in self.store.call.call_args_list]
        self.assertIn(('rm',CID), args)
        self.assertFalse(any('prune' in a or 'volume' in a or '-v' in a or '-f' in a for a in args))

    def test_source_volume_mount_rejects_cleanup(self):
        spec,row = self.resource()
        row['Mounts'] = [{'Type':'volume','Destination':'/var/lib/mysql','Name':'source-mariadb-data'}]
        self.store.call.side_effect = lambda *a,**kw:self.response(CID if a[0]=='ps' else json.dumps([row]))
        with self.assertRaises(RuntimeError): self.store.cleanup()
        self.assertTrue(self.store.path.exists())
        self.assertFalse(any(c.args[0] in {'rm','stop'} for c in self.store.call.call_args_list))

    def test_disappeared_container_is_not_recreated_for_cleanup(self):
        self.resource(); self.store.call.return_value = self.response('')
        self.assertTrue(self.store.cleanup()['cleaned'])
        self.assertTrue(all(c.args[0]=='ps' for c in self.store.call.call_args_list))

    def test_changed_container_id_is_not_removed(self):
        spec,row = self.resource(); row['Id']='d'*64
        self.store.call.side_effect=lambda *a,**kw:self.response(CID if a[0]=='ps' else json.dumps([row]))
        with self.assertRaises(RuntimeError): self.store.cleanup()
        self.assertTrue(self.store.path.exists())

    def test_missing_tmpfs_is_not_trusted(self):
        spec,row = self.resource(); row['HostConfig']['Tmpfs']={}
        self.store.call.side_effect=lambda *a,**kw:self.response(CID if a[0]=='ps' else json.dumps([row]))
        with self.assertRaises(RuntimeError): self.store.inspect_owned(spec)

    def test_memory_limit_change_refuses_cleanup(self):
        spec,row = self.resource(); row['HostConfig']['Memory']=0
        self.store.call.side_effect=lambda *a,**kw:self.response(CID if a[0]=='ps' else json.dumps([row]))
        with self.assertRaises(RuntimeError): self.store.inspect_owned(spec)

    def test_no_swap_limits_means_no_oom_experiment(self):
        self.store.call.return_value=self.response(json.dumps({'OSType':'linux','MemoryLimit':True,'SwapLimit':False,'MemTotal':8*1024**3}))
        with self.assertRaises(RuntimeError): self.store.resource_limits()

    def test_small_total_memory_refused(self):
        self.store.call.return_value=self.response(json.dumps({'OSType':'linux','MemoryLimit':True,'SwapLimit':True,'MemTotal':2*1024**3}))
        with self.assertRaises(RuntimeError): self.store.resource_limits()

    def test_create_records_intention_before_disconnected_create(self):
        self.store.image_id=Mock(return_value=IID)
        def call(*a,**kw):
            self.assertTrue(self.store.path.exists())
            self.assertEqual(json.loads(self.store.path.read_text())['resources'][0]['name'],'db-lab-mvp-'+RUN+'-database')
            raise RuntimeError('disconnect after create')
        self.store.call.side_effect=call
        with self.assertRaises(RuntimeError): self.store.create('database',IID,tmpfs={'/data':16},memory=128,environment={'REDIS_PASSWORD':'x'*24})
        self.assertFalse((self.root/'.state'/ (RUN+'.env')).exists())
        self.assertTrue(self.store.path.exists())

    def test_client_cannot_join_source_network(self):
        with self.assertRaises(ValueError): self.store.create('client',IID,network='container:'+'a'*64)
        self.store.call.assert_not_called()

    def test_only_one_database_is_allowed(self):
        self.resource()
        with self.assertRaises(ValueError): self.store.create('database',IID)
        self.store.call.assert_not_called()

    def test_no_active_recovery_is_noop(self):
        self.store.path.unlink()
        self.assertEqual(self.store.cleanup()['resources'],0)
        self.store.call.assert_not_called()

    def test_snapshot_candidate_must_change_image(self):
        from test_advanced_snapshot import bundle
        api = {'service':'api','id':'a'*64,'image_id':IID}
        db = {'service':'mariadb','id':'d'*64,'image_id':IID}
        self.lab.docker.return_value=json.dumps(bundle())
        self.store.image_id=Mock(return_value=IID)
        with self.assertRaisesRegex(ValueError,'equals current'):
            self.store.snapshot([api,db],self.root,'mariadb:11.8')
        self.store.call.assert_not_called()

class InputTests(TestCase):
    def test_only_explicit_official_image_allowed(self):
        for name in ['mariadb:11.8','docker.io/library/mariadb:11.8.3','library/mariadb:10.11.10']:
            self.assertEqual(candidate(name),name)

    def test_arbitrary_image_and_latest_refused(self):
        for name in ['mariadb:latest','evil/mariadb:11.8','mysql:8.4','mariadb','mariadb:11.8;echo bad',None]:
            with self.assertRaises(ValueError): candidate(name)

    def test_each_mutation_requires_confirmation(self):
        parser=manage.parser()
        import contextlib,io
        for args in [('drills','run','redis-oom'),('drills','prepare'),('drills','recover')]:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):parser.parse_args(args)

    def test_network_and_recovery_commands_available(self):
        p=manage.parser()
        a=p.parse_args(['simulate','plan','db-network-loss'])
        self.assertEqual(a.scenario,'db-network-loss')
        a=p.parse_args(['drills','run','backup-restore','--snapshot','old.json','--yes'])
        self.assertEqual(a.snapshot,'old.json')

    def test_pressure_cannot_target_original_cache(self):
        with patch.dict(os.environ,{'MVP_DISPOSABLE':RUN}), self.assertRaises(ValueError):
            pressure(Settings(redis_host='redis'),'oom')

    def test_pressure_requires_disposable_marker(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError):
            pressure(Settings(redis_host='127.0.0.1'),'disk-full')

class DeadlockTests(TestCase):
    def test_unrelated_rows_never_locked(self):
        conn=Mock(); cur=Mock(); cur.__enter__=Mock(return_value=cur); cur.__exit__=Mock(return_value=False)
        cur.fetchone.return_value={'item':'not-owned'};conn.cursor.return_value=cur
        repo=Mock();repo.connect.side_effect=[conn,conn]
        ids=['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002']
        result=deadlock_drill.deadlock(repo,ids,SIM)
        self.assertFalse(result['observed_deadlock'])
        self.assertFalse(any('FOR UPDATE' in c.args[0] for c in cur.execute.call_args_list))
        self.assertEqual(conn.rollback.call_count,2);self.assertEqual(conn.close.call_count,2)

    def test_duplicate_target_refused_before_connection(self):
        repo=Mock();oid='00000000-0000-0000-0000-000000000001'
        with self.assertRaises(ValueError):deadlock_drill.deadlock(repo,[oid,oid],SIM)
        repo.connect.assert_not_called()

    def test_one_1213_and_one_survivor_both_rollback(self):
        # Explicit scheduling double: this proves controller classification, NOT InnoDB deadlock detection.
        lock_attempts=threading.Barrier(2,timeout=2)
        connections=[]
        def connect():
            index=len(connections)
            conn=Mock();cur=Mock();cur.__enter__=Mock(return_value=cur);cur.__exit__=Mock(return_value=False)
            cur.fetchone.return_value={'item':SIM+'-fixture'}
            conn.cursor.return_value=cur;connections.append(conn)
            locked=0
            def execute(sql,*args):
                nonlocal locked
                if 'FOR UPDATE' in sql:
                    locked+=1
                    if locked==2:
                        lock_attempts.wait()
                        if index==0:raise RuntimeError(1213,'test victim')
            cur.execute.side_effect=execute
            return conn
        repo=Mock();repo.connect.side_effect=connect
        result=deadlock_drill.deadlock(repo,['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002'],SIM)
        self.assertTrue(result['observed_deadlock'])
        for conn in connections:
            conn.rollback.assert_called_once();conn.close.assert_called_once();conn.commit.assert_not_called()

class PressureEvidenceTests(TestCase):
    def run_pressure(self, kind, state, client=None, logs=''):
        lab=Mock(); m=Mock(ROOT=Path('/no-real-path'))
        store=Disposable(lab,m); store.record={'run_id':RUN,'resources':[{'role':'database'}]}
        store.create=Mock(return_value=CID); store.wait_db=Mock()
        store.call=Mock(return_value=types.SimpleNamespace(stdout=logs,stderr='',returncode=0))
        store.inspect_owned=Mock(return_value={'State':state,'HostConfig':{'Memory':64*1024**2}})
        store.client=Mock(return_value=client or {'client_error':'ConnectionError'})
        with patch('tools.advanced.time.sleep'):
            result=store.pressure([{'service':'redis','image_id':IID},{'service':'api','image_id':IID}],kind)
        return result,store

    def test_connection_error_alone_is_not_oom(self):
        result,_=self.run_pressure('redis-oom',{'OOMKilled':False,'ExitCode':1,'Running':False})
        self.assertFalse(result['observed'])

    def test_exit_137_without_oom_flag_is_not_oom(self):
        result,_=self.run_pressure('redis-oom',{'OOMKilled':False,'ExitCode':137,'Running':False})
        self.assertFalse(result['observed'])

    def test_positive_oom_requires_exit_and_kernel_flag(self):
        result,store=self.run_pressure('redis-oom',{'OOMKilled':True,'ExitCode':137,'Running':False})
        self.assertTrue(result['observed'])
        self.assertEqual(store.create.call_args.kwargs['memory'],64)
        self.assertEqual(store.create.call_args.kwargs['tmpfs'],{'/data':4})

    def test_aof_error_without_enospc_is_not_disk_full(self):
        result,_=self.run_pressure('redis-disk-full',{'OOMKilled':False,'ExitCode':0,'Running':True},
                                  {'aof_last_write_status':'err'},'Permission denied')
        self.assertFalse(result['observed'])

    def test_enospc_plus_aof_failure_is_observed(self):
        result,store=self.run_pressure('redis-disk-full',{'OOMKilled':False,'ExitCode':0,'Running':True},
                                      {'aof_last_write_status':'err','baseline_readable':True},'No space left on device')
        self.assertTrue(result['observed']);self.assertTrue(result['baseline_readable'])
        self.assertEqual(store.create.call_args.kwargs['tmpfs'],{'/data':16})
        self.assertEqual(store.create.call_args.kwargs['memory'],128)

    def test_disk_full_trial_that_ooms_is_not_success(self):
        result,_=self.run_pressure('redis-disk-full',{'OOMKilled':True,'ExitCode':137,'Running':False},
                                  {'aof_last_write_status':'err'},'No space left on device')
        self.assertFalse(result['observed'])
