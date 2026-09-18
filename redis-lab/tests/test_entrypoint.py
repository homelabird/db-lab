"""Execute the shell entrypoint in a temporary filesystem, with Redis exec stubbed.
This tests rendering and preservation; it is NOT a real Redis/Podman startup test.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fake = self.root / 'record-exec.sh'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_FILE"\n')
        fake.chmod(0o755)
        script = (ROOT / 'container/entrypoint.sh').read_text()
        script = script.replace('/data', str(self.root / 'data'))
        script = script.replace('/opt/lab/config', str(ROOT / 'config'))
        script = script.replace('/usr/local/bin/docker-entrypoint.sh', str(fake))
        self.script = self.root / 'entrypoint.sh'; self.script.write_text(script)
        self.env = dict(os.environ, NODE_NAME='redis-2', NODE_IP='10.89.77.12',
            REDIS_PASSWORD='TestRedisPassword123', SENTINEL_PASSWORD='TestSentinelPassword123',
            MASTER_NAME='mymaster', PRIMARY_IP='10.89.77.11', LAB_MODE='redis',
            INITIAL_ROLE='replica', REDIS_MAXMEMORY='192mb', DOWN_AFTER_MS='5000',
            FAILOVER_TIMEOUT_MS='30000', ARGV_FILE=str(self.root / 'argv.txt'))
    def run_entry(self):
        return subprocess.run(['sh', str(self.script)], env=self.env, text=True, capture_output=True)
    def test_replica_config_initializes(self):
        result = self.run_entry(); self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.root / 'data/state/redis.conf').read_text()
        self.assertIn('replicaof 10.89.77.11 6379', text)
        self.assertIn('requirepass TestRedisPassword123', text)
        self.assertNotIn('@@', text)
    def test_primary_has_no_replicaof(self):
        self.env.update(NODE_NAME='redis-1', NODE_IP='10.89.77.11', INITIAL_ROLE='master')
        self.assertEqual(self.run_entry().returncode, 0)
        self.assertNotIn('replicaof ', (self.root / 'data/state/redis.conf').read_text())
    def test_sentinel_has_separate_auth_and_writable_config(self):
        self.env.update(LAB_MODE='sentinel', NODE_NAME='sentinel-1', NODE_IP='10.89.77.21')
        result = self.run_entry(); self.assertEqual(result.returncode, 0, result.stderr)
        path = self.root / 'data/state/sentinel.conf'; text = path.read_text()
        self.assertIn('requirepass TestSentinelPassword123', text)
        self.assertIn('sentinel auth-pass mymaster TestRedisPassword123', text)
        self.assertIn('sentinel monitor mymaster 10.89.77.11 6379 2', text)
        self.assertIn('--sentinel', (self.root / 'argv.txt').read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
    def test_rewritten_role_survives_restart(self):
        self.assertEqual(self.run_entry().returncode, 0)
        path = self.root / 'data/state/redis.conf'
        rewritten = path.read_text().replace('replicaof 10.89.77.11 6379', 'replicaof 10.89.77.13 6379')
        path.write_text(rewritten)
        self.assertEqual(self.run_entry().returncode, 0)
        self.assertEqual(path.read_text(), rewritten)
    def test_sentinel_epoch_survives_restart(self):
        self.env.update(LAB_MODE='sentinel', NODE_NAME='sentinel-1', NODE_IP='10.89.77.21')
        self.assertEqual(self.run_entry().returncode, 0)
        path = self.root / 'data/state/sentinel.conf'
        text = path.read_text() + '\nsentinel current-epoch 9\n'; path.write_text(text)
        self.assertEqual(self.run_entry().returncode, 0)
        self.assertEqual(path.read_text(), text)
    def test_changed_password_fails_without_overwriting(self):
        self.assertEqual(self.run_entry().returncode, 0)
        path = self.root / 'data/state/redis.conf'; original = path.read_text()
        self.env['REDIS_PASSWORD'] = 'DifferentPassword123'
        self.assertEqual(self.run_entry().returncode, 78)
        self.assertEqual(path.read_text(), original)
    def test_unsafe_password_refused(self):
        self.env['REDIS_PASSWORD'] = 'Unsafe|Password123'
        self.assertEqual(self.run_entry().returncode, 64)

if __name__ == '__main__': unittest.main(verbosity=2)
