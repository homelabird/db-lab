"""Static regression checks for the Elasticsearch 9 lab; no runtime cluster needed."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT.parent / 'lib' / 'es-lab'


class Es9DefaultsTests(unittest.TestCase):
    def test_host_ports_and_client_url(self):
        env = (ROOT / '.env.example').read_text()
        for key in ('ES_PORT=9201', 'KIBANA_PORT=5602', 'ES_URL=http://127.0.0.1:9201',
                    'ES_BIND_IP=127.0.0.1', 'COMPOSE_PROJECT_NAME=es9-lab'):
            self.assertIn(key, env)
        self.assertRegex(env, r'(?m)^ES_PORT=9201$')
        self.assertRegex(env, r'(?m)^KIBANA_PORT=5602$')
        text = (ROOT / 'compose.yaml').read_text()
        self.assertIn('${ES_BIND_IP:-127.0.0.1}:${ES_PORT:-9201}:9200', text)
        self.assertIn('${ES_BIND_IP:-127.0.0.1}:${KIBANA_PORT:-5602}:5601', text)

    def test_only_es01_publishes_the_host_port(self):
        # Like the legacy lab, only es01 (plus kibana) publishes a host port;
        # the other nodes stay on the internal bridge. All five declared the
        # same container port, so the host binding must appear exactly once.
        text = (ROOT / 'compose.yaml').read_text()
        self.assertEqual(text.count('${ES_BIND_IP:-127.0.0.1}:${ES_PORT:-9201}:9200'), 1)

    def test_no_cerebro_and_no_docker_only_network_opts(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertNotIn('cerebro', text)
        self.assertNotIn('driver_opts', text)
        self.assertNotIn('enable_icc', text)
        self.assertIn('driver: bridge', text)

    def test_latest_images_are_pinned(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertEqual(text.count('image: docker.elastic.co/elasticsearch/elasticsearch:9.5.3'), 3)
        self.assertEqual(text.count('docker.elastic.co/elasticsearch/elasticsearch:9.5.3'), 3)
        self.assertEqual(text.count('image: docker.elastic.co/kibana/kibana:9.5.3'), 1)
        self.assertIn('ELASTICSEARCH_HOSTS=["http://es01:9200","http://es02:9200","http://es03:9200","http://es04:9200","http://es05:9200"]', text)

    def test_security_is_opt_in_and_autoconfig_disabled(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertEqual(text.count('xpack.security.enabled=${XPACK_SECURITY_ENABLED:-false}'), 5)
        self.assertEqual(text.count('xpack.security.autoconfiguration.enabled=false'), 5)
        env = (ROOT / '.env.example').read_text()
        for key in ('XPACK_SECURITY_ENABLED=false', 'ELASTIC_USERNAME=elastic',
                    'SNAPSHOT_REPO_NAME=lab-snapshots',
                    'SNAPSHOT_PATH=/usr/share/elasticsearch/snapshots'):
            self.assertIn(key, env)
        self.assertIn('ELASTIC_PASSWORD', env)

    def test_bootstrap_injects_initial_master_nodes_only_on_fresh_data(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertIn('cluster.initial_master_nodes=es01,es02,es03', text)
        self.assertIn('if [ ! -d /usr/share/elasticsearch/data/nodes ]; then', text)
        self.assertIn("exec env 'cluster.initial_master_nodes=es01,es02,es03' /usr/local/bin/docker-entrypoint.sh \"$@\"", text)
        self.assertIn('exec /usr/local/bin/docker-entrypoint.sh "$@"', text)

    def test_snapshot_repository_is_shared_on_all_nodes(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertEqual(text.count('es-snapshots:/usr/share/elasticsearch/snapshots'), 6)
        self.assertIn('es-snapshots: null', text)
        self.assertEqual(text.count('path.repo=/usr/share/elasticsearch/snapshots'), 5)

    def test_snapshot_init_runs_first_as_root_and_chowns(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertIn('snapshot-init:', text)
        self.assertIn('user: "0:0"', text)
        self.assertIn('chown -R 1000:0 /usr/share/elasticsearch/snapshots', text)
        # Defined once on the shared node anchor; all 5 ES nodes inherit it.
        self.assertEqual(text.count('condition: service_completed_successfully'), 1)
        self.assertEqual(text.count('<<: *es9-node'), 5)

    def test_cert_init_is_a_real_shell_command_and_shares_a_named_volume(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertIn('cert-init:', text)
        self.assertIn('entrypoint:\n    - /bin/bash\n    - -c\n    - |', text)
        self.assertIn('es-certs:/usr/share/elasticsearch/config/certs', text)
        self.assertEqual(text.count('es-certs:/usr/share/elasticsearch/config/certs:ro'), 5)
        self.assertIn('compose run --rm cert-init', (ROOT / 'scripts/01-up.sh').read_text())

    def test_install_verification_checks_all_stack_layers(self):
        verifier = (SHARED / 'verify-install.py').read_text()
        for check in ('_cluster/health', '_snapshot/', '_verify', '/api/status', 'available'):
            self.assertIn(check, verifier)
        self.assertIn('Authorization', verifier)
        self.assertIn('overall.get("level") or overall.get("state")', verifier)
        self.assertIn('verify-install) verify_install', (ROOT / 'lab.sh').read_text())

    def test_no_scenario_or_cerebro_machinery(self):
        for name in ('scenarios', 'datasets', 'cerebro'):
            self.assertFalse((ROOT / name).exists(), name)
        # Seed machinery (shared with the legacy lab) IS present.
        for name in ('mappings', 'queries'):
            self.assertTrue((ROOT / name).is_dir(), name)

    def test_wires_the_shared_core(self):
        loader = (ROOT / 'scripts/common.sh').read_text()
        self.assertIn('export LAB_ROOT', loader)
        self.assertIn('lib/es-lab/common.sh', loader)
        self.assertIn('LAB_CONTAINER_PREFIX="${LAB_CONTAINER_PREFIX:-es9-lab-}"', loader)
        self.assertIn('LAB_CONTAINER_PREFIX=es9-lab-', (ROOT / '.env.example').read_text())
        self.assertIn('${LAB_CONTAINER_PREFIX:-es9-lab-}es01', (ROOT / 'compose.yaml').read_text())
        self.assertIn('doctor|check [--install-missing]', (ROOT / 'lab.sh').read_text())
        self.assertIn('check_host_packages "$install_missing"', (ROOT / 'scripts/00-doctor.sh').read_text())
        lablib = (ROOT / 'scripts/lablib.py').read_text()
        self.assertIn('from lablib_core import', lablib)
        common = (SHARED / 'common.sh').read_text()
        for name in ('resolve_engine', 'network_subnet', 'es_network_diagnose',
                     'ensure_snapshot_repo', 'container_running'):
            self.assertIn(f'{name}()', common)
        core = (SHARED / 'lablib_core.py').read_text()
        self.assertIn('def ensure_snapshot_repo', core)
        self.assertIn("'snapshot-repo'", core)

    def test_compose_uses_shared_node_anchor(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertIn('x-es9-node: &es9-node', text)
        self.assertEqual(text.count('<<: *es9-node'), 5)


if __name__ == '__main__':
    unittest.main()
