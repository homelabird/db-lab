"""Actual guard functions with fake external tools; no engines or clusters."""
import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
control=load('control');k8s=load('k8s_guard')

class ControlTests(unittest.TestCase):
    def test_settings_are_data_and_env_overrides_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'kafka-lab').mkdir();(root/'kafka-lab/.env').write_text('NODES=3\nKAFKA_HEAP_OPTS=$(touch /tmp/never-execute)\n')
            with patch.object(control,'ROOT',root),patch.dict(os.environ,{'NODES':'11'}):
                result=control.settings('kafka')
            self.assertEqual(result['NODES'],'11');self.assertIn('$(touch',result['KAFKA_HEAP_OPTS'])
    def test_duplicate_settings_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'kafka-lab').mkdir();(root/'kafka-lab/.env').write_text('NODES=3\nNODES=11\n')
            with patch.object(control,'ROOT',root),self.assertRaises(ValueError):control.settings('kafka')
    def test_kafka_ports_and_bounds(self):
        ports=control.planned_ports('kafka',{'NODES':'11','KAFKA_MODE':'kraft','KAFKA10_PORT':'45000'})
        self.assertEqual(ports[9][1],45000)
        for values in ({'NODES':'101'},{'NODES':'2'},{'KAFKA1_PORT':'70000'}):
            with self.subTest(values=values),self.assertRaises(ValueError):control.planned_ports('kafka',values)
    def test_preflight_detects_planned_collisions(self):
        settings={'elasticsearch':{'ES_PORT':'19092'},'kafka':{'NODES':'3'}}
        with patch.object(control,'settings',side_effect=lambda p:settings[p]),patch.object(control.shutil,'which',return_value=None):
            data=control.preflight(['elasticsearch','kafka'])
        self.assertFalse(data['ok']);self.assertTrue(any('Port collision' in x for x in data['errors']))
    def test_existing_public_es_is_rejected(self):
        with patch.object(control,'settings',return_value={'ES_BIND_IP':'0.0.0.0'}),patch.object(control.shutil,'which',return_value=None):
            data=control.preflight(['elasticsearch'])
        self.assertTrue(any('ES_ALLOW_PUBLIC_BIND' in x for x in data['errors']))
    def test_uninitialized_health_never_starts_children(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(control,'ROOT',Path(tmp)),patch.object(control.subprocess,'run') as run:
            result=control.health(['redis','mariadb'])
        self.assertFalse(result['ready']);run.assert_not_called()
    def test_health_failure_does_not_echo_native_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'redis-lab').mkdir();(root/'redis-lab/.env').touch()
            with patch.object(control,'ROOT',root),patch.object(control.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'secret-password','secret-stderr')):
                result=control.health(['redis'])
        self.assertNotIn('secret-password',json.dumps(result));self.assertFalse(result['ready'])
    def test_receipts_are_private_and_not_health_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'run.json';control.record(path,'up','__start__',0);control.record(path,'up','kafka',7);control.record(path,'up','__finish__',1)
            result=json.loads(path.read_text());self.assertEqual(result['status'],'failed');self.assertFalse(result['health_verified']);self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_cli_default_selection_parses(self):
        p=subprocess.run([os.sys.executable,str(ROOT/'scripts/control.py'),'health','--json'],capture_output=True,text=True)
        self.assertNotEqual(p.returncode,2);self.assertEqual(len(json.loads(p.stdout)['checks']),4)

class ExposureTests(unittest.TestCase):
    def run_guard(self,bind,allow='no'):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'scripts').mkdir();(root/'scripts/common.sh').write_bytes((ROOT/'elasticsearch/scripts/common.sh').read_bytes())
            (root/'.env').write_text(f'ES_BIND_IP={bind}\nES_ALLOW_PUBLIC_BIND={allow}\n')
            env={k:v for k,v in os.environ.items() if not k.startswith('ES_')}
            return subprocess.run(['bash','-c','source "$1"; check_exposure','bash',str(root/'scripts/common.sh')],env=env,capture_output=True,text=True)
    def test_loopback_no_ack(self):self.assertEqual(self.run_guard('127.0.0.1').returncode,0)
    def test_old_public_env_refused(self):self.assertNotEqual(self.run_guard('0.0.0.0').returncode,0)
    def test_public_ack_warns(self):
        p=self.run_guard('0.0.0.0','yes');self.assertEqual(p.returncode,0);self.assertIn('Unauthenticated',p.stderr)
    def test_invalid_address_refused(self):self.assertNotEqual(self.run_guard('localhost;bad','yes').returncode,0)
    def test_up_contains_no_host_policy_write(self):
        source=(ROOT/'elasticsearch/scripts/01-up.sh').read_text()
        self.assertNotIn('iptables',source);self.assertNotIn('sudo -n',source)
        self.assertNotRegex(source, r'(?m)^\s*sudo\s')
        self.assertLess(source.index('check_exposure'),source.index('compose up'))

class K8sGuardTests(unittest.TestCase):
    def setUp(self):
        self.args=argparse.Namespace(context='kind-db-lab',namespace='db-lab',release='training',chart=str(ROOT/'helmchart'),kubeconfig='/tmp/labconfig',secret='db-lab-credentials',helm_args=[])
        self.guard=k8s.Guard(self.args)
        self.secret={'data':{k:base64.b64encode(('a'*32).encode()).decode() for k in k8s.KEYS}}
    def test_default_and_malformed_credentials_refused(self):
        for password in ('change-me','short','x'*129,'not safe '*4):self.assertFalse(k8s.valid_password(password))
        self.assertTrue(k8s.valid_password('safe_-01'*4))
    def test_secret_keys_checked(self):
        with patch.object(self.guard,'get',return_value=self.secret):self.guard.check_secret('test',k8s.KEYS)
        with patch.object(self.guard,'get',return_value={'data':{}}),self.assertRaises(ValueError):self.guard.check_secret('test',k8s.KEYS)
    def test_init_preserves_existing_credentials(self):
        with patch.object(self.guard,'get',side_effect=lambda kind,name:self.secret if kind=='secret' else {'metadata':{'name':name}}),patch.object(self.guard,'call') as call,contextlib.redirect_stderr(io.StringIO()):self.guard.initialize()
        call.assert_not_called()
    def test_init_creates_secret_via_stdin_only(self):
        with patch.object(self.guard,'get',return_value=None),patch.object(self.guard,'call') as call,contextlib.redirect_stderr(io.StringIO()):self.guard.initialize()
        args,payload=call.call_args.args
        obj=json.loads(payload);self.assertEqual(set(obj['stringData']),set(k8s.KEYS));self.assertIn('--context',args)
        for v in obj['stringData'].values():self.assertNotIn(v,' '.join(args));self.assertTrue(k8s.valid_password(v))
    def test_failed_command_never_echoes_secret_output(self):
        with patch.object(k8s.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'sensitive','secret')):
            with self.assertRaises(ValueError) as error:self.guard.call(['helm','template'])
        self.assertNotIn('secret',str(error.exception));self.assertNotIn('sensitive',str(error.exception))
    def manifest(self):
        return {'apiVersion':'apps/v1','kind':'StatefulSet','metadata':{'name':'training-db-lab-mariadb'},'spec':{'replicas':3,'selector':{'matchLabels':{'app.kubernetes.io/instance':'training'}},'template':{'spec':{'containers':[{'env':[{'name':'BOOTSTRAP_NEW_CLUSTER','value':'yes'}]}]}},'volumeClaimTemplates':[{'metadata':{'name':'data'},'spec':{}}]}}
    def call_for(self,obj,prior=None):
        def call(args,**kwargs):
            if args[:2]==['helm','list']:return '[{}]' if prior else '[]'
            if args[:3]==['helm','get','values']:return json.dumps(prior)
            if args[:2]==['helm','template']:
                if prior:
                    pos=args.index('-f');saved=json.loads(Path(args[pos+1]).read_text());self.assertEqual(saved,prior)
                return json.dumps(obj)
            return 'namespace/db-lab'
        return call
    def get_for(self,pvcs=(),old=None,classes=True):
        def get(kind,name=None):
            if kind=='statefulset':return old
            if kind=='pvc':return {'items':[{'metadata':{'name':x}} for x in pvcs]}
            if kind=='storageclass':return {'items':[{'metadata':{'name':'standard','annotations':{'storageclass.kubernetes.io/is-default-class':'true'}}}] if classes else []}
            raise AssertionError(kind)
        return get
    def test_fresh_bootstrap_refuses_existing_pvc(self):
        obj=self.manifest()
        with patch.object(self.guard,'call',side_effect=self.call_for(obj)),patch.object(self.guard,'get',side_effect=self.get_for(['data-training-db-lab-mariadb-0'])),self.assertRaisesRegex(ValueError,'Fresh bootstrap refused'):self.guard.preflight()
    def test_immutable_selector_migration_refused(self):
        obj=self.manifest();old={'spec':{'selector':{'matchLabels':{'component':'mariadb'}}}}
        with patch.object(self.guard,'call',side_effect=self.call_for(obj)),patch.object(self.guard,'get',side_effect=self.get_for(old=old)),self.assertRaisesRegex(ValueError,'immutable selector'):self.guard.preflight()
    def test_missing_default_storageclass_refused(self):
        with patch.object(self.guard,'call',side_effect=self.call_for(self.manifest())),patch.object(self.guard,'get',side_effect=self.get_for(classes=False)),self.assertRaisesRegex(ValueError,'StorageClass'):self.guard.preflight()
    def test_old_chart_migration_refused(self):
        def call(args,**kwargs):
            if args[:2]==['helm','list']:return '[{"chart":"db-lab-0.1.0"}]'
            return 'namespace/db-lab'
        with patch.object(self.guard,'call',side_effect=call),self.assertRaisesRegex(ValueError,'in-place migration'):self.guard.preflight()
    def test_existing_values_used_and_bootstrap_sealing_reported(self):
        stream=io.StringIO()
        with patch.object(self.guard,'call',side_effect=self.call_for(self.manifest(),{'redis':{'enabled':False}})),patch.object(self.guard,'get',side_effect=self.get_for()),contextlib.redirect_stdout(stream):self.guard.preflight()
        self.assertTrue(json.loads(stream.getvalue())['seal_needed'])

if __name__=='__main__':unittest.main()
