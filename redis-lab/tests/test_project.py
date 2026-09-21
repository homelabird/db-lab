"""Offline tests. Redis/Podman are deliberately mocked where noted."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'client'))
sys.path.insert(0, str(ROOT / 'scripts'))
import core
import manage


class CoreTests(unittest.TestCase):
    def test_endpoints(self): self.assertEqual(core.endpoint_list('a:6379,b:26379'), [('a',6379),('b',26379)])
    def test_duplicate_endpoint_rejected(self):
        with self.assertRaises(ValueError): core.endpoint_list('a:6379,a:6379')
    def test_port_rejected(self):
        with self.assertRaises(ValueError): core.endpoint_list('a:0')
    def test_empty_host_rejected(self):
        with self.assertRaises(ValueError): core.endpoint_list(':6379')
    def test_run_id(self): self.assertEqual(core.safe_run_id('20260101T090000-deadbeef'), '20260101T090000-deadbeef')
    def test_path_traversal_rejected(self):
        for name in ('../secrets', '/etc/passwd', 'a/b', ''):
            with self.subTest(name=name), self.assertRaises(ValueError): core.safe_run_id(name)
    def test_bounded_int(self): self.assertEqual(core.bounded_int('5',1,10),5)
    def test_int_limit(self):
        with self.assertRaises(ValueError): core.bounded_int('11',1,10)
    def test_payload_size(self): self.assertEqual(len(core.payload_for('run',1,64)),64)
    def test_payload_unique(self): self.assertNotEqual(core.payload_for('run',1,64),core.payload_for('run',2,64))
    def test_error_classification(self):
        for name,text,result in [('OutOfMemoryError','command rejected','memory-limit'),('ResponseError','MISCONF problem','persistence-error'),('AuthenticationError','bad','authentication'),('ReadOnlyError','no writes','wrong-role'),('MasterNotFoundError','no master','master-not-found'),('TimeoutError','slow','timeout')]:
            with self.subTest(name=name): self.assertEqual(core.classify_error(name,text),result)
    def test_acked_present(self): self.assertEqual(core.verification_bucket({'value':'v','write_ack':True},'v'),'acknowledged_present')
    def test_acked_missing(self): self.assertEqual(core.verification_bucket({'value':'v','write_ack':True},None),'acknowledged_missing_or_changed')
    def test_uncertain_present(self): self.assertEqual(core.verification_bucket({'value':'v','write_ack':False},'v'),'uncertain_but_present')
    def test_uncertain_absent(self): self.assertEqual(core.verification_bucket({'value':'v','write_ack':False},None),'uncertain_absent_or_changed')
    def test_rejected_absent(self): self.assertEqual(core.verification_bucket({'value':'v','outcome':'rejected'},None),'rejected_absent')
    def test_not_sent_absent(self): self.assertEqual(core.verification_bucket({'value':'v','outcome':'not-sent'},None),'not_sent_absent')
    def test_read_run_filters_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'run.jsonl';path.write_text('{"event":"start"}\n{"event":"request","value":"v"}\n')
            self.assertEqual(len(list(core.read_run(path))),1)
    def test_truncated_log_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'run.jsonl';path.write_text('{"event":"requ')
            with self.assertRaises(ValueError): list(core.read_run(path))


class EnvironmentTests(unittest.TestCase):
    def setUp(self): self.env=manage.parse_env(ROOT/'.env.example')
    def test_default_env_valid(self): manage.validate_env(self.env)
    def test_cluster_env_valid(self):
        self.env['DEPLOYMENT_MODE'] = 'cluster'
        manage.validate_env(self.env)

    def test_cluster_node_count_bounds(self):
        self.env['CLUSTER_NODE_COUNT'] = '3'
        self.env['CLUSTER_REPLICAS'] = '0'
        manage.validate_env(self.env)
        self.env['CLUSTER_NODE_COUNT'] = '101'
        with self.assertRaises(ValueError): manage.validate_env(self.env)

    def test_invalid_deployment_mode_rejected(self):
        self.env['DEPLOYMENT_MODE'] = 'active-active'
        with self.assertRaises(ValueError): manage.validate_env(self.env)
    def test_password_shell_injection_rejected(self):
        self.env['REDIS_PASSWORD']='$(touch /tmp/bad)'
        with self.assertRaises(ValueError): manage.validate_env(self.env)
    def test_duplicate_ip_rejected(self):
        self.env['REDIS_2_IP']=self.env['REDIS_1_IP']
        with self.assertRaises(ValueError): manage.validate_env(self.env)
    def test_gateway_ip_rejected(self):
        self.env['REDIS_1_IP']='10.89.77.1'
        with self.assertRaises(ValueError): manage.validate_env(self.env)
    def test_outside_subnet_rejected(self):
        self.env['REDIS_1_IP']='192.168.1.11'
        with self.assertRaises(ValueError): manage.validate_env(self.env)
    def test_bad_lab_name(self):
        self.env['LAB_NAME']='../../etc'
        with self.assertRaises(ValueError): manage.validate_env(self.env)
    def test_duplicate_env_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env';path.write_text('KEY=a\nKEY=b\n')
            with self.assertRaises(ValueError): manage.parse_env(path)
    def test_env_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env';path.write_text('KEY=$(echo hello)\n')
            self.assertEqual(manage.parse_env(path)['KEY'],'$(echo hello)')
    def test_random_env_creation_and_reuse(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manage,'ROOT',Path(directory)):
            (Path(directory)/'.env.example').write_text((ROOT/'.env.example').read_text())
            first=manage.load_env(); second=manage.load_env()
            self.assertEqual(first['REDIS_PASSWORD'],second['REDIS_PASSWORD'])
            self.assertNotEqual(first['REDIS_PASSWORD'],self.env['REDIS_PASSWORD'])
            self.assertEqual((Path(directory)/'.env').stat().st_mode & 0o777,0o600)


class ManagementTests(unittest.TestCase):
    def setUp(self): self.lab=manage.Lab(manage.parse_env(ROOT/'.env.example'))
    def test_node_allowlist(self):
        with self.assertRaises(ValueError): self.lab.cname('production-redis')
    def test_volume_allowlist(self):
        with self.assertRaises(ValueError): self.lab.volume_owned('production')
    def test_password_redaction(self):
        self.assertNotIn(self.lab.env['REDIS_PASSWORD'],self.lab.redact(self.lab.env['REDIS_PASSWORD']))
    def test_unowned_container_refused(self):
        self.lab.run=Mock(return_value=subprocess.CompletedProcess([],0,json.dumps([{'Config':{'Labels':{}}}]),''))
        with self.assertRaises(RuntimeError): self.lab.inspect('redis-1')
    def test_owned_container_allowed(self):
        obj={'Config':{'Labels':{manage.LABEL:self.lab.name}}}
        self.lab.run=Mock(return_value=subprocess.CompletedProcess([],0,json.dumps([obj]),''))
        self.assertEqual(self.lab.inspect('redis-1'),obj)
    def test_unowned_volume_refused(self):
        self.lab.run=Mock(return_value=subprocess.CompletedProcess([],0,'[{"Labels":{}}]',''))
        with self.assertRaises(RuntimeError): self.lab.volume_owned('sandbox-data')
    def test_reset_requires_confirmation(self):
        self.lab.run=Mock()
        with self.assertRaises(ValueError): self.lab.down(reset=True)
        self.lab.run.assert_not_called()
    def test_restore_requires_confirmation(self):
        self.lab.run=Mock()
        with self.assertRaises(ValueError): self.lab.restore('/tmp/file.rdb',False)
        self.lab.run.assert_not_called()
    def test_cli_password_not_in_argv(self):
        self.lab.execute=Mock(return_value=None)
        self.lab.cli('redis-1',['SET','lab:key','hello world'])
        command=self.lab.execute.call_args.args[1]
        self.assertNotIn(self.lab.env['REDIS_PASSWORD'],' '.join(command))
        self.assertEqual(command[-1],'hello world')
        self.assertIn('$REDIS_PASSWORD',command[2])
    def test_sentinel_cli_uses_correct_password(self):
        self.lab.execute=Mock(return_value=None);self.lab.cli('sentinel-1',['PING'])
        command=self.lab.execute.call_args.args[1]
        self.assertIn('$SENTINEL_PASSWORD',command[2]);self.assertIn('26379',command[2])
    def test_fault_dynamically_targets_current_master(self):
        self.lab.master=Mock(return_value='redis-3'); self.lab.inspect=Mock(return_value={'State':{'Running':True}})
        self.lab.state=Mock(return_value={});self.lab.save_state=Mock();self.lab.run=Mock()
        self.lab.fault('kill-master',None)
        self.assertEqual(self.lab.run.call_args.args[0][-1],'rslab-redis-3')
        self.assertEqual(self.lab.save_state.call_args.args[0]['redis-3']['action'],'kill')
    def test_fault_recovery_preserves_original_ip(self):
        self.lab.inspect=Mock(return_value={'NetworkSettings':{'Networks':{}}});self.lab.run=Mock()
        self.lab.connect_network('redis-2')
        command=self.lab.run.call_args.args[0]
        self.assertIn('10.89.77.12',command); self.assertIn('--alias',command)
    def test_workload_cli_parser(self):
        args=manage.parser().parse_args(['workload','--seconds','60','--mode','fixed','--fixed-node','redis-2'])
        self.assertEqual(args.seconds,60);self.assertEqual(args.fixed_node,'redis-2')

    def test_cluster_nodes_selects_requested_prefix(self):
        self.lab.env['DEPLOYMENT_MODE'] = 'cluster'
        self.lab.env['CLUSTER_NODE_COUNT'] = '3'
        self.assertEqual(self.lab.cluster_nodes(), ['redis-cluster-1', 'redis-cluster-2', 'redis-cluster-3'])

    def test_cluster_init_parser_accepts_node_count(self):
        args = manage.parser().parse_args(['cluster-init', '--cluster-nodes', '3'])
        self.assertEqual(args.cluster_nodes, 3)

    def test_seed_simulation_parser(self):
        args = manage.parser().parse_args(['seed-simulate', '--seconds', '60',
                                           '--interval', '2', '--rate', '5',
                                           '--profiles', 'events,counters',
                                           '--mix', 'read:70,write:25,delete:5',
                                           '--hotset', '100', '--jitter', '0.2', '--ttl', '60'])
        self.assertEqual((args.seconds, args.interval, args.rate, args.profiles),
                         (60, 2, 5, 'events,counters'))
        self.assertEqual((args.mix, args.hotset, args.jitter, args.ttl),
                         ('read:70,write:25,delete:5', 100, 0.2, 60))

    def test_user_friendly_aliases(self):
        self.assertEqual(manage.parser().parse_args(['st']).command, 'st')
        self.assertEqual(manage.parser().parse_args(['sim']).command, 'sim')
        self.assertEqual(manage.parser().parse_args(['stop']).command, 'stop')
    def test_redis_cli_args_not_consumed(self):
        args=manage.parser().parse_args(['cli','redis-1','SCAN','0','MATCH','lab:*'])
        self.assertEqual(args.redis_args,['SCAN','0','MATCH','lab:*'])
    def test_environment_change_is_detected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manage,'ROOT',Path(directory)):
            (Path(directory)/'.lab').mkdir();self.lab.check_env_state(save=True)
            self.lab.env['REDIS_PASSWORD']='DifferentPassword123'
            with self.assertRaises(RuntimeError): self.lab.check_env_state()


# Import client logic without pretending to have executed redis-py or a live Redis.
# Production client dependencies are installed by the Containerfile, not these mocks.
def load_client_with_stub():
    package=types.ModuleType('redis');package.__path__=[]
    package.Redis=Mock;package.RedisError=Exception;package.ResponseError=RuntimeError
    modules={'redis':package}
    for module,name in [('redis.backoff','NoBackoff'),('redis.retry','Retry'),('redis.sentinel','Sentinel')]:
        obj=types.ModuleType(module);setattr(obj,name,Mock);modules[module]=obj
    spec=importlib.util.spec_from_file_location('lab_client_test',ROOT/'client/client.py')
    client=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,modules): spec.loader.exec_module(client)
    return client
CLIENT=load_client_with_stub()


def healthy():
    state={'redis':{},'sentinel':{}}
    for i in range(1,4):
        state['redis'][f'redis-{i}']={'reachable':True,'address':f'10.89.77.{10+i}',
            'role':'master' if i==1 else 'slave','connected_slaves':2,
            'master_host':'10.89.77.11','master_link_status':'up','master_sync_in_progress':0}
        state['sentinel'][f'sentinel-{i}']={'reachable':True,'master':'10.89.77.11',
            'flags':'master','quorum':'OK 3 usable Sentinels','other_sentinels':2,'replicas':2}
    return state


class TopologyTests(unittest.TestCase):
    def test_healthy_topology(self): self.assertEqual(CLIENT.topology_errors(healthy()),[])
    def test_two_masters_rejected(self):
        s=healthy();s['redis']['redis-2']['role']='master';self.assertTrue(CLIENT.topology_errors(s))
    def test_quorum_failure_rejected(self):
        s=healthy();s['sentinel']['sentinel-1']['quorum']='NOQUORUM';self.assertTrue(CLIENT.topology_errors(s))
    def test_missing_sentinel_rejected(self):
        s=healthy();s['sentinel']['sentinel-1']={'reachable':False};self.assertTrue(CLIENT.topology_errors(s))
    def test_replica_wrong_upstream_rejected(self):
        s=healthy();s['redis']['redis-2']['master_host']='10.89.77.13';self.assertTrue(CLIENT.topology_errors(s))
    def test_replica_syncing_rejected(self):
        s=healthy();s['redis']['redis-2']['master_sync_in_progress']=1;self.assertTrue(CLIENT.topology_errors(s))
    def test_sentinel_stale_view_rejected(self):
        s=healthy();s['sentinel']['sentinel-1']['master']='10.89.77.13';self.assertTrue(CLIENT.topology_errors(s))
    def test_sdown_rejected(self):
        s=healthy();s['sentinel']['sentinel-1']['flags']='master,s_down';self.assertTrue(CLIENT.topology_errors(s))
    def test_external_master_rejected(self):
        with self.assertRaises(ValueError): CLIENT.node_name('203.0.113.1')

    def test_simulation_mix_normalizes_weights(self):
        self.assertEqual(CLIENT.parse_mix('read:70,write:25,delete:5'),
                         {'read': 0.7, 'write': 0.25, 'delete': 0.05})

    def test_simulation_mix_rejects_invalid_values(self):
        for value in ('read:-1,write:1', 'read:0,write:0', 'read:1,unknown:1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CLIENT.parse_mix(value)

    def test_cluster_slot_coverage(self):
        previous = CLIENT.DEPLOYMENT_MODE
        CLIENT.DEPLOYMENT_MODE = 'cluster'
        try:
            state = {'redis': {}, 'cluster': {}}
            for i in range(1, 7):
                name = f'redis-cluster-{i}'
                state['redis'][name] = {'reachable': True}
                state['cluster'][name] = {
                    'cluster_state': 'ok',
                    'cluster_slots_assigned': '16384',
                    'cluster_slots_ok': '16384',
                    'cluster_slots_fail': '0',
                    'cluster_slots_pfail': '0',
                    'cluster_known_nodes': '6',
                }
            self.assertEqual(CLIENT.topology_errors(state), [])
            state['cluster']['redis-cluster-1']['cluster_slots_ok'] = '16383'
            self.assertTrue(CLIENT.topology_errors(state))
        finally:
            CLIENT.DEPLOYMENT_MODE = previous


try:
    import yaml
except ImportError:
    yaml=None

@unittest.skipIf(yaml is None, 'PyYAML not installed; YAML parse tests skipped (other tests use only standard library)')
class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.doc=yaml.safe_load((ROOT/'compose.yaml').read_text())
    def test_seven_default_services(self):
        self.assertEqual(len([s for s in self.doc['services'].values() if not s.get('profiles')]),7)
    def test_sandbox_is_optional(self): self.assertEqual(self.doc['services']['redis-sandbox']['profiles'],['sandbox'])
    def test_no_published_ports(self): self.assertTrue(all(not s.get('ports') for s in self.doc['services'].values()))
    def test_no_privileged_services(self): self.assertTrue(all(not s.get('privileged') for s in self.doc['services'].values()))
    def test_no_bind_mounts(self):
        for service in self.doc['services'].values():
            for mount in service.get('volumes',[]): self.assertFalse(mount.startswith(('./','/','../')))
    def test_no_automatic_restart(self): self.assertTrue(all(s['restart']=='no' for s in self.doc['services'].values()))
    def test_separate_pod_namespaces(self): self.assertIs(self.doc['x-podman']['in_pod'],False)
    def test_distinct_writable_sentinel_volumes(self):
        volumes=[self.doc['services'][n]['volumes'][0] for n in manage.SENTINELS]
        self.assertEqual(len(set(volumes)),3);self.assertTrue(all(v.endswith(':/data') for v in volumes))
    def test_all_services_owned(self):
        for service in self.doc['services'].values(): self.assertIn(manage.LABEL,service['labels'])
    def test_all_static_addresses_distinct(self):
        addresses=[s['networks']['labnet']['ipv4_address'] for s in self.doc['services'].values()]
        self.assertEqual(len(addresses),len(set(addresses)))


@unittest.skipIf(yaml is None, 'PyYAML not installed; YAML parse tests skipped')
class ClusterComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.doc=yaml.safe_load((ROOT/'compose.cluster.yaml').read_text())

    def test_six_cluster_nodes_and_client(self):
        self.assertEqual(
            sorted(name for name in self.doc['services'] if name.startswith('redis-cluster-')),
            [f'redis-cluster-{i}' for i in range(1, 7)],
        )
        self.assertEqual(set(self.doc['services']) - {'lab-client'},
                         {f'redis-cluster-{i}' for i in range(1, 7)})

    def test_cluster_has_no_sentinels_or_published_ports(self):
        self.assertFalse(any(name.startswith('sentinel-') for name in self.doc['services']))
        self.assertTrue(all(not service.get('ports') for service in self.doc['services'].values()))

    def test_cluster_nodes_use_distinct_data_volumes(self):
        volumes = [self.doc['services'][f'redis-cluster-{i}']['volumes'][0] for i in range(1, 7)]
        self.assertEqual(len(set(volumes)), 6)

    def test_cluster_environment_and_bus_port_are_configured(self):
        node = self.doc['services']['redis-cluster-1']
        self.assertEqual(node['environment']['LAB_MODE'], 'cluster')
        self.assertIn('CLUSTER_BUS_PORT', node['environment'])
        self.assertIn('cluster-announce-bus-port @@CLUSTER_BUS_PORT@@',
                      (ROOT / 'config/cluster.conf.template').read_text())

if __name__=='__main__': unittest.main(verbosity=2)
