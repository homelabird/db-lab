"""Exercise Redis cluster lifecycle decisions with explicit CLI doubles."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import manage


def result(text):
    return subprocess.CompletedProcess([], 0, text, '')


class ClusterFlowTests(unittest.TestCase):
    def setUp(self):
        env = manage.parse_env(ROOT / '.env.example')
        env.update(DEPLOYMENT_MODE='cluster', CLUSTER_NODE_COUNT='3', CLUSTER_REPLICAS='0')
        self.lab = manage.Lab(env)
        self.fresh = {name: {'cluster_known_nodes': '1', 'cluster_slots_assigned': '0',
                            'cluster_slots_ok': '0', 'cluster_slots_fail': '0',
                            'cluster_slots_pfail': '0', 'cluster_state': 'fail', 'dbsize': 0}
                      for name in self.lab.cluster_nodes()}

    def invoke(self, inventory):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(self.lab, 'check_env_state'))
        stack.enter_context(patch.object(self.lab, 'inspect'))
        stack.enter_context(patch.object(self.lab, 'cluster_inventory', return_value=inventory))
        self.execute = stack.enter_context(patch.object(self.lab, 'execute'))
        self.wait = stack.enter_context(patch.object(self.lab, 'client'))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.lab.cluster_init()

    def test_empty_cluster_is_created_then_verified(self):
        self.invoke(self.fresh)
        self.execute.assert_called_once()
        self.assertIn('--cluster-replicas 0', self.execute.call_args.args[1][-1])
        self.wait.assert_called_once_with('wait', '--timeout', str(self.lab.readiness_budget()))

    def test_existing_healthy_cluster_is_only_verified(self):
        for info in self.fresh.values():
            info.update(cluster_known_nodes='3', cluster_slots_assigned='16384',
                        cluster_slots_ok='16384', cluster_state='ok', dbsize=10)
        self.invoke(self.fresh)
        self.execute.assert_not_called()
        self.wait.assert_called_once()

    def test_partially_initialized_cluster_is_not_recreated(self):
        self.fresh['redis-cluster-1']['cluster_slots_assigned'] = '5461'
        with self.assertRaisesRegex(RuntimeError, 'partially initialized'):
            self.invoke(self.fresh)
        self.execute.assert_not_called()

    def test_nonempty_unassigned_nodes_are_preserved(self):
        self.fresh['redis-cluster-1']['dbsize'] = 1
        with self.assertRaises(RuntimeError):
            self.invoke(self.fresh)
        self.execute.assert_not_called()

    def test_cluster_inventory_parses_authenticated_replies(self):
        raw = '\n'.join(f'{k}:{v}' for k, v in self.fresh['redis-cluster-1'].items() if k != 'dbsize')
        def cli(node, args, **kwargs):
            return result('PONG\n' if args == ['PING'] else '0\n' if args == ['DBSIZE'] else raw)
        with patch.object(self.lab, 'cli', side_effect=cli):
            self.assertEqual(self.lab.cluster_inventory(timeout=0), self.fresh)

    def test_inventory_timeout_does_not_imply_empty_nodes(self):
        with patch.object(self.lab, 'cli', return_value=result('NOAUTH')), \
             patch.object(manage.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'No cluster creation'):
                self.lab.cluster_inventory(timeout=0)

    def test_bus_port_cannot_collide_with_client_port(self):
        self.lab.env['CLUSTER_BUS_PORT'] = '6379'
        with self.assertRaises(ValueError):
            manage.validate_env(self.lab.env)


if __name__ == '__main__':
    unittest.main()
