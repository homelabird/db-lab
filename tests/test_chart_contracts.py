"""Go-template SUBSET + YAML/schema tests, NOT Helm or Kubernetes validation."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import jsonschema
import yaml

ROOT=Path(__file__).resolve().parents[1];CHART=ROOT/'helmchart'
class ChartContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.base=Path(cls.tmp.name)
        cls.values=yaml.safe_load((CHART/'values.yaml').read_text());cls.schema=json.loads((CHART/'values.schema.json').read_text());cls.chart=yaml.safe_load((CHART/'Chart.yaml').read_text())
        cls.renderer=os.environ.get('DB_LAB_CONTRACT_RENDERER')
        if not cls.renderer:
            if not shutil.which('go'):raise unittest.SkipTest('Go is missing: chart subset renderer NOT RUN; run real Helm separately')
            cls.renderer=str(cls.base/'renderer');subprocess.run(['go','build','-o',cls.renderer,str(ROOT/'tests/tooling/render_contract.go')],check=True,timeout=120)
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def full(self):
        values=copy.deepcopy(self.values)
        for component in ('elasticsearch','kafka','mariadb'):values[component]['enabled']=True
        values['elasticsearch']['allowLegacy']=True;return values
    def render(self,values=None,release='alpha',check=True):
        values=copy.deepcopy(self.values if values is None else values)
        jsonschema.validate(values,self.schema)
        context={'Values':values,'Release':{'Name':release,'Namespace':'lab-space','Service':'Helm'},'Chart':{'Name':self.chart['name'],'Version':self.chart['version']}}
        path=self.base/'context.json';path.write_text(json.dumps(context))
        p=subprocess.run([self.renderer,str(CHART),str(path)],capture_output=True,text=True,timeout=20)
        if not check:return p
        self.assertEqual(p.returncode,0,p.stderr)
        return [x for x in yaml.safe_load_all(p.stdout) if x]
    def test_default_only_redis_and_sentinels(self):
        docs=self.render();states=[d for d in docs if d['kind']=='StatefulSet']
        self.assertEqual({d['spec']['selector']['matchLabels']['component'] for d in states},{'redis','redis-sentinel'})
    def test_full_profile_and_service_selectors(self):
        docs=self.render(self.full());states=[d for d in docs if d['kind']=='StatefulSet'];self.assertEqual(len(states),6)
        for svc in [d for d in docs if d['kind']=='Service']:
            selector=svc['spec']['selector'];self.assertEqual(selector['app.kubernetes.io/instance'],'alpha')
            matches=[s for s in states if all(s['spec']['template']['metadata']['labels'].get(k)==v for k,v in selector.items())];self.assertEqual(len(matches),1)
    def test_two_releases_do_not_share_selectors(self):
        left=self.render(self.full(),'alpha');right=self.render(self.full(),'beta')
        right_labels=[x['spec']['template']['metadata']['labels'] for x in right if x['kind']=='StatefulSet']
        for svc in [x for x in left if x['kind']=='Service']:
            self.assertFalse(any(all(p.get(k)==v for k,v in svc['spec']['selector'].items()) for p in right_labels))
    def test_long_names_do_not_collide(self):
        a=self.render(release='a'*45+'first');b=self.render(release='a'*45+'second')
        self.assertFalse({x['metadata']['name'] for x in a}&{x['metadata']['name'] for x in b})
        self.assertTrue(all(len(x['metadata']['name'])<=63 for x in a+b))
    def test_pod_readiness_resources_and_parallel_bootstrap(self):
        for state in [x for x in self.render(self.full()) if x['kind']=='StatefulSet']:
            self.assertEqual(state['spec']['podManagementPolicy'],'Parallel');pod=state['spec']['template']['spec']
            self.assertFalse(pod['automountServiceAccountToken'])
            for c in pod['containers']:
                self.assertIn('startupProbe',c);self.assertIn('readinessProbe',c)
                for kind in ('requests','limits'):self.assertEqual(set(c['resources'][kind]),{'cpu','memory'})
    def test_peer_dns_published_before_readiness(self):
        for s in [x for x in self.render(self.full()) if x['kind']=='Service' and x['spec'].get('clusterIP')=='None']:self.assertTrue(s['spec']['publishNotReadyAddresses'])
    def test_mariadb_storage_and_credentials(self):
        state=next(x for x in self.render(self.full()) if x['kind']=='StatefulSet' and x['metadata']['name'].endswith('-mariadb'))
        c=state['spec']['template']['spec']['containers'][0];self.assertIn('/bitnami/mariadb',[x['mountPath'] for x in c['volumeMounts']])
        for e in c['env']:
            if 'PASSWORD' in e['name']:self.assertIn('secretKeyRef',e['valueFrom'])
    def test_kafka_dependent_environment_order(self):
        state=next(x for x in self.render(self.full()) if x['kind']=='StatefulSet' and x['metadata']['name'].endswith('-kafka'))
        env=state['spec']['template']['spec']['containers'][0]['env'];names=[x['name'] for x in env]
        self.assertLess(names.index('POD_NAME'),names.index('KAFKA_ADVERTISED_LISTENERS'))
        self.assertIn('.svc.cluster.local',next(x['value'] for x in env if x['name']=='KAFKA_ADVERTISED_LISTENERS'))
    def test_zookeeper_actual_image_directories_and_ids(self):
        state=next(x for x in self.render(self.full()) if x['kind']=='StatefulSet' and x['metadata']['name'].endswith('-zookeeper'));c=state['spec']['template']['spec']['containers'][0]
        self.assertIn('/var/lib/zookeeper',[x['mountPath'] for x in c['volumeMounts']]);self.assertIn('+ 1',c['args'][0]);self.assertIn('/var/lib/zookeeper/log',c['args'][0])
        peers=next(e['value'] for e in c['env'] if e['name']=='ZOOKEEPER_SERVERS');self.assertEqual(peers.count('.svc.cluster.local'),3)
    def test_sentinel_persistent_config_and_hostname_contract(self):
        script=(CHART/'scripts/sentinel-start.sh').read_text();self.assertIn('/data/sentinel.conf',script);self.assertIn('sentinel resolve-hostnames yes',script);self.assertIn('sentinel announce-hostnames yes',script)
    def test_role_discovery_and_no_forced_galera_recovery(self):
        redis=(CHART/'scripts/redis-start.sh').read_text();self.assertIn('$1 >= 2',redis);self.assertIn('replicaof %s 6379',redis)
        maria=(CHART/'scripts/mariadb-start.sh').read_text();self.assertNotIn('FORCE_SAFETOBOOTSTRAP=yes',maria);self.assertIn('safe_to_bootstrap',maria)
    def test_unsupported_node_count_and_plain_password_rejected(self):
        for component in ('redis','kafka','mariadb','elasticsearch','zookeeper'):
            values=self.full();values[component]['replicas']=2
            with self.subTest(component=component),self.assertRaises(jsonschema.ValidationError):self.render(values)
        values=self.full();values['redis']['password']='change-me'
        with self.assertRaises(jsonschema.ValidationError):self.render(values)
    def test_bad_mode_and_legacy_not_silently_enabled(self):
        values=self.full();values['zookeeper']['enabled']=False;self.assertNotEqual(self.render(values,check=False).returncode,0)
        values=self.full();values['elasticsearch']['allowLegacy']=False;self.assertNotEqual(self.render(values,check=False).returncode,0)
    def test_bootstrap_and_recovery_mutually_exclusive(self):
        v=self.full();v['mariadb']['bootstrapNewCluster']=True;v['mariadb']['recovery']={'bootstrapOrdinal':0,'confirmed':True};self.assertNotEqual(self.render(v,check=False).returncode,0)
    def test_config_changes_trigger_rollout(self):
        states=[x for x in self.render(self.full()) if x['kind']=='StatefulSet' and x['spec']['selector']['matchLabels']['component'] in ('redis','redis-sentinel','mariadb')]
        self.assertEqual(len(states),3)
        for s in states:self.assertTrue(s['spec']['template']['metadata']['annotations'])
    def test_network_policy_and_distributed_opt_in(self):
        v=self.full();v['scheduling']['requireSeparateNodes']=True;docs=self.render(v)
        policy=next(x for x in docs if x['kind']=='NetworkPolicy');self.assertEqual(policy['spec']['podSelector']['matchLabels']['app.kubernetes.io/instance'],'alpha')
        for s in [x for x in docs if x['kind']=='StatefulSet']:self.assertIn('requiredDuringSchedulingIgnoredDuringExecution',s['spec']['template']['spec']['affinity']['podAntiAffinity'])

if __name__=='__main__':unittest.main()
