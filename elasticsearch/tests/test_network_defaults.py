"""Static regression checks for host publishing defaults; not a runtime network test."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NetworkDefaultsTests(unittest.TestCase):
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
        for name in ('scripts/common.sh', 'scripts/lablib.py'):
            text = (ROOT / name).read_text()
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
        self.assertIn('XPACK_SECURITY_ENABLED=false', text)


if __name__ == '__main__':
    unittest.main()
