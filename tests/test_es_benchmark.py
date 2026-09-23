import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import os

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'lib'/'es-lab'))
sys.path.insert(0,str(ROOT/'lib'))
import db_lab_benchmark
import benchmark
from lablib_core import APIError


class FakeClient:
    def __init__(self): self.documents=0
    def assert_lab(self): return {'cluster_name':'test-lab','version':{'number':'9.5.3'}}
    def request(self, method, path, body=None, content_type='application/json', timeout=None):
        if method=='GET' and path.endswith('/_settings'):
            raise APIError(404,method,path,'missing')
        if method=='POST' and path=='/_bulk':
            count=len(body.decode().splitlines())//2;self.documents+=count
            return {'items':[{'index':{'status':201}} for _ in range(count)]}
        if method=='GET' and path.endswith('/_count'): return {'count':self.documents}
        return {}


class ElasticsearchBenchmarkTests(unittest.TestCase):
    def test_bounded_run_writes_normalized_report_and_deletes_temp_index(self):
        with tempfile.TemporaryDirectory() as temp:
            clock=[0.0]
            def monotonic(): return clock[0]
            def sleep(duration): clock[0]+=duration
            tick=[0]
            def pressure_snapshot():
                tick[0]+=100
                return {'host_cpu_ticks_total':tick[0],'host_cpu_ticks_idle':tick[0]*.75,
                        'host_load_1m':1.0,'host_available_memory_bytes':1000,
                        'host_swap_free_bytes':500,'host_memory_bytes':2000,
                        'host_swap_total_bytes':1000}
            with patch.object(benchmark,'LAB_ROOT',Path(temp)), \
                 patch.dict(os.environ,{'RUNTIME':''}), \
                 patch.object(benchmark.time,'monotonic',side_effect=monotonic), \
                 patch.object(benchmark.time,'sleep',side_effect=sleep), \
                 patch.object(db_lab_benchmark,'host_pressure_snapshot',side_effect=pressure_snapshot):
                result=benchmark.run(5,2,2,seed=23,client=FakeClient())
            report=json.loads((Path(temp)/'reports/benchmarks'/f"{result['run_id']}.json").read_text())
        self.assertEqual(result['status'],'PASS')
        self.assertTrue(report['verification']['refreshed_document_count_matches_acknowledgements'])
        self.assertTrue(report['verification']['temporary_index_deleted'])
        self.assertEqual(report['database']['version'],'9.5.3')
        self.assertEqual(report['workload']['parameters']['seed'],23)
        self.assertIsInstance(report['environment']['source_dirty'],bool)
        observation=report['environment']['runtime_observation']
        self.assertGreaterEqual(observation['sample_count'],2)
        self.assertGreaterEqual(observation['coverage_seconds'],5)
        self.assertGreaterEqual(observation['cpu_intervals_observed'],1)


if __name__=='__main__': unittest.main()
