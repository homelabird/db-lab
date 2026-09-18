"""Host-only tests: no containers/DB; they cannot prove SQL or replication runtime behavior."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import lab
import health
import state
import seed
import dashboard

UUID = 'b341d9ac-7280-11ef-a073-11bb33cc44dd'

def states(positions=(42,42,42), safe='galera1'):
    return {n: {'initialized':True, 'init_complete':True, 'uuid':UUID,
                'seqno':positions[i], 'safe_to_bootstrap':int(n == safe)}
            for i,n in enumerate(lab.NODES)}

def ready_status():
    return dict(sql_alive=True, wsrep_cluster_status='Primary', wsrep_local_state='4',
                wsrep_ready='ON', wsrep_connected='ON')

class RecoveryTests(unittest.TestCase):
    def test_empty_cluster(self):
        data = {n:dict(s,initialized=False,init_complete=False,seqno=-1,uuid=None,safe_to_bootstrap=0)
                for n,s in states().items()}
        self.assertEqual(lab.select_safe_node(data), ('galera1','authorize-new'))
    def test_unique_safe_node_not_assumed_one(self):
        self.assertEqual(lab.select_safe_node(states(safe='galera3'))[0], 'galera3')
    def test_missing_node_check_refused(self):
        data = states(); data.pop('galera2')
        with self.assertRaises(lab.LabError): lab.select_safe_node(data)
    def test_partial_initialization_refused(self):
        data=states(); data['galera2']['init_complete']=False
        with self.assertRaises(lab.LabError): lab.select_safe_node(data)
    def test_no_safe_node_refused(self):
        with self.assertRaises(lab.LabError): lab.select_safe_node(states(safe=None))
    def test_two_safe_flags_refused(self):
        data=states(); data['galera2']['safe_to_bootstrap']=1
        with self.assertRaises(lab.LabError): lab.select_safe_node(data)
    def test_known_more_advanced_node_refused(self):
        with self.assertRaises(lab.LabError): lab.select_safe_node(states((42,45,42)))
    def test_unknown_position_requires_recovery_even_with_safe_flag(self):
        with self.assertRaises(lab.LabError): lab.select_safe_node(states((42,-1,42)))
    def test_mixed_history_refused(self):
        data=states(); data['galera2']['uuid']='c'*36
        with self.assertRaises(lab.LabError): lab.select_safe_node(data)
    def test_zero_uuid_refused(self):
        data=states()
        for s in data.values(): s['uuid']='00000000-0000-0000-0000-000000000000'
        with self.assertRaises(lab.LabError): lab.select_safe_node(data)
    def test_recovered_highest(self):
        self.assertEqual(lab.select_recovered_node(states((41,49,44),None)),'galera2')
    def test_recovered_tie_deterministic(self):
        self.assertEqual(lab.select_recovered_node(states((49,49,44),None)),'galera1')
    def test_recovered_missing_position_refused(self):
        with self.assertRaises(lab.LabError): lab.select_recovered_node(states((42,-1,43)))
    def test_recovered_mixed_uuid_refused(self):
        data=states(); data['galera2']['uuid']='d'*36
        with self.assertRaises(lab.LabError): lab.select_recovered_node(data)
    def test_recovered_uninitialized_refused(self):
        data=states(); data['galera2']['initialized']=False
        with self.assertRaises(lab.LabError): lab.select_recovered_node(data)
    def test_saved_state_parsing(self):
        self.assertEqual(state.parse_state(f'version: 2.1\nuuid: {UUID}\nseqno: 77\nsafe_to_bootstrap: 1\n'),
                         dict(uuid=UUID,seqno=77,safe_to_bootstrap=1))
    def test_empty_state_is_not_safe(self):
        self.assertEqual(state.parse_state(''),dict(uuid=None,seqno=-1,safe_to_bootstrap=0))
    def test_recovery_log_last_position(self):
        self.assertEqual(state.recovered_position(f'WSREP: Recovered position: {UUID}:3\nWSREP: Recovered position: {UUID}:9'),
                         dict(uuid=UUID,seqno=9))
    def test_missing_recovery_log_position(self):
        with self.assertRaises(ValueError): state.recovered_position('InnoDB started')
    def test_negative_recovery_position(self):
        with self.assertRaises(ValueError): state.recovered_position(f'Recovered position: {UUID}:-1')
    def test_recovery_zero_uuid(self):
        with self.assertRaises(ValueError): state.recovered_position('Recovered position: 00000000-0000-0000-0000-000000000000:0')

class HealthTests(unittest.TestCase):
    def test_primary_synced_ready(self): self.assertTrue(health.is_ready(ready_status()))
    def test_sql_alive_not_sufficient(self): self.assertFalse(health.is_ready({'sql_alive':True}))
    def test_no_sql_connection(self): self.assertFalse(health.is_ready(dict(ready_status(),sql_alive=False)))
    def test_nonprimary(self): self.assertFalse(health.is_ready(dict(ready_status(),wsrep_cluster_status='non-Primary')))
    def test_donor_not_served(self): self.assertFalse(health.is_ready(dict(ready_status(),wsrep_local_state='2')))
    def test_joiner_not_served(self): self.assertFalse(health.is_ready(dict(ready_status(),wsrep_local_state='1')))
    def test_wsrep_ready_off(self): self.assertFalse(health.is_ready(dict(ready_status(),wsrep_ready='OFF')))
    def test_disconnected(self): self.assertFalse(health.is_ready(dict(ready_status(),wsrep_connected='OFF')))
    def test_standalone_sql_health(self): self.assertTrue(health.is_ready({'sql_alive':True},'standalone'))
    def test_probe_failure_no_secret(self):
        with patch.object(health.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','secret123456')):
            report=health.collect()
        self.assertFalse(report['ready']); self.assertNotIn('secret123456',json.dumps(report))
    def test_dashboard_all_nodes_unreachable(self):
        with patch.object(dashboard,'fetch',side_effect=lambda n:dict(node=n,ready=False)):
            data=dashboard.cluster()
        self.assertEqual(data['ready_nodes'],0); self.assertFalse(data['single_uuid'])

class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.text=(ROOT/'.env.example').read_text().replace('GENERATE_WITH_LAB_INIT','a'*36)
        self.path=self.root/'.env'; self.path.write_text(self.text)
    def tearDown(self): self.tmp.cleanup()
    def bad(self,old,new):
        self.path.write_text(self.text.replace(old,new))
        with self.assertRaises((lab.LabError,ValueError)): lab.load_env(self.path)
    def test_default_configuration_valid(self): self.assertEqual(lab.load_env(self.path)['BIND_ADDRESS'],'127.0.0.1')
    def test_password_shell_syntax_rejected(self): self.bad('ROOT_PASSWORD='+'a'*36,'ROOT_PASSWORD=bad$(command)')
    def test_short_password_rejected(self): self.bad('ROOT_PASSWORD='+'a'*36,'ROOT_PASSWORD=short')
    def test_placeholder_rejected(self): self.bad('ROOT_PASSWORD='+'a'*36,'ROOT_PASSWORD=GENERATE_WITH_LAB_INIT')
    def test_project_shell_syntax_rejected(self): self.bad('LAB_PROJECT=mariadb-ha','LAB_PROJECT=../../other')
    def test_duplicate_ports_rejected(self): self.bad('WRITER_PORT=13306','WRITER_PORT=13301')
    def test_privileged_port_rejected(self): self.bad('WRITER_PORT=13306','WRITER_PORT=330')
    def test_invalid_bind_address(self): self.bad('BIND_ADDRESS=127.0.0.1','BIND_ADDRESS=not-an-ip')
    def test_invalid_memory(self): self.bad('GCACHE_SIZE=128M','GCACHE_SIZE=128M;bad')
    def test_initialize_random_and_private(self):
        (self.root/'.env.example').write_bytes((ROOT/'.env.example').read_bytes()); self.path.unlink()
        with patch.object(lab,'ROOT',self.root): lab.initialize()
        parsed=lab.load_env(self.path)
        self.assertEqual(len({parsed[k] for k in lab.PASSWORDS}),4)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
    def test_initialize_never_overwrites(self):
        with patch.object(lab,'ROOT',self.root): lab.initialize()
        self.assertEqual(self.path.read_text(),self.text)
    def test_streaming_checksum(self):
        self.assertEqual(lab.sha256_file(self.path),hashlib.sha256(self.path.read_bytes()).hexdigest())

class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template=(ROOT/'datasets/commerce-data-template.sql').read_text()
        cls.sql=''.join(seed.generate(cls.template,'tiny',50,256))
    def test_standard_count(self): self.assertEqual(sum(seed.expected_counts('standard').values()),2040068)
    def test_small_count(self): self.assertEqual(seed.expected_counts('small')['order_item'],60000)
    def test_large_count(self): self.assertEqual(seed.expected_counts('large')['shipment'],360000)
    def test_tiny_count(self): self.assertEqual(seed.expected_counts('tiny')['shipment'],180)
    def test_no_helper_table_dependency(self):
        for text in ('_numbers','_digits','ENGINE=MEMORY','foreign_key_checks = 0'):
            self.assertNotIn(text,self.sql)
    def test_autocommit_batches(self): self.assertIn('SET autocommit=1;',self.sql)
    def test_sequence_read_source(self): self.assertIn('(SELECT seq AS n FROM seq_0_to_49) AS numbers',self.sql)
    def test_fk_sizes_substituted(self):
        self.assertIn('((o.n * 17 + i.item_no * 997) % 100) + 1',self.sql)
        self.assertIn('((n * 17) % 100) + 1',self.sql)
        self.assertNotIn('(n % 50000)',self.sql)
    def test_price_threshold_not_replaced(self): self.assertIn('IF(x.net >= 50000, 0, 3000)',self.sql)
    def test_update_aggregations_bounded(self):
        self.assertEqual(self.sql.count('UPDATE orders o'),4)
        self.assertEqual(self.sql.count('FROM order_item WHERE order_id BETWEEN '),4)
    def test_inventory_bounded(self):
        self.assertIn('WHERE p.product_id BETWEEN 1 AND 50;',self.sql)
        self.assertIn('WHERE p.product_id BETWEEN 51 AND 100;',self.sql)
    def test_shipment_where_clause_preserved(self):
        self.assertIn("WHERE o.status NOT IN ('CANCELLED','REFUNDED','PENDING') AND o.order_id BETWEEN",self.sql)
    def test_payload_added(self): self.assertIn("'payload', RPAD(SHA2(CONCAT('row-',n),256), 256, 'x')",self.sql)
    def test_batch_boundaries(self): self.assertEqual(list(seed.blocks(101,50)),[(0,49),(50,99),(100,100)])
    def test_invalid_batch_zero(self):
        with self.assertRaises(ValueError): list(seed.generate(self.template,'tiny',0))
    def test_invalid_batch_large(self):
        with self.assertRaises(ValueError): list(seed.generate(self.template,'tiny',5001))
    def test_invalid_payload(self):
        with self.assertRaises(ValueError): list(seed.generate(self.template,'tiny',50,8193))
    def test_schema_exception_handler(self):
        text=(ROOT/'datasets/commerce-schema.sql').read_text()
        self.assertIn('EXIT HANDLER FOR SQLEXCEPTION',text)
        self.assertIn('RESIGNAL',text)
    def test_each_preset_sequence_coverage(self):
        for size,config in seed.SIZES.items():
            with self.subTest(size=size):
                sql=''.join(seed.generate(self.template,size,500))
                for table,key in (('customer','customer'),('product','product'),('orders','orders'),
                                  ('product_review','product_review'),('api_request_log','api_request_log')):
                    statements=re.findall(r'INSERT INTO '+table+r'\b.*?;',sql,re.S)
                    spans=[tuple(map(int,re.search(r'FROM seq_(\d+)_to_(\d+)',s).groups())) for s in statements]
                    self.assertEqual(spans[0][0],0); self.assertEqual(spans[-1][1],config[key]-1)
                    self.assertTrue(all(b[0]==a[1]+1 for a,b in zip(spans,spans[1:])))
                    self.assertTrue(all(hi-lo+1<=500 for lo,hi in spans))

class SafetyContractTests(unittest.TestCase):
    def test_offline_refuses_running(self):
        instance=object.__new__(lab.Lab)
        with patch.object(instance,'state',return_value='running'),patch.object(instance,'comp') as comp:
            with self.assertRaises(lab.LabError): instance.offline('galera1','wipe')
            comp.assert_not_called()
    def test_offline_refuses_paused(self):
        instance=object.__new__(lab.Lab)
        with patch.object(instance,'state',return_value='paused'),patch.object(instance,'comp') as comp:
            with self.assertRaises(lab.LabError): instance.offline('galera1','inspect')
            comp.assert_not_called()
    def test_no_bootstrap_if_active_but_unready(self):
        instance=object.__new__(lab.Lab)
        with patch.object(instance,'build'),patch.object(instance,'state',return_value='running'),\
             patch.object(instance,'health',return_value={'ready':False}),patch.object(instance,'offline') as off:
            with self.assertRaises(lab.LabError): instance.up()
            off.assert_not_called()
    def test_rebuild_refuses_unhealthy_survivor(self):
        instance=object.__new__(lab.Lab)
        with patch.object(instance,'health',return_value={'ready':False}),patch.object(instance,'offline') as off:
            with self.assertRaises(lab.LabError): instance.rebuild('galera3')
            off.assert_not_called()
    def test_bootstrap_not_persistent_in_compose(self):
        text=(ROOT/'compose.yaml').read_text()
        self.assertNotIn('wsrep-new-cluster',text)
    def test_entrypoint_consumes_token_before_start(self):
        text=(ROOT/'images/node/entrypoint.sh').read_text()
        self.assertLess(text.index('rm -f /var/lib/labctl/bootstrap-once'),text.index('exec /usr/local/bin/gosu'))
    def test_init_wsrep_disabled(self):
        text=(ROOT/'images/node/entrypoint.sh').read_text()
        self.assertIn('mariadbd --wsrep-on=OFF --skip-networking',text)
    def test_no_global_prune_implementation(self):
        text=(ROOT/'scripts/lab.py').read_text()
        self.assertNotIn("'prune'",text)
    def test_cli_help(self):
        result=subprocess.run([sys.executable,str(ROOT/'scripts/lab.py'),'--help'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        for name in ('recover','rebuild','quorum-demo','verify','restore'): self.assertIn(name,result.stdout)
    def test_shell_syntax(self):
        for path in [ROOT/'lab.sh',*ROOT.glob('images/node/*.sh')]:
            with self.subTest(path=path.name):
                result=subprocess.run(['bash','-n',str(path)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)

try:
    import yaml
except ImportError: yaml=None

@unittest.skipUnless(yaml,'PyYAML optional: skips Compose structural checks only')
class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.cfg=yaml.safe_load((ROOT/'compose.yaml').read_text())
    def test_three_separate_data_volumes(self):
        volumes=[self.cfg['services'][n]['volumes'][0] for n in lab.NODES]
        self.assertEqual(len(set(volumes)),3)
    def test_unique_server_ids(self):
        ids=[self.cfg['services'][n]['environment']['SERVER_ID'] for n in lab.NODES]
        self.assertEqual(len(set(ids)),3)
    def test_fail_closed_restart_policy(self):
        for node in lab.NODES: self.assertEqual(self.cfg['services'][node]['restart'],'no')
    def test_restore_network_isolated(self):
        self.assertTrue(set(self.cfg['services']['restore']['networks']).isdisjoint(
            self.cfg['services']['galera1']['networks']))
    def test_restore_wsrep_off_mode(self): self.assertEqual(self.cfg['services']['restore']['environment']['LAB_MODE'],'standalone')
    def test_loopback_ports(self):
        for service in self.cfg['services'].values():
            for port in service.get('ports',[]): self.assertTrue(port.startswith('${BIND_ADDRESS:-127.0.0.1}'))
    def test_no_bind_mount_or_container_socket(self):
        for service in self.cfg['services'].values():
            for volume in service.get('volumes',[]):
                self.assertNotIn(volume[0],('./')); self.assertNotIn('sock',volume)
    def test_nonprivileged_network(self):
        for service in self.cfg['services'].values():
            self.assertFalse(service.get('privileged')); self.assertNotEqual(service.get('network_mode'),'host')
    def test_dashboard_no_db_password(self):
        env=self.cfg['services']['dashboard'].get('environment',{})
        self.assertNotIn('MARIADB_ROOT_PASSWORD',env)

if __name__=='__main__': unittest.main(verbosity=2)
