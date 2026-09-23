"""Real SSH transport to an isolated, short-lived local sshd; no DB containers."""
from __future__ import annotations
import getpass
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = shutil.which('ansible-playbook')
SSHD = shutil.which('sshd')


class SshTargetTests(unittest.TestCase):
    def test_real_ssh_module_transfer_and_native_execution(self):
        if not PLAYBOOK or not SSHD:
            self.fail('BLOCKED: install ansible-core and openssh-server to run scripts/test-ansible-ssh.sh')
        with tempfile.TemporaryDirectory(prefix='db-lab-ansible-ssh-') as temp:
            base = Path(temp)
            key, host_key = base / 'client_key', base / 'host_key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(host_key)], check=True)
            authorized = base / 'authorized_keys'
            authorized.write_bytes(Path(str(key) + '.pub').read_bytes())
            authorized.chmod(0o600)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]

            project = base / 'project'
            (project / 'scripts').mkdir(parents=True)
            (project / 'mvp-lab').mkdir()
            (project / 'scripts/control.py').write_text('')
            (project / 'mvp-lab/lab.sh').write_text('#!/bin/bash\n')
            controller = project / 'all.sh'
            controller.write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$*" >> called.txt\n')
            controller.chmod(0o700)
            (project / '.env').write_text('PASSWORD=fixture-secret\n')
            (project / '.env.example').write_text('PASSWORD=change-me\n')
            (project / 'reports').mkdir()
            (project / 'reports/private.json').write_text('fixture report')
            subprocess.run(['git', 'init', '-q', str(project)], check=True)

            known_hosts = base / 'known_hosts'
            host_pub = Path(str(host_key) + '.pub').read_text().split()
            known_hosts.write_text(f'[127.0.0.1]:{port} {host_pub[0]} {host_pub[1]}\n')
            config = base / 'sshd_config'
            config.write_text('\n'.join((
                f'Port {port}', 'ListenAddress 127.0.0.1', f'HostKey {host_key}',
                f'PidFile {base / "sshd.pid"}', 'PasswordAuthentication no',
                'KbdInteractiveAuthentication no', 'PubkeyAuthentication yes',
                f'AuthorizedKeysFile {authorized}', 'StrictModes no', 'UsePAM no',
                f'AllowUsers {getpass.getuser()}', 'AllowTcpForwarding no',
                'X11Forwarding no', 'Subsystem sftp internal-sftp', 'LogLevel VERBOSE', '',
            )))
            log = (base / 'sshd.log').open('w+')
            server = subprocess.Popen([SSHD, '-D', '-e', '-f', str(config)],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            self.addCleanup(self._stop_server, server, log)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    log.flush()
                    self.fail('isolated sshd failed: ' + log.read())
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                self.fail('isolated sshd did not open its loopback port')

            inventory = base / 'inventory.json'
            inventory.write_text(json.dumps({'all': {'children': {'db_lab': {'hosts': {
                'disposable_ssh': {
                    'ansible_host': '127.0.0.1', 'ansible_user': getpass.getuser(),
                    'ansible_port': port, 'ansible_ssh_private_key_file': str(key),
                    'ansible_python_interpreter': sys.executable,
                    'ansible_ssh_common_args': f'-o UserKnownHostsFile={known_hosts} -o StrictHostKeyChecking=yes',
                    'db_lab_project_root': str(project),
                }
            }}}}}))
            remote_tmp = base / 'remote-tmp'
            remote_tmp.mkdir(mode=0o700)
            env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / 'ansible/ansible.cfg'),
                       ANSIBLE_LIBRARY=str(ROOT / 'ansible/library'),
                       ANSIBLE_MODULE_UTILS=str(ROOT / 'ansible/module_utils'),
                       ANSIBLE_LOCAL_TEMP=str(base / 'ansible-tmp'),
                       ANSIBLE_REMOTE_TEMP=str(remote_tmp), ANSIBLE_NOCOLOR='1',
                       ANSIBLE_HOST_KEY_CHECKING='True')
            deploy_root = Path.home() / ('db-lab-ssh-deploy-' + uuid.uuid4().hex)
            self.addCleanup(shutil.rmtree, deploy_root, ignore_errors=True)
            deploy_vars = {'db_lab_source_root': str(project), 'db_lab_deploy_root': str(deploy_root),
                           'db_lab_allow_changes': True}
            check = subprocess.run([PLAYBOOK, '-i', str(inventory), str(ROOT / 'ansible/deploy.yml'),
                                    '--check', '-e', json.dumps(deploy_vars)],
                                   cwd=ROOT / 'ansible', env=env, text=True,
                                   capture_output=True, timeout=60)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertFalse(deploy_root.exists())

            p = subprocess.run([PLAYBOOK, '-i', str(inventory), str(ROOT / 'ansible/deploy.yml'),
                                '-e', json.dumps(deploy_vars)],
                               cwd=ROOT / 'ansible', env=env, text=True,
                               capture_output=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            release_dirs = list((deploy_root / 'releases').iterdir())
            self.assertEqual(len(release_dirs), 1)
            release = release_dirs[0]
            self.assertTrue((release / 'all.sh').is_file())
            self.assertFalse((release / '.env').exists())
            self.assertFalse((release / 'reports').exists())
            self.assertTrue((release / '.env.example').is_file())

            repeated = subprocess.run([PLAYBOOK, '-i', str(inventory), str(ROOT / 'ansible/deploy.yml'),
                                       '-e', json.dumps(deploy_vars)],
                                      cwd=ROOT / 'ansible', env=env, text=True,
                                      capture_output=True, timeout=60)
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            self.assertIn('changed=0', repeated.stdout)

            inv = json.loads(inventory.read_text())
            inv['all']['children']['db_lab']['hosts']['disposable_ssh']['db_lab_project_root'] = str(release)
            inventory.write_text(json.dumps(inv))
            p = subprocess.run([PLAYBOOK, '-i', str(inventory), str(ROOT / 'ansible/control.yml')],
                               cwd=ROOT / 'ansible', env=env, text=True,
                               capture_output=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual((release / 'called.txt').read_text().strip(), '--fail-fast mvp status')
            self.assertIn('disposable_ssh', p.stdout)

    @staticmethod
    def _stop_server(server, log):
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=3)
        log.close()


if __name__ == '__main__':
    unittest.main()
