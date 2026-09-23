"""Static regression checks for host publishing defaults; not a runtime network test."""
import re
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NetworkDefaultsTests(unittest.TestCase):
    def test_generated_env_overrides_script_defaults_but_not_caller_env(self):
        shared = ROOT.parent / 'lib' / 'es-lab' / 'common.sh'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.env.example').write_text(
                'ES_URL=http://127.0.0.1:19211\nLAB_CLUSTER_NAME=es9-fresh\n')
            command = (
                f'export LAB_ROOT={shlex.quote(temp)}; '
                f'source {shlex.quote(str(shared))}; ensure_lab_env >/dev/null; '
                'printf "%s|%s" "$ES_URL" "$LAB_CLUSTER_NAME"')
            child_env = {key: value for key, value in os.environ.items()
                         if key not in ('ES_URL', 'LAB_CLUSTER_NAME')}
            result = subprocess.run(['bash', '-c', command], env=child_env,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, 'http://127.0.0.1:19211|es9-fresh')
            self.assertEqual((root / '.env').stat().st_mode & 0o777, 0o600)

            child_env['ES_URL'] = 'http://caller.invalid:9999'
            result = subprocess.run(['bash', '-c', command], env=child_env,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, 'http://caller.invalid:9999|es9-fresh')

    def test_env_defaults_to_loopback(self):
        text = (ROOT / '.env.example').read_text()
        self.assertRegex(text, r'(?m)^ES_BIND_IP=127\.0\.0\.1$')
        self.assertNotRegex(text, r'(?m)^ES_BIND_IP=.*?/')

    def test_ui_compose_port_defaults_are_loopback(self):
        text = (ROOT / 'compose.yaml').read_text()
        values = re.findall(r'\$\{ES_BIND_IP:-([^}]+)\}', text)
        self.assertEqual(values, ['127.0.0.1', '127.0.0.1', '127.0.0.1'])
        self.assertIn('${ES_BIND_IP:-127.0.0.1}:${ES_PORT:-9200}:9200', text)
        self.assertIn('${ES_BIND_IP:-127.0.0.1}:${CEREBRO_PORT:-9000}:9000', text)
        self.assertIn('${ES_BIND_IP:-127.0.0.1}:${KIBANA_PORT:-5601}:5601', text)

    def test_local_client_url_is_not_a_listen_address(self):
        env = (ROOT / '.env.example').read_text()
        self.assertRegex(env, r'(?m)^ES_URL=http://127\.0\.0\.1:9200$')
        shared = ROOT.parent / 'lib' / 'es-lab'
        for name in ('common.sh', 'lablib_core.py'):
            text = (shared / name).read_text()
            self.assertIn('http://127.0.0.1:9200', text)
            self.assertNotIn('http://0.0.0.0', text)

    def test_cerebro_uses_container_dns(self):
        conf = (ROOT / 'cerebro/application.conf').read_text()
        self.assertIn('server.http.address = "0.0.0.0"', conf)
        self.assertIn('host = "http://es01:9200"', conf)

    def test_kibana_matches_elasticsearch_and_uses_container_dns(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertIn('docker.elastic.co/kibana/kibana:7.17.29', text)
        self.assertIn('ELASTICSEARCH_HOSTS=["http://es01:9200","http://es02:9200","http://es03:9200","http://es04:9200","http://es05:9200"]', text)
        self.assertIn('XPACK_SECURITY_ENABLED=${XPACK_SECURITY_ENABLED:-false}', text)

    def test_network_has_no_docker_only_driver_opts(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertNotIn('driver_opts', text)
        self.assertNotIn('enable_icc', text)
        self.assertIn('driver: bridge', text)

    def test_snapshot_repository_is_shared_and_preconfigured(self):
        main = (ROOT / 'compose.yaml').read_text()
        self.assertEqual(main.count('- es-snapshots:/usr/share/elasticsearch/snapshots'), 5)
        self.assertIn('es-snapshots: null', main)
        self.assertIn('path.repo=/usr/share/elasticsearch/snapshots', main)
        scaleout = (ROOT / 'compose.scaleout.yaml').read_text()
        self.assertIn('- es-snapshots:/usr/share/elasticsearch/snapshots', scaleout)
        self.assertIn('path.repo=/usr/share/elasticsearch/snapshots', scaleout)

    def test_cert_init_and_install_verifier_are_wired(self):
        main = (ROOT / 'compose.yaml').read_text()
        self.assertIn('cert-init:', main)
        self.assertEqual(main.count('es-certs:/usr/share/elasticsearch/config/certs:ro'), 5)
        self.assertIn('compose run --rm cert-init', (ROOT / 'scripts/01-up.sh').read_text())
        self.assertIn('verify-install) verify_install', (ROOT / 'lab.sh').read_text())
        self.assertIn('--green', (ROOT / 'scripts/01-up.sh').read_text())
        self.assertIn('kibana_status_available', (ROOT / 'scripts/01-up.sh').read_text())
        self.assertIn('auth=(--user', (ROOT.parent / 'lib/es-lab/common.sh').read_text())

    def test_xpack_optin_and_snapshot_vars_documented(self):
        env = (ROOT / '.env.example').read_text()
        for key in ('XPACK_SECURITY_ENABLED=false',
                    'ELASTIC_USERNAME=elastic',
                    'SNAPSHOT_REPO_NAME=lab-snapshots',
                    'SNAPSHOT_PATH=/usr/share/elasticsearch/snapshots'):
            self.assertIn(key, env)
        self.assertIn('ELASTIC_PASSWORD', env)

    def test_engine_and_network_helpers_exist(self):
        shared = ROOT.parent / 'lib' / 'es-lab'
        common = (shared / 'common.sh').read_text()
        for name in ('resolve_engine', 'network_subnet', 'es_network_diagnose',
                     'ensure_snapshot_repo', 'container_running'):
            self.assertIn(f'{name}()', common)
        core = (shared / 'lablib_core.py').read_text()
        self.assertIn('def ensure_snapshot_repo', core)
        self.assertIn("'snapshot-repo'", core)

    def test_lab_wires_the_shared_core(self):
        loader = (ROOT / 'scripts/common.sh').read_text()
        self.assertIn('export LAB_ROOT', loader)
        self.assertIn('lib/es-lab/common.sh', loader)
        self.assertIn('LAB_CONTAINER_PREFIX="${LAB_CONTAINER_PREFIX:-cerebro-seed-}"', loader)
        self.assertIn('LAB_CONTAINER_PREFIX=cerebro-seed-', (ROOT / '.env.example').read_text())
        self.assertIn('${LAB_CONTAINER_PREFIX:-cerebro-seed-}es01', (ROOT / 'compose.yaml').read_text())
        self.assertIn('doctor|check [--install-missing]', (ROOT / 'lab.sh').read_text())
        self.assertIn('check_host_packages "$install_missing"', (ROOT / 'scripts/00-doctor.sh').read_text())
        lablib = (ROOT / 'scripts/lablib.py').read_text()
        self.assertIn('from lablib_core import', lablib)


if __name__ == '__main__':
    unittest.main()
