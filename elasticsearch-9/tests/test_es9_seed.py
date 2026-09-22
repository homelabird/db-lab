"""Offline regression checks for the es9 seed wiring.

Mirrors the legacy lab's generator guarantees (determinism, strict-mapping types,
incident cadence) against the ES 9 copy, and proves both labs share one generator.
"""
import hashlib
import importlib
import ipaddress
import json
import sys
import unittest
from pathlib import Path

ES9 = Path(__file__).resolve().parent.parent
LEGACY = ES9.parent / 'elasticsearch'


def load(module, lab):
    sys.path.insert(0, str(lab / 'scripts'))
    try:
        return importlib.import_module(module)
    finally:
        sys.path.pop(0)


class Es9SeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = load('generate_and_load', ES9)
        cls.legacy = load('generate_and_load', LEGACY)
        from lablib import INDICES, LAYOUT
        cls.indices, cls.layout = INDICES, LAYOUT

    def cfg(self, *extra):
        return self.seed.config_from_args(
            self.seed.parser().parse_args(['--size-mb', '0.15', *extra]))

    def test_lab_dispatches_seed_verify_query_size_purge(self):
        lab = (ES9 / 'lab.sh').read_text()
        for command in ('seed) run_python generate_and_load.py', 'verify) run_python verify_seed.py',
                        'query|queries)', 'size|dataset-size)', 'purge)'):
            self.assertIn(command, lab)
        self.assertIn('generate_and_load.py', lab)
        for mapping in (ES9 / 'mappings').glob('*.json'):
            json.loads(mapping.read_text())

    def test_shared_generator_same_documents_for_both_labs(self):
        c = self.cfg()
        for index in self.indices:
            ours = list(self.seed.documents(c, index))
            theirs = list(self.legacy.documents(c, index))
            self.assertEqual(ours, theirs)

    def test_generator_version_is_real(self):
        self.assertEqual(self.seed.GENERATOR_VERSION, 'seed-v4-real')
        shared = Path(ES9.parent / 'lib' / 'es-lab' / 'datagen' / 'realistic.py')
        self.assertIn('seed-v4-real', shared.read_text())

    def test_deterministic_and_seed_changes_data(self):
        c = self.cfg()
        self.assertEqual(list(self.seed.documents(c, self.indices[0])),
                         list(self.seed.documents(c, self.indices[0])))
        self.assertNotEqual(list(self.seed.documents(c, self.indices[0])),
                            list(self.seed.documents(self.cfg('--seed', '43'), self.indices[0])))

    def test_strict_mapping_types_and_budget(self):
        c = self.cfg()
        for index in self.indices:
            props = self.seed.definition(c, index)['mappings']['properties']
            records = list(self.seed.documents(c, index))
            total = sum(len(source) + 1 for _, source in records)
            self.assertGreaterEqual(total, self.seed.budget_for(c, index))
            self.assertLess(total, self.seed.budget_for(c, index) + max(len(s) + 1 for _, s in records))
            ids = set()
            for action, source in records:
                metadata = json.loads(action)['index']
                self.assertNotIn(metadata['_id'], ids)
                ids.add(metadata['_id'])
                self.assertEqual(metadata['_id'], json.loads(source)['document_id'])
                self._check_fields(json.loads(source), props, metadata['_id'])

    def test_first_doc_known_incidents(self):
        c = self.cfg()
        from datagen.realistic import documents as shared
        index = self.indices
        first = {name: json.loads(next(shared(c, name))[1]) for name in index}
        self.assertTrue(first[index[0]]['is_fraud'])
        self.assertGreaterEqual(first[index[0]]['risk_score'], 850)
        self.assertEqual(first[index[1]]['status'], 502)
        self.assertEqual(first[index[2]]['action'], 'LOGIN')
        self.assertEqual(first[index[2]]['result'], 'FAIL')

    def _check_fields(self, doc, definitions, where):
        for field, value in doc.items():
            mapping = definitions.get(field)
            self.assertIsNotNone(mapping, f'{where}: unmapped field {field}')
            self.assertIsNotNone(value, f'{where}: null {field}')
            if mapping.get('type') in ('object', 'nested') or 'properties' in mapping:
                for child in (value if isinstance(value, list) else [value]):
                    self._check_fields(child, mapping.get('properties', {}), where + '.' + field)
                continue
            typ = mapping.get('type')
            if typ == 'ip':
                ipaddress.ip_address(value)
            elif typ == 'geo_point':
                self.assertIsInstance(value, dict)
                self.assertIn('lat', value)
                self.assertIn('lon', value)
            elif typ == 'date':
                from datetime import datetime
                datetime.fromisoformat(value.replace('Z', '+00:00'))
            elif typ == 'boolean':
                self.assertIsInstance(value, bool)
            elif typ in ('integer', 'long'):
                self.assertIs(type(value), int)
            elif typ == 'double':
                numbers = value if isinstance(value, list) else [value]
                self.assertTrue(all(isinstance(item, (float, int)) for item in numbers))
            elif typ in ('keyword', 'text'):
                self.assertTrue(isinstance(value, (str, list)))


if __name__ == '__main__':
    unittest.main(verbosity=2)