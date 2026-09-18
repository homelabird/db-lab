"""Static regression checks for host publishing defaults; not a runtime network test."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NetworkDefaultsTests(unittest.TestCase):
    def test_env_publishes_all_ipv4(self):
        text = (ROOT / '.env.example').read_text()
        self.assertRegex(text, r'(?m)^ES_BIND_IP=0\.0\.0\.0$')
        self.assertNotRegex(text, r'(?m)^ES_BIND_IP=.*?/')

    def test_both_compose_port_defaults_publish_all_ipv4(self):
        text = (ROOT / 'compose.yaml').read_text()
        values = re.findall(r'\$\{ES_BIND_IP:-([^}]+)\}', text)
        self.assertEqual(values, ['0.0.0.0', '0.0.0.0'])
        self.assertIn('${ES_BIND_IP:-0.0.0.0}:${ES_PORT:-9200}:9200', text)
        self.assertIn('${ES_BIND_IP:-0.0.0.0}:${CEREBRO_PORT:-9000}:9000', text)

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


if __name__ == '__main__':
    unittest.main()
