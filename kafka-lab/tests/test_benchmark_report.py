import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('save_benchmark', ROOT/'scripts/save-benchmark.py')
save_benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(save_benchmark)
runner_spec = importlib.util.spec_from_file_location('run_observed', ROOT/'scripts/run-observed.py')
run_observed = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(run_observed)


class BenchmarkReportTests(unittest.TestCase):
    def test_writes_versioned_report_with_delivery_verification(self):
        native = {'run_id':'0123456789ab','started_utc':'2026-09-24T00:00:00+00:00',
                  'topic':'lab.benchmark.0123456789abcdef0123456789abcdef',
                  'parameters':{'rate':100},'elapsed_seconds':2.0,
                  'client_library_version':'2.11.1','queued':200,'delivered':200,
                  'failed':0,'pending':0,'logical_bytes':50000,
                  'delivered_per_second':100.0,'ack_latency_p95_ms_last_10000':8.5,'errors':{},
                  'benchmark_dataset_state_verified':True,'benchmark_cleanup_verified':True,
                  'benchmark_topic_partitions':12}
        with tempfile.TemporaryDirectory() as directory:
            observation={'scope':'host','sample_count':2,'cpu_intervals_observed':1,'coverage_seconds':0.01}
            with patch.object(save_benchmark,'host_environment',return_value={'host_fingerprint':'test','host_cpu_count':8,
                'host_memory_bytes':1000,'container_limits':{'kafka':{'memory_bytes':None,'cpu_cores':None}}}):
                path, report = save_benchmark.save(native,'PASS',directory,'7.9.0','zk',3,'podman','5.6',observation)
            actual = json.loads(path.read_text())
            self.assertEqual(actual['schema_version'],1)
            self.assertTrue(actual['verification']['all_queued_delivered'])
            self.assertTrue(actual['verification']['dataset_state_verified'])
            self.assertEqual(actual['dataset']['partition_count'],12)
            self.assertEqual(actual['metrics']['delivered_per_second'],100.0)
            self.assertEqual(report['database']['configured_image_tag'],'7.9.0')
            self.assertEqual(actual['environment']['runtime_observation'],observation)

    def test_failed_run_cannot_claim_delivery_verification(self):
        native = {'run_id':'0123456789ab','queued':4,'delivered':3,'failed':1,'pending':0,'logical_bytes':10}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(save_benchmark,'host_environment',return_value={}):
                _, report = save_benchmark.save(native,'FAIL',directory,'7.9.0','kraft',3,'docker','29.6')
            self.assertFalse(report['verification']['all_queued_delivered'])

    def test_persistent_topic_never_claims_dataset_isolation(self):
        native={'run_id':'0123456789ab','topic':'lab.payments','queued':1,'delivered':1,
                'failed':0,'pending':0,'benchmark_dataset_state_verified':True,
                'benchmark_cleanup_verified':True}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(save_benchmark,'host_environment',return_value={}):
            _,report=save_benchmark.save(native,'PASS',directory,'7.9.0','zk',3,'docker','29.6')
        self.assertFalse(report['dataset']['temporary_topic'])
        self.assertFalse(report['verification']['dataset_state_verified'])

    def test_invalid_run_id_and_counters_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(save_benchmark,'host_environment',return_value={}):
                with self.assertRaises(ValueError):
                    save_benchmark.save({'run_id':'../escape'},'PASS',directory,'7.9.0','zk',3,'docker','29.6')
            native={'run_id':'0123456789ab','queued':'4'}
            with patch.object(save_benchmark,'host_environment',return_value={}):
                with self.assertRaises(ValueError):
                    save_benchmark.save(native,'PASS',directory,'7.9.0','zk',3,'docker','29.6')

    def test_observed_runner_samples_around_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'observation.json'
            stream=io.StringIO()
            with redirect_stdout(stream):
                status=run_observed.main(['--output',str(output),'--',sys.executable,'-c',
                                          'import time; time.sleep(.03); print("child-output")'])
            observation=json.loads(output.read_text())
            self.assertEqual(status,0)
            self.assertIn('child-output',stream.getvalue())
            self.assertEqual(observation['scope'],'host')
            self.assertGreaterEqual(observation['sample_count'],2)
            self.assertGreater(observation['coverage_seconds'],0)


if __name__ == '__main__':
    unittest.main()
