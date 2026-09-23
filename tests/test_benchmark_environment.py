import json
import sys
from pathlib import Path
import unittest
import time
from unittest.mock import mock_open, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
from db_lab_benchmark import HostPressureSampler, environment


class BenchmarkEnvironmentTests(unittest.TestCase):
    def test_cpu_ticks_exclude_guest_counters_already_in_user_and_nice(self):
        from db_lab_benchmark import host_pressure_snapshot
        def open_proc(path, *args, **kwargs):
            if path == '/proc/stat':
                return mock_open(read_data='cpu 10 20 30 40 5 6 7 8 900 100\n').return_value
            raise OSError(path)
        with patch('builtins.open',side_effect=open_proc):
            snapshot=host_pressure_snapshot()
        self.assertEqual(snapshot['host_cpu_ticks_total'],126)
        self.assertEqual(snapshot['host_cpu_ticks_idle'],45)

    def test_background_sampler_covers_blocking_work_and_stops(self):
        sampler=HostPressureSampler(interval_seconds=0.01).start()
        time.sleep(0.03)
        observation=sampler.stop()
        self.assertGreaterEqual(observation['sample_count'],3)
        self.assertGreaterEqual(observation['coverage_seconds'],0.02)
        self.assertIsNone(sampler._thread)

    def test_pressure_summary_uses_full_window_cpu_ticks_and_memory_minima(self):
        sampler=HostPressureSampler()
        sampler.samples=[
            {'elapsed_seconds':0,'host_cpu_ticks_total':1000,'host_cpu_ticks_idle':700,
             'host_load_1m':2.0,'host_available_memory_bytes':900,'host_swap_free_bytes':80},
            {'elapsed_seconds':2,'host_cpu_ticks_total':2000,'host_cpu_ticks_idle':1300,
             'host_load_1m':3.0,'host_available_memory_bytes':700,'host_swap_free_bytes':20},
        ]
        result=sampler.summary()
        self.assertEqual(result['coverage_seconds'],2)
        self.assertEqual(result['host_cpu_busy_pct_mean'],40)
        self.assertEqual(result['host_cpu_busy_pct_peak'],40)
        self.assertEqual(result['host_load_1m_peak'],3)
        self.assertEqual(result['host_available_memory_bytes_min'],700)
        self.assertEqual(result['host_swap_free_bytes_min'],20)

    def test_host_identity_is_hashed_and_missing_container_inspection_is_explicit(self):
        result=environment(None,['lab-db-1'])
        self.assertRegex(result['host_fingerprint'],r'^[0-9a-f]{20}$')
        self.assertNotIn('host_name',result)
        self.assertIn('host_available_memory_bytes',result)
        self.assertIn('host_swap_total_bytes',result)
        self.assertIn('host_swap_free_bytes',result)
        self.assertIsNone(result['container_limits'])
        self.assertIsNone(result['container_images'])

    @patch('db_lab_benchmark.subprocess.run')
    def test_container_config_digest_and_repo_digest_are_recorded(self,run):
        run.side_effect=[
            type('Result',(),{'stdout':json.dumps([{'Image':'sha256:config-id','HostConfig':{
                'Memory':512,'NanoCpus':2_000_000_000,'CpuQuota':0,'CpuPeriod':0}}])})(),
            type('Result',(),{'stdout':json.dumps([{'Id':'sha256:config-id',
                'RepoDigests':['example.test/redis@sha256:manifest-b','example.test/redis@sha256:manifest-a']}])})(),
        ]
        result=environment('podman',['lab-db-1'])
        self.assertEqual(result['container_limits']['lab-db-1'],
                         {'memory_bytes':512,'cpu_cores':2.0})
        self.assertEqual(result['container_images']['lab-db-1'],{
            'image_id':'sha256:config-id',
            'repo_digests':['example.test/redis@sha256:manifest-a','example.test/redis@sha256:manifest-b']})

    @patch('db_lab_benchmark.subprocess.run')
    def test_partial_image_inspection_is_not_reported_as_confirmed(self,run):
        run.return_value=type('Result',(),{'stdout':'not-json'})()
        result=environment('docker',['lab-db-1'])
        self.assertIsNone(result['container_limits'])
        self.assertIsNone(result['container_images'])


if __name__=='__main__':unittest.main()
