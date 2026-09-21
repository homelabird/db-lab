"""Actual controller filesystem tests; no container engine is invoked."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('kafka_topology_state', ROOT / 'scripts/topology-state.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TopologyStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kafka state ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.env').write_text('# preserve\nNODES=3\nCUSTOM_VALUE=preserve-me\n')
        (self.root / '.env').chmod(0o600)

    def run_state(self, action='save', nodes=1, mode='zk'):
        module.run(self.root, action, 'kzk-lab', mode, nodes)

    def test_check_does_not_persist_a_topology(self):
        old = (self.root / '.env').read_bytes()
        self.run_state('check')
        self.assertEqual((self.root / '.env').read_bytes(), old)
        self.assertFalse((self.root / '.state/active-topology.json').exists())

    def test_save_preserves_other_env_settings_and_mode(self):
        self.run_state()
        self.assertEqual((self.root / '.env').read_text(), '# preserve\nNODES=1\nCUSTOM_VALUE=preserve-me\nKAFKA_MODE=zk\nLAB_NAME=kzk-lab\n')
        self.assertEqual((self.root / '.env').stat().st_mode & 0o777, 0o600)

    def test_identical_save_is_idempotent(self):
        self.run_state()
        before = (self.root / '.state/active-topology.json').read_bytes()
        self.run_state()
        self.assertEqual((self.root / '.state/active-topology.json').read_bytes(), before)

    def test_different_count_does_not_touch_pinned_config(self):
        self.run_state()
        old = (self.root / '.env').read_bytes()
        with self.assertRaisesRegex(ValueError, 'pinned'):
            self.run_state(nodes=5)
        self.assertEqual((self.root / '.env').read_bytes(), old)

    def test_mode_change_is_not_an_implicit_migration(self):
        self.run_state()
        with self.assertRaisesRegex(ValueError, 'pinned'):
            self.run_state(mode='kraft')

    def test_symlink_env_is_refused(self):
        (self.root / '.env').rename(self.root / 'real-env')
        (self.root / '.env').symlink_to(self.root / 'real-env')
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            self.run_state()
        self.assertIn('NODES=3', (self.root / 'real-env').read_text())

    def test_duplicate_count_refused_before_pin_write(self):
        with (self.root / '.env').open('a') as stream:
            stream.write('NODES=7\n')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.run_state()
        self.assertFalse((self.root / '.state/active-topology.json').exists())

    def test_corrupt_pin_is_not_repaired_or_overwritten(self):
        self.run_state()
        pin = self.root / '.state/active-topology.json'
        pin.write_text('not-json')
        with self.assertRaises(ValueError):
            self.run_state()
        self.assertEqual(pin.read_text(), 'not-json')

    def test_invalid_zookeeper_quorum_count_refused(self):
        with self.assertRaises(ValueError):
            self.run_state(nodes=2)


if __name__ == '__main__':
    unittest.main()
