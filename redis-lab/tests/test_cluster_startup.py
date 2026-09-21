"""Startup-path regressions; host contracts, NOT a live Redis/Podman test."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'client'), str(ROOT / 'tests')]
import manage
import test_project


def completed(text='', rc=0):
    return subprocess.CompletedProcess([], rc, text, '')


class ClusterStartupTests(unittest.TestCase):
    def setUp(self):
        self.env = manage.parse_env(ROOT / '.env.example')
        self.env['DEPLOYMENT_MODE'] = 'cluster'
        self.lab = manage.Lab(self.env)

    def test_cluster_owned_volume_passes_allowlist(self):
        data = [{'Labels': {manage.LABEL: self.lab.name}}]
        with patch.object(self.lab, 'run', return_value=completed(json.dumps(data))):
            self.assertEqual(self.lab.volume_owned('cluster-1-data'), data[0])

    def test_cluster_does_not_operate_sentinel_volume(self):
        with patch.object(self.lab, 'run', return_value=completed('', 1)) as run:
            with self.assertRaises(ValueError):
                self.lab.volume_owned('redis-1-data')
            run.assert_not_called()

    def test_cluster_volume_still_requires_owner_label(self):
        with patch.object(self.lab, 'run', return_value=completed('[{"Labels":{}}]')):
            with self.assertRaisesRegex(RuntimeError, 'not owned'):
                self.lab.volume_owned('cluster-1-data')

    def test_cluster_build_contexts_contain_the_containerfile(self):
        with tempfile.TemporaryDirectory(prefix='redis cluster ') as directory:
            base = Path(directory)
            (base / 'Containerfile').write_text('FROM scratch\n')
            with patch.object(manage, 'ROOT', base):
                generated = self.lab.cluster_compose_path()
            contexts = [line.split(':', 1)[1].strip() for line in generated.read_text().splitlines()
                        if line.strip().startswith('context:')]
            self.assertEqual(len(contexts), 2)
            for raw in contexts:
                value = json.loads(raw) if raw.startswith('"') else raw
                path = (generated.parent / value).resolve()
                self.assertTrue((path / 'Containerfile').is_file(), str(path))

    def assert_cluster_create_addresses(self, count, base):
        self.env.update(CLUSTER_NODE_COUNT=str(count), CLUSTER_BASE_IP=base,
                        CLUSTER_REPLICAS='2' if count == 9 else '1')
        self.lab = manage.Lab(self.env)
        empty = {n: {'cluster_known_nodes': '1', 'cluster_slots_assigned': '0', 'dbsize': 0}
                 for n in self.lab.cluster_nodes()}
        with patch.object(self.lab, 'cluster_inventory', return_value=empty, create=True), \
             patch.object(self.lab, 'inspect'), patch.object(self.lab, 'check_env_state'), \
             patch.object(self.lab, 'execute', return_value=completed()) as execute, \
             patch.object(self.lab, 'client', return_value=completed()), \
             patch.object(self.lab, 'cli', return_value=completed(
                 'cluster_state:ok\ncluster_slots_assigned:16384\n')):
            self.lab.cluster_init()
        command = execute.call_args.args[1][-1]
        for address in self.lab.cluster_endpoints():
            self.assertIn(address, command)

    def test_nine_nodes_do_not_require_legacy_seventh_ip(self):
        self.assert_cluster_create_addresses(9, '10.89.77.81')

    def test_custom_base_ip_is_used_for_cluster_creation(self):
        self.assert_cluster_create_addresses(6, '10.89.77.101')

    def test_replica_layout_requires_at_least_three_masters(self):
        for count, replicas in [(4, 1), (6, 2), (8, 3)]:
            with self.subTest(count=count, replicas=replicas):
                self.env.update(CLUSTER_NODE_COUNT=str(count), CLUSTER_REPLICAS=str(replicas))
                with self.assertRaises(ValueError):
                    manage.validate_env(self.env)

    def test_supported_replica_layouts_remain_valid(self):
        for count, replicas in [(3, 0), (6, 1), (9, 2), (8, 1)]:
            with self.subTest(count=count, replicas=replicas):
                self.env.update(CLUSTER_NODE_COUNT=str(count), CLUSTER_REPLICAS=str(replicas))
                manage.validate_env(self.env)

    def test_rejected_topology_change_preserves_env_bytes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manage, 'ROOT', Path(directory)):
            base = Path(directory)
            (base / '.lab').mkdir()
            original = ''.join(f'{k}={v}\n' for k, v in self.env.items())
            (base / '.env').write_text(original)
            self.lab.check_env_state(save=True)
            with patch.object(manage, 'load_env', return_value=dict(self.env)), \
                 patch.object(manage.Lab, 'up', side_effect=RuntimeError('preflight blocked')), \
                 patch.object(sys, 'argv', ['manage.py', 'up', '--cluster-nodes', '8']):
                with self.assertRaises(RuntimeError):
                    manage.main()
            self.assertEqual((base / '.env').read_text(), original)


class ClusterClientTests(unittest.TestCase):
    def setUp(self):
        self.client = test_project.load_client_with_stub()
        self.client.Retry = Mock(return_value=object())
        self.client.DEPLOYMENT_MODE = 'cluster'
        self.client.CLUSTER_NODE_COUNT = 3
        self.client.CLUSTER_REPLICAS = 0
        self.client.CLUSTER_NODES = [('10.89.77.' + str(i), 6379) for i in range(51, 54)]

    def good_state(self):
        names = [f'redis-cluster-{i}' for i in range(1, 4)]
        return {'redis': {n: {'reachable': True} for n in names},
                'cluster': {n: {'cluster_state': 'ok', 'cluster_slots_assigned': '16384',
                    'cluster_slots_ok': '16384', 'cluster_slots_fail': '0',
                    'cluster_slots_pfail': '0', 'cluster_known_nodes': '3'} for n in names}}

    def test_cluster_client_passes_password_only_once(self):
        cluster = types.ModuleType('redis.cluster')
        cluster.ClusterNode = Mock(side_effect=lambda host, port: (host, port))
        cluster.RedisCluster = Mock()
        with patch.dict(sys.modules, {'redis.cluster': cluster}):
            self.client.master_client()
        cluster.RedisCluster.assert_called_once()
        self.assertIn('password', cluster.RedisCluster.call_args.kwargs)

    def test_snapshot_uses_driver_cluster_info_response_parser(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.info.return_value = {'role': 'master'}
        connection.dbsize.return_value = 0
        good = self.good_state()['cluster']['redis-cluster-1']
        # Real redis-py selects its callback using the complete command name.
        connection.execute_command.side_effect = lambda *a: good if a == ('CLUSTER INFO',) else 'cluster_state:ok\r\n'
        connection.cluster.side_effect = lambda arg: good if arg == 'info' else None
        with patch.object(self.client, 'direct', return_value=connection):
            state = self.client.snapshot()
        self.assertEqual(state['cluster']['redis-cluster-1'], good)

    def test_missing_cluster_info_cannot_pass_health(self):
        state = self.good_state()
        state['cluster']['redis-cluster-3'] = {}
        self.assertTrue(self.client.topology_errors(state))

    def test_wrong_cluster_membership_cannot_pass_health(self):
        state = self.good_state()
        state['cluster']['redis-cluster-2']['cluster_known_nodes'] = '6'
        self.assertTrue(self.client.topology_errors(state))

    def test_healthy_complete_cluster_passes(self):
        self.assertEqual(self.client.topology_errors(self.good_state()), [])

    def test_wait_uses_a_dedicated_key_owner_connection_with_zero_replicas(self):
        self.assert_readiness_wait(0)

    def test_wait_uses_configured_two_replicas(self):
        self.assert_readiness_wait(2)

    def assert_readiness_wait(self, replicas):
        self.client.CLUSTER_REPLICAS = replicas
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.get.return_value = 'ok'
        connection.wait.return_value = replicas
        direct_owner = Mock()
        direct_owner.client.return_value = connection
        cluster = MagicMock(spec=['__enter__', '__exit__', 'get_node_from_key', 'get_redis_connection'])
        cluster.__enter__.return_value = cluster
        cluster.get_redis_connection.return_value = direct_owner
        with patch.object(self.client, 'master_client', return_value=cluster), \
             patch.object(self.client, 'snapshot', return_value=self.good_state()), \
             patch.object(self.client, 'print_status'), \
             patch.object(self.client.time, 'monotonic', side_effect=[0, 0, 2]), \
             patch.object(self.client.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            self.client.wait_ready(1)
        cluster.get_node_from_key.assert_called_once()
        connection.wait.assert_called_once_with(replicas, 1000)


if __name__ == '__main__':
    unittest.main()
