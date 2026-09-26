"""Offline contracts for ES9 node-fault drill planning and safety guards."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / 'scripts'))
import drills


class Es9DrillCliTests(unittest.TestCase):
    def test_list_and_plans_are_read_only_and_describe_fixed_targets(self):
        listing = io.StringIO()
        with contextlib.redirect_stdout(listing):
            self.assertEqual(drills.main(['list']), 0)
        self.assertIn('node-outage', listing.getvalue())
        plans = []
        for scenario in ('node-outage', 'quorum-loss'):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(drills.main(['plan', scenario]), 0)
            plans.append(json.loads(output.getvalue()))
        self.assertEqual(plans[0]['planned_targets'], ['auto'])
        self.assertEqual(plans[1]['planned_targets'], ['es02', 'es03', 'es04'])
        self.assertTrue(all(plan['requires_yes'] and plan['automatic_recovery'] for plan in plans))

    def test_node_target_cannot_stop_the_host_published_node(self):
        with self.assertRaisesRegex(RuntimeError, '--node'):
            drills.main(['run', 'node-outage', '--node', 'es01', '--yes'])

    def test_all_live_changes_require_explicit_consent(self):
        for command in (['run', 'node-outage'], ['recover']):
            with self.subTest(command=command), self.assertRaisesRegex(RuntimeError, '--yes'):
                drills.main(command)

    def test_private_report_is_mode_600_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'reports' / 'report.json'
            drills.write_private_json(path, {'status': 'PASS'})
            self.assertEqual(json.loads(path.read_text()), {'status': 'PASS'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_symlink_report_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'target'
            target.mkdir()
            link = root / 'link'
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'symlinked'):
                drills.write_private_json(link / 'report.json', {'status': 'PASS'})

    def test_nan_and_unbounded_timeouts_are_rejected(self):
        for args in (['list', '--hold', 'nan'], ['list', '--timeout', 'inf'],
                     ['list', '--hold', '31'], ['list', '--timeout', '601']):
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                drills.main(args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
