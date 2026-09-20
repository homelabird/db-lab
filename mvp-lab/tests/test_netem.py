"""No tc modifications in these tests: fake qdisc and Docker contracts only."""
import contextlib
import copy
import io
import json
import tempfile
from pathlib import Path
import threading
from unittest import TestCase
from unittest.mock import Mock, patch
from mvp_app import netem_agent as a
from tools import netem
from tools.simulation import Runner, Plan, Journal

EMPTY = [{'kind': 'noqueue', 'handle': '0:', 'root': True}]
MINE = [{'kind': 'netem', 'handle': a.HANDLE, 'root': True, 'drops': 3, 'packets': 40}]
RUN = 'sim-0123456789ab'

class AgentTests(TestCase):
    def test_default_root_is_accepted_and_restored(self):
        with patch.object(a, 'state', side_effect=[EMPTY, MINE, MINE, EMPTY]), patch.object(a, 'tc') as tc, contextlib.redirect_stdout(io.StringIO()):
            stop = Mock(); result = a.run('delay', 4, stop)
        stop.wait.assert_called_once_with(4)
        self.assertIn('180ms', tc.call_args_list[0].args)
        self.assertEqual(tc.call_args_list[-1].args, ('qdisc', 'del', 'dev', 'eth0', 'root', 'handle', a.HANDLE))
        self.assertEqual(result['statistics'], MINE)

    def test_real_packet_loss_command_not_application_exception(self):
        with patch.object(a, 'state', side_effect=[EMPTY, MINE, MINE, EMPTY]), patch.object(a, 'tc') as tc, contextlib.redirect_stdout(io.StringIO()):
            a.run('loss', 3, Mock())
        self.assertIn('10%', tc.call_args_list[0].args)
        self.assertIn('random', tc.call_args_list[0].args)

    def test_foreign_qdisc_never_replaced(self):
        with patch.object(a, 'state', return_value=[{'kind': 'fq_codel', 'root': True}]), patch.object(a, 'tc') as tc:
            with self.assertRaises(RuntimeError): a.run('loss', 3, Mock())
        tc.assert_not_called()

    def test_wrong_handle_never_deleted(self):
        with patch.object(a, 'state', return_value=[{'kind': 'netem', 'root': True, 'handle': '123:'}]), patch.object(a, 'tc') as tc:
            with self.assertRaises(RuntimeError): a.clear()
        tc.assert_not_called()

    def test_timer_interruption_still_removes_qdisc(self):
        stop = Mock(); stop.wait.side_effect = KeyboardInterrupt()
        with patch.object(a, 'state', side_effect=[EMPTY, MINE, MINE, EMPTY]), patch.object(a, 'tc') as tc, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt): a.run('loss', 3, stop)
        self.assertEqual(tc.call_args.args[1], 'del')

    def test_cleanup_on_already_expired_helper_is_idempotent(self):
        with patch.object(a, 'state', return_value=EMPTY), patch.object(a, 'tc') as tc:
            self.assertEqual(a.clear(), [])
        tc.assert_not_called()

    def test_invalid_duration_refused_before_tc(self):
        for duration in (0, 31, 10000):
            with patch.object(a, 'tc') as tc, self.assertRaises(ValueError): a.run('delay', duration, Mock())
            tc.assert_not_called()

class NetworkContractTests(TestCase):
    def record(self):
        return {'project': 'db-lab-mvp', 'run_id': RUN, 'original': {'id': 'a'*64}, 'helper_image': 'sha256:'+'b'*64,
                'helper_name': 'db-lab-mvp-netem-'+RUN}

    def test_helper_has_no_host_network_or_mount_and_only_net_admin(self):
        lab = Mock(); opts = netem.options(lab, self.record())
        self.assertEqual(opts[opts.index('--network')+1], 'container:'+'a'*64)
        self.assertNotIn('--privileged', opts); self.assertNotIn('--volume', opts)
        self.assertNotIn('--pid', opts)
        self.assertEqual(opts[opts.index('--cap-add')+1], 'NET_ADMIN')
        self.assertEqual(opts[opts.index('--memory')+1], opts[opts.index('--memory-swap')+1])

    def test_host_network_target_refused(self):
        lab = Mock(); lab.docker.return_value = json.dumps([{'HostConfig': {'NetworkMode': 'host'}}])
        with self.assertRaises(RuntimeError): netem.check_target(lab, {'id': 'a'*64})

    def test_multiple_networks_refused(self):
        lab = Mock(); lab.docker.return_value = json.dumps([{'HostConfig': {'NetworkMode': 'p_default'}, 'NetworkSettings': {'Networks': {'a':{}, 'b':{}}}}])
        with self.assertRaises(RuntimeError): netem.check_target(lab, {'id': 'a'*64})

    def test_helper_name_tamper_rejected_before_lookup(self):
        lab = Mock(); lab.c.config = {'MVP_PROJECT': 'db-lab-mvp'}
        record = self.record(); record['helper_name'] = 'other'
        with self.assertRaises(RuntimeError): netem.helper(lab, record)
        lab.docker.assert_not_called()

    def test_missing_prepared_image_fails_with_next_command(self):
        lab = Mock(); lab.c.config = {'MVP_PROJECT': 'db-lab-mvp'}; lab.docker.side_effect = RuntimeError()
        with self.assertRaisesRegex(RuntimeError, 'drills prepare'): netem.image(lab)

    def test_no_helper_record_means_no_cleanup_docker(self):
        lab = Mock(); self.assertEqual(netem.restore(lab, {})['helper_created'], False)
        lab.docker.assert_not_called()

    def runner(self, scenario):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        journal = Journal(Path(temp.name)/'run'); self.addCleanup(journal.close)
        return Runner(Plan(scenario=scenario), Mock(), Mock(), journal, RUN, 'db-lab-mvp', evidence_kind='TEST-DOUBLE')

    def test_packet_loss_needs_positive_kernel_drop_counter(self):
        r = self.runner('db-network-loss')
        r.journal.rows['timeline'].append({'stage':'fault_restore', 'detail': {'netem': {'statistics': [{'drops': 0}]}}})
        self.assertFalse(r.effect_observed())
        r.journal.rows['timeline'][0]['detail']['netem']['statistics'][0]['drops'] = 2
        self.assertTrue(r.effect_observed())

    def test_latency_needs_traffic_and_observed_effect_not_just_tc_add(self):
        r = self.runner('db-network-delay')
        r.journal.rows['requests'] += [{'scope':'workload', 'phase':'baseline','latency_ms':10}, {'scope':'workload','phase':'fault','latency_ms':220}]
        self.assertFalse(r.effect_observed())
        r.journal.rows['timeline'].append({'stage':'fault_restore', 'detail': {'netem': {'statistics': [{'packets':50}]}}})
        self.assertTrue(r.effect_observed())

    def test_small_latency_change_is_inconclusive(self):
        r = self.runner('db-network-delay')
        r.journal.rows['requests'] += [{'scope':'workload','phase':'baseline','latency_ms':100}, {'scope':'workload','phase':'fault','latency_ms':101}]
        r.journal.rows['timeline'].append({'stage':'fault_restore', 'detail': {'netem': {'statistics': [{'packets':50}]}}})
        self.assertFalse(r.effect_observed())
