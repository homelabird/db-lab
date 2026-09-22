"""Static regression checks for the ES9 modern feature scenarios.

Runs without a cluster: verifies the dispatcher, the feature script's API usage,
the kNN fixtures, and that the shared query catalog was NOT polluted with
ES9-only endpoints (the 7.x legacy lab reuses that catalog file).
"""
import json
import re
import unittest
from pathlib import Path

ES9 = Path(__file__).resolve().parent.parent
CATALOG = ES9.parent / 'elasticsearch' / 'queries' / 'catalog.json'


class Es9FeaturesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ES9 / 'scripts/features.py').read_text(encoding='utf-8')

    def test_lab_dispatches_features(self):
        lab = (ES9 / 'lab.sh').read_text()
        self.assertIn('features) run_python features.py', lab)
        self.assertIn('features [--clean]', lab)

    def test_script_uses_modern_api_surface(self):
        for token in ('/_query', '_async_search', 'dense_vector', '_index_template/', '_ilm/policy/'):
            self.assertIn(token, self.script)
        self.assertIn('client.assert_lab()', self.script)
        self.assertIn('--clean', self.script)

    def test_clean_removes_every_artefact(self):
        for path in ('/_data_stream/{DATA_STREAM}', '/_index_template/{TEMPLATE}',
                     '/_ilm/policy/{ILM}', '/{KNN_INDEX}'):
            self.assertIn(path, self.script)

    def test_shared_catalog_has_no_es9_only_endpoints(self):
        catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
        queue = catalog['examples'] if isinstance(catalog, dict) and 'examples' in catalog else catalog
        paths = [e.get('path', '') for e in queue]
        for forbidden in ('/_query', '_async_search', '/_knn_search'):
            self.assertNotIn(forbidden, ' '.join(paths))
        self.assertGreaterEqual(len(paths), 20, 'catalog examples parsed')

    def test_knn_fixture_dimensions(self):
        match = re.search(r'KNN_DOCS = \[(.*?)\n\]', self.script, re.S)
        self.assertIsNotNone(match, 'KNN_DOCS fixture missing')
        docs = re.findall(r"\('knn-\d+',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*\[([^\]]+)\]\)",
                          match.group(1))
        self.assertEqual(len(docs), 6)
        for (title, _category, _text, dims) in docs:
            vector = [float(x) for x in dims.split(',')]
            self.assertEqual(len(vector), 8, title)
            self.assertTrue(all(-1.0 <= v <= 1.0 for v in vector), title)


if __name__ == '__main__':
    unittest.main(verbosity=2)