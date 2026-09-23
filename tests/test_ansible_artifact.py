from __future__ import annotations
import importlib.util
from pathlib import Path
import tarfile
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'ansible/package_artifact.py'
SPEC = importlib.util.spec_from_file_location('package_artifact', SCRIPT)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class PackageArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='db-lab-package-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'
        (self.root / 'scripts').mkdir(parents=True)
        (self.root / 'mvp-lab').mkdir()
        (self.root / 'all.sh').write_text('#!/bin/sh\n')
        (self.root / 'scripts/control.py').write_text('')
        (self.root / 'mvp-lab/lab.sh').write_text('#!/bin/sh\n')
        (self.root / '.env').write_text('PASSWORD=private\n')
        (self.root / '.env.example').write_text('PASSWORD=change-me\n')
        (self.root / 'reports').mkdir()
        (self.root / 'reports/private.json').write_text('private report')
        (self.root / '.state').mkdir()
        (self.root / '.state/active.json').write_text('runtime state')
        (self.root / 'elasticsearch/certs').mkdir(parents=True)
        (self.root / 'elasticsearch/certs/node.key').write_text('private key')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.output = Path(self.tmp.name) / 'artifacts'

    def test_archive_is_deterministic_and_excludes_runtime_secrets(self):
        first = package.create(self.root, self.output)
        second = package.create(self.root, self.output)
        self.assertEqual(first['sha256'], second['sha256'])
        with tarfile.open(first['artifact'], 'r:gz') as archive:
            names = set(archive.getnames())
            self.assertIn('.env.example', names)
            self.assertIn('all.sh', names)
            self.assertIn('scripts/control.py', names)
            self.assertFalse(any(name == '.env' or name.endswith('/.env') for name in names))
            self.assertFalse(any(name.startswith(('.state/', 'reports/', 'elasticsearch/certs/')) for name in names))
            self.assertTrue(all(not name.startswith('/') and '..' not in Path(name).parts for name in names))

    def test_symlinked_source_file_is_refused(self):
        (self.root / 'scripts/private').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            package.create(self.root, self.output)


if __name__ == '__main__':
    unittest.main()
