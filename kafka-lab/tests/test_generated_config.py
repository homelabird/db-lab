import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('kid',ROOT/'scripts/kraft-id.py')
kid=importlib.util.module_from_spec(spec);spec.loader.exec_module(kid)
class GeneratedConfigTests(unittest.TestCase):
    def render(self,mode='zk',nodes=3,extra=(),ok=True):
        with tempfile.TemporaryDirectory(prefix='compose with spaces ') as tmp:
            out=Path(tmp)/'compose.generated.yaml'
            args=['python3',str(ROOT/'scripts/render-compose.py'),'--mode',mode,'--nodes',str(nodes),'--lab-name','test-lab','--cp-version','7.9.0','--advertised-host','localhost','--bind-ip','127.0.0.1','--output',str(out),*extra]
            p=subprocess.run(args,cwd='/',text=True,capture_output=True)
            if not ok:self.assertNotEqual(p.returncode,0);self.assertFalse(out.exists());return
            self.assertEqual(p.returncode,0,p.stderr)
            return json.loads(out.read_text())
    def test_build_context_exists_from_generated_location(self):
        c=self.render()['services']['tools']['build']
        self.assertTrue(Path(c['context']).is_absolute())
        self.assertTrue((Path(c['context'])/c['dockerfile']).is_file())
    def test_public_settings_are_forwarded(self):
        c=self.render(extra=['--port','1:19100','--kafka-heap=-Xms512m -Xmx1024m','--zookeeper-heap=-Xms64m -Xmx128m'])
        self.assertEqual(c['services']['kafka1']['ports'],['127.0.0.1:19100:9093'])
        self.assertIn(':19100',c['services']['kafka1']['environment']['KAFKA_ADVERTISED_LISTENERS'])
        self.assertEqual(c['services']['kafka1']['environment']['KAFKA_HEAP_OPTS'],'-Xms512m -Xmx1024m')
        self.assertEqual(c['services']['zk1']['environment']['KAFKA_HEAP_OPTS'],'-Xms64m -Xmx128m')
    def test_node_boundaries(self):
        for n in (1,3,9,11,99):
            c=self.render(nodes=n);self.assertIn('kafka'+str(n),c['services'])
            ports=[c['services']['kafka'+str(i)]['ports'][0] for i in range(1,n+1)]
            self.assertEqual(len(ports),len(set(ports)))
    def test_bad_ports_nodes_ids_fail_before_output(self):
        for args in (['--port','1:29092'],['--port','1:65536'],['--port','4:12345']):self.render(extra=args,ok=False)
        self.render(nodes=2,ok=False);self.render(nodes=101,ok=False)
        self.render(mode='kraft',extra=['--cluster-id','c84fb8da-8d29-4d8b-9014-c0fa611d4a78'],ok=False)
    def test_kraft_id_valid_persisted_and_conflict_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'id';value=kid.ensure(path)
            self.assertEqual(len(base64.urlsafe_b64decode(value+'==')),16)
            self.assertEqual(kid.ensure(path),value)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            other=kid.ensure(Path(tmp)/'other')
            with self.assertRaises(ValueError):kid.ensure(path,other)
            self.assertEqual(path.read_text().strip(),value)
            self.render(mode='kraft',extra=['--cluster-id',value])
    def test_invalid_saved_id_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'id';path.write_text('old-invalid-uuid')
            with self.assertRaises(ValueError):kid.ensure(path)
            self.assertEqual(path.read_text(),'old-invalid-uuid')
