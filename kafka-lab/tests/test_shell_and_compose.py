from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='kzk-test-')
        self.root=Path(self.tmp.name)
        for f in ['lab.sh','.env.example','compose.yaml','compose.ui.yaml']:
            shutil.copy2(ROOT/f,self.root/f)
        self.bin=self.root/'bin'; self.bin.mkdir()
        shutil.copy2(ROOT/'tests/fake_podman.py',self.bin/'podman')
        (self.bin/'podman').chmod(0o755)
        (self.bin/'podman-compose').write_text('''#!/usr/bin/env -S python3 -S
import sys,json,os
from pathlib import Path
p=Path(os.environ['FAKE_STATE']);s=json.loads(p.read_text());s.setdefault('compose_calls',[]).append(sys.argv[1:]);p.write_text(json.dumps(s))
if '--help' in sys.argv: print('--in-pod --env-file')
elif '--version' in sys.argv: print('podman-compose version 1.3.0 [MOCK]')
''')
        (self.bin/'podman-compose').chmod(0o755)
        self.state=self.root/'fake-state.json'
        self.data={'containers':{f'kzk-lab-{name}':dict(owner='kzk-lab',running=True,paused=False,connected=True)
                   for name in ['zk1','zk2','zk3','kafka1','kafka2','kafka3','tools']}}
        self.save()
        self.env=dict(os.environ,PATH=f'{self.bin}:{os.environ["PATH"]}',FAKE_STATE=str(self.state))
    def tearDown(self): self.tmp.cleanup()
    def save(self): self.state.write_text(json.dumps(self.data))
    def load(self): self.data=json.loads(self.state.read_text()); return self.data
    def run_lab(self,*args,success=True,extra=None):
        env=dict(self.env);env.update(extra or {})
        p=subprocess.run(['bash',str(self.root/'lab.sh'),*args],cwd='/',env=env,text=True,capture_output=True,timeout=15)
        if success: self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        else: self.assertNotEqual(p.returncode,0,p.stdout+p.stderr)
        return p
    def test_help_without_engine(self):
        self.run_lab('help')
        self.assertEqual(self.load().get('calls',[]),[])
    def test_stop_only_requested_lab_broker(self):
        self.run_lab('fault','stop-broker','2')
        self.assertFalse(self.load()['containers']['kzk-lab-kafka2']['running'])
        self.assertTrue(self.data['containers']['kzk-lab-kafka1']['running'])
    def test_kill_signal_explicit(self):
        self.run_lab('fault','kill-broker','1')
        self.assertIn(['kill','--signal','KILL','kzk-lab-kafka1'], self.load()['calls'])
    def test_reject_other_node_id(self):
        self.run_lab('fault','kill-broker','99',success=False)
        self.assertFalse(any(a[0]=='kill' for a in self.load().get('calls',[])))
    def test_wrong_owner_protected(self):
        self.data['containers']['kzk-lab-kafka1']['owner']='production';self.save()
        self.run_lab('fault','kill-broker','1',success=False)
        self.assertFalse(any(a[0]=='kill' for a in self.load().get('calls',[])))
    def test_quorum_fault_stops_two_zk(self):
        self.run_lab('fault','zk-quorum')
        s=self.load()['containers']
        self.assertTrue(s['kzk-lab-zk1']['running'])
        self.assertFalse(s['kzk-lab-zk2']['running']);self.assertFalse(s['kzk-lab-zk3']['running'])
    def test_recover_unpause_start_and_dns_alias(self):
        self.data['containers']['kzk-lab-kafka1'].update(paused=True)
        self.data['containers']['kzk-lab-kafka2'].update(running=False)
        self.data['containers']['kzk-lab-kafka3'].update(connected=False)
        self.data['containers']['kzk-lab-zk2'].update(running=False)
        self.save();self.run_lab('recover');s=self.load()
        for c in s['containers'].values():
            self.assertTrue(c['running']);self.assertFalse(c['paused']);self.assertTrue(c['connected'])
        self.assertIn(['network','connect','--alias','kafka3','kzk-lab-net','kzk-lab-kafka3'],s['calls'])
    def test_recovery_health_failure_not_swallowed(self):
        p=self.run_lab('recover',success=False,extra={'FAKE_FAIL_WAIT':'1'})
        self.assertNotIn('복구 확인:',p.stdout)
    def test_seed_arguments_preserved(self):
        self.run_lab('seed','--kind','access','--count','17')
        self.assertIn(['exec','kzk-lab-tools','python','/opt/lab/client.py','seed','--kind','access','--count','17'],self.load()['calls'])
    def test_read_arguments_preserved(self):
        self.run_lab('read','lab.payments','--max','9')
        self.assertIn(['exec','kzk-lab-tools','python','/opt/lab/client.py','read','--topic','lab.payments','--max','9'],self.load()['calls'])
    def test_cli_uses_surviving_broker(self):
        self.data['containers']['kzk-lab-kafka1']['running']=False;self.save()
        self.run_lab('kcli','kafka-topics','--list')
        self.assertIn(['exec','-i','kzk-lab-kafka2','kafka-topics','--bootstrap-server','kafka1:9092,kafka2:9092,kafka3:9092','--list'],self.load()['calls'])
    def test_reset_requires_explicit_yes(self):
        self.run_lab('reset',success=False)
        self.assertFalse(any('down' in a for a in self.load().get('compose_calls',[])))
    def test_reset_only_compose_project_volumes(self):
        self.run_lab('reset','--yes')
        calls=self.load()['compose_calls'];self.assertEqual(calls[-1][-2:],['down','-v'])
        self.assertIn('--in-pod=false',calls[-1]);self.assertIn('kzk-lab',calls[-1])
    def test_down_preserves_volumes(self):
        self.run_lab('down')
        self.assertEqual(self.load()['compose_calls'][-1][-1],'down')
        self.assertNotIn('-v',self.data['compose_calls'][-1])
    def test_active_lock_blocks_fault(self):
        lock=self.root/'.state/demo.lock';lock.mkdir(parents=True);(lock/'pid').write_text(str(os.getpid()))
        self.run_lab('fault','stop-broker','1',success=False)
        self.assertFalse(any(a[0]=='stop' for a in self.load().get('calls',[])))
    def test_bad_advertised_address_rejected(self):
        s=(self.root/'.env.example').read_text().replace('ADVERTISED_HOST=localhost','ADVERTISED_HOST=0.0.0.0')
        (self.root/'.env').write_text(s)
        self.run_lab('seed',success=False)
        self.assertEqual(self.load().get('calls',[]),[])
    def test_kraft_version_rejected(self):
        s=(self.root/'.env.example').read_text().replace('CP_VERSION=7.9.0','CP_VERSION=8.0.0')
        (self.root/'.env').write_text(s)
        self.run_lab('up',success=False)
    def test_logs_cannot_target_unrelated_container(self):
        self.run_lab('logs','production-db',success=False)
        self.assertFalse(any(a[0]=='logs' for a in self.load().get('calls',[])))


class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try: import yaml
        except ImportError: raise unittest.SkipTest('PyYAML unavailable; install distro python3-yaml to run YAML tests')
        cls.raw=(ROOT/'compose.yaml').read_text()
        cls.compose=yaml.safe_load(cls.raw)
        cls.ui=yaml.safe_load((ROOT/'compose.ui.yaml').read_text())
    def test_three_plus_three_and_tools(self):
        self.assertEqual(set(self.compose['services']),{'zk1','zk2','zk3','kafka1','kafka2','kafka3','tools'})
    def test_not_kraft(self):
        for i in [1,2,3]:
            env=self.compose['services'][f'kafka{i}']['environment']
            self.assertIn('KAFKA_ZOOKEEPER_CONNECT',env)
            self.assertNotIn('KAFKA_PROCESS_ROLES',env)
            self.assertNotIn('KAFKA_CONTROLLER_QUORUM_VOTERS',env)
    def test_broker_replication_and_safety(self):
        for i in [1,2,3]:
            e=self.compose['services'][f'kafka{i}']['environment']
            self.assertEqual(e['KAFKA_BROKER_ID'],str(i))
            self.assertEqual(e['KAFKA_MIN_INSYNC_REPLICAS'],'2')
            self.assertEqual(e['KAFKA_DEFAULT_REPLICATION_FACTOR'],'3')
            self.assertEqual(e['KAFKA_UNCLEAN_LEADER_ELECTION_ENABLE'],'false')
    def test_zk_distinct_ids_no_host_exposure(self):
        for i in [1,2,3]:
            svc=self.compose['services'][f'zk{i}']
            self.assertEqual(svc['environment']['ZOOKEEPER_SERVER_ID'],str(i))
            self.assertEqual(svc['environment']['ZOOKEEPER_SERVERS'],'zk1:2888:3888;zk2:2888:3888;zk3:2888:3888')
            self.assertNotIn('ports',svc)
    def test_zk_4lw_uses_java_property(self):
        for i in [1,2,3]:
            e=self.compose["services"][f"zk{i}"]["environment"]
            self.assertEqual(e["KAFKA_OPTS"],"-Dzookeeper.4lw.commands.whitelist=ruok,srvr,mntr,stat")
            self.assertNotIn("ZOOKEEPER_4LW_COMMANDS_WHITELIST", e)
    def test_persistent_separate_volumes(self):
        self.assertEqual(len(self.compose['volumes']),9)
        for v in self.compose['volumes'].values(): self.assertIn('io.kzk.lab',v['labels'])
    def test_no_privileged_or_host_socket(self):
        for svc in self.compose['services'].values():
            self.assertFalse(svc.get('privileged',False))
            self.assertNotIn('docker.sock',str(svc));self.assertNotIn('podman.sock',str(svc))
    def test_ui_readonly_and_local_binding(self):
        ui=self.ui['services']['ui']
        self.assertEqual(ui['environment']['KAFKA_CLUSTERS_0_READONLY'],'true')
        self.assertIn('127.0.0.1',ui['ports'][0])
    def test_no_latest_kafka_image(self):
        self.assertNotIn(':latest',self.raw)
    def test_named_volume_chown_not_host_bind(self):
        for name,svc in self.compose['services'].items():
            for volume in svc.get('volumes',[]):
                source,target,options=volume.split(':')
                self.assertIn(source,self.compose['volumes']);self.assertEqual(options,'U')
    def test_default_interpolation_valid_yaml(self):
        import yaml
        expanded=re.sub(r'\$\{[^}:]+:-([^}]*)\}',lambda m:m.group(1),self.raw)
        obj=yaml.safe_load(expanded)
        self.assertEqual(obj['services']['kafka1']['ports'],['127.0.0.1:19092:9093'])
        self.assertEqual(obj['services']['kafka2']['environment']['KAFKA_ADVERTISED_LISTENERS'],
                         'INTERNAL://kafka2:9092,EXTERNAL://localhost:29092')


if __name__=='__main__': unittest.main()
