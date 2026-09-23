import json
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from benchmarks import build_run_command, compare, repeat_database, run_database


def report(run_id, *, status='PASS', workers=4, delivered=100):
    return {'schema_version': 1, 'run_id': run_id, 'status': status,
            'evidence_kind': 'live_database', 'database': {'product': 'Kafka', 'version': '7.9'},
            'workload': {'name': 'produce', 'parameters': {'workers': workers}},
            'environment': {'container_engine': 'podman', 'container_engine_version': '5.4'},
            'metrics': {'delivered': delivered, 'latency': {'p95_ms': 10}},
            'verification': {'delivery_complete': status == 'PASS'}}


class BenchmarkCompareTests(unittest.TestCase):
    def test_repeat_runner_collects_only_fresh_pass_reports_and_saves_blocked_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);report_dir=root/'kafka'/'reports'/'benchmarks';report_dir.mkdir(parents=True)
            calls=[]
            def run(database, **options):
                calls.append((database,options))
                item=report(f'run-{len(calls):04d}')
                (report_dir/f"{item['run_id']}.json").write_text(json.dumps(item))
                return 0
            with patch('benchmarks.ROOT',root),patch('benchmarks.REPORT_DIRS',{'kafka':report_dir}), \
                 patch('benchmarks.run_database',side_effect=run):
                result=repeat_database('kafka',repeat=3,seconds=30)
            self.assertEqual(result,2)
            self.assertEqual(len(calls),3)
            saved=list((root/'reports'/'benchmarks').glob('kafka-comparison-*.json'))
            self.assertEqual(len(saved),1)
            comparison=json.loads(saved[0].read_text())
            self.assertEqual(comparison['repeat_count'],3)
            self.assertFalse(comparison['performance_comparison_ready'])
            self.assertEqual(len(comparison['report_paths']),3)

    def test_repeat_runner_requires_three_runs_and_full_duration(self):
        with self.assertRaisesRegex(ValueError,'repeat must be 3..10'):
            repeat_database('kafka',repeat=2,seconds=30,dry_run=True)
        with self.assertRaisesRegex(ValueError,'seconds >= 30'):
            repeat_database('kafka',repeat=3,seconds=10,dry_run=True)

    def test_repeat_runner_exports_fresh_redis_report_from_ops_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'redis-lab'/'output';output.mkdir(parents=True)
            old=output/'ops-old';old.mkdir()
            (old/'latest.json').write_text(json.dumps({'run_id':'old-run'}))
            (old/'old-run').mkdir()
            (old/'old-run'/'benchmark.json').write_text(json.dumps(report('old-run')))
            pointer=output/'ops-latest-export.json'
            pointer.write_text(json.dumps({'directory':'output/ops-old'}))
            ids=iter(('redis-0001','redis-0002','redis-0003'))
            def export(*args,**kwargs):
                run_id=next(ids);target=output/f'ops-{run_id}';target.mkdir()
                (target/'latest.json').write_text(json.dumps({'run_id':run_id}))
                (target/run_id).mkdir()
                (target/run_id/'benchmark.json').write_text(json.dumps(report(run_id)))
                pointer.write_text(json.dumps({'directory':str(target.relative_to(root/'redis-lab'))}))
                return SimpleNamespace(returncode=0)
            with patch('benchmarks.ROOT',root),patch('benchmarks.run_database',return_value=0), \
                 patch('benchmarks.subprocess.run',side_effect=export):
                result=repeat_database('redis',repeat=3,seconds=30)
            self.assertEqual(result,2)
            self.assertEqual(len(list((root/'reports'/'benchmarks').glob('redis-comparison-*.json'))),1)

    def test_repeat_runner_returns_success_only_when_comparison_gates_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);report_dir=root/'kafka'/'reports'/'benchmarks';report_dir.mkdir(parents=True)
            def run(database, **options):
                run_id=f'run-{len(list(report_dir.glob("*.json"))) + 1:04d}'
                item=report(run_id)
                item['duration_seconds']=30
                item['verification']['dataset_state_verified']=True
                item['environment'].update(host_fingerprint='host-a',host_cpu_count=8,
                    host_memory_bytes=1000,host_available_memory_bytes=800,
                    host_swap_total_bytes=200,host_swap_free_bytes=100,host_load_1m=1,
                    container_limits={'kafka':{'memory_bytes':512,'cpu_cores':2}},
                    container_images={'kafka-1':{'image_id':'sha256:image-a','repo_digests':[]}},
                    runtime_observation={'scope':'host','sample_count':31,'cpu_intervals_observed':30,
                        'coverage_seconds':30,'host_cpu_busy_pct_mean':25})
                (report_dir/f'{run_id}.json').write_text(json.dumps(item))
                return 0
            with patch('benchmarks.ROOT',root),patch('benchmarks.REPORT_DIRS',{'kafka':report_dir}), \
                 patch('benchmarks.run_database',side_effect=run):
                result=repeat_database('kafka',repeat=3,seconds=30)
            self.assertEqual(result,0)
            saved=list((root/'reports'/'benchmarks').glob('kafka-comparison-*.json'))
            self.assertTrue(json.loads(saved[0].read_text())['performance_comparison_ready'])

    def test_common_runner_targets_only_the_selected_lab_and_forwards_bounded_options(self):
        expected = {
            'es7': ('elasticsearch', 'benchmark'),
            'es9': ('elasticsearch-9', 'benchmark'),
            'mariadb': ('mariadb-ha-lab', 'load'),
            'kafka': ('kafka-lab', 'benchmark'),
            'redis': ('redis-lab', 'load'),
        }
        for database, (lab, operation) in expected.items():
            with self.subTest(database=database):
                cwd, command = build_run_command(database, seconds=17)
                self.assertEqual(cwd.name, lab)
                self.assertEqual(command[0:3], ['bash', 'lab.sh' if database != 'redis' else 'ops.sh', operation])
                self.assertNotIn('up', command)
                self.assertIn('17', command)
        _, es = build_run_command('es7', rate=77, batch=12)
        self.assertIn('77', es); self.assertIn('12', es)
        _, mariadb = build_run_command('mariadb', workers=3)
        self.assertIn('3', mariadb)
        _, kafka = build_run_command('kafka', rate=77, payload=64)
        self.assertIn('77', kafka); self.assertIn('64', kafka)
        _, redis = build_run_command('redis', rate=77, workers=3, keys=40, payload=64)
        self.assertIn('77', redis); self.assertIn('40', redis)
        with self.assertRaisesRegex(ValueError, 'seconds'):
            build_run_command('redis', seconds=4)
        with self.assertRaisesRegex(ValueError, 'do not apply'):
            build_run_command('mariadb', rate=77)
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            build_run_command('postgres')

    @patch('benchmarks.subprocess.run')
    def test_common_runner_invokes_only_the_selected_lab_and_propagates_exit(self, mocked_run):
        mocked_run.return_value.returncode = 7
        with redirect_stderr(StringIO()):
            result = run_database('mariadb', seconds=15, workers=2)
        self.assertEqual(result, 7)
        args, options = mocked_run.call_args
        self.assertEqual(args[0][:3], ['bash', 'lab.sh', 'load'])
        self.assertEqual(options['cwd'].name, 'mariadb-ha-lab')
        self.assertFalse(options['check'])

    def test_repeated_runs_report_median_dispersion_and_environment_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, value in enumerate((100, 110, 120)):
                path = Path(directory) / f'{index}.json'
                path.write_text(json.dumps(report(f'run-{index}', delivered=value)))
                paths.append(path)
            result = compare(paths)
        self.assertEqual(result['repeat_count'], 3)
        self.assertTrue(result['dispersion_interpretable'])
        self.assertFalse(result['environment_confirmed'])
        self.assertFalse(result['performance_comparison_ready'])
        self.assertEqual(result['metrics']['delivered']['median'], 110)
        self.assertAlmostEqual(result['metrics']['delivered']['sample_stdev'], 10)
        with self.assertRaisesRegex(ValueError, 'more than once'):
            compare([paths[0], paths[0]])

    def test_different_host_resources_do_not_confirm_performance_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[]
            for index in range(2):
                item=report(f'run-{index}')
                item['environment'].update(host_fingerprint='host-a' if index == 0 else 'host-b',
                    host_cpu_count=8, host_memory_bytes=1000, host_available_memory_bytes=800,
                    host_swap_total_bytes=200, host_swap_free_bytes=100, container_limits={'memory_bytes':512},
                    container_images={'redis-1':{'image_id':'sha256:image-a','repo_digests':[]}})
                path=Path(directory)/f'{index}.json';path.write_text(json.dumps(item));paths.append(path)
            result=compare(paths)
        self.assertFalse(result['environment_confirmed'])
        self.assertFalse(result['performance_comparison_ready'])
        self.assertEqual(result['differing_environment_metadata'],['host_fingerprint'])

    def test_matching_static_resources_still_disclose_unmeasured_runtime_contention(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[]
            for index, load in enumerate((12.0,15.0,11.0)):
                item=report(f'run-{index}')
                item['environment'].update(host_fingerprint='host-a',host_cpu_count=8,
                    host_memory_bytes=1000,host_available_memory_bytes=800-index*100,
                    host_swap_total_bytes=200,host_swap_free_bytes=100-index*25,container_limits={'redis':{'memory_bytes':512,'cpu_cores':2}},
                    container_images={'redis-1':{'image_id':'sha256:image-a','repo_digests':[]}},
                    host_load_1m=load)
                path=Path(directory)/f'{index}.json';path.write_text(json.dumps(item));paths.append(path)
            result=compare(paths)
        self.assertTrue(result['environment_confirmed'])
        self.assertFalse(result['performance_comparison_ready'])
        self.assertTrue(result['runtime_contention_unverified'])
        self.assertEqual(result['host_load_1m_by_run'],[12.0,15.0,11.0])
        self.assertEqual(result['host_pressure_by_run'][1]['host_available_memory_bytes'],700)

    def test_complete_full_run_observation_and_dataset_proofs_enable_three_run_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[]
            for index in range(3):
                item=report(f'run-{index}')
                item['duration_seconds']=30
                item['verification']['dataset_state_verified']=True
                item['environment'].update(host_fingerprint='host-a',host_cpu_count=8,
                    host_memory_bytes=1000,host_available_memory_bytes=800-index*10,
                    host_swap_total_bytes=200,host_swap_free_bytes=100-index,
                    host_load_1m=1+index,
                    container_limits={'redis':{'memory_bytes':512,'cpu_cores':2}},
                    container_images={'redis-1':{'image_id':'sha256:image-a','repo_digests':[]}},
                    runtime_observation={'scope':'host','sample_count':31,'cpu_intervals_observed':30,
                        'coverage_seconds':30,'host_cpu_busy_pct_mean':25})
                path=Path(directory)/f'{index}.json';path.write_text(json.dumps(item));paths.append(path)
            result=compare(paths)
        self.assertTrue(result['performance_comparison_ready'])
        self.assertFalse(result['runtime_contention_unverified'])
        self.assertEqual(result['dataset_state_unverified'],[])

    def test_missing_environment_fields_in_any_repeat_are_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[]
            for index in range(2):
                item=report(f'run-{index}')
                item['environment'].update(host_fingerprint='host-a',host_cpu_count=8,
                    host_memory_bytes=1000,host_available_memory_bytes=800,
                    host_swap_total_bytes=200,host_swap_free_bytes=100,container_limits={'redis':{'memory_bytes':512,'cpu_cores':2}},
                    container_images={'redis-1':{'image_id':'sha256:image-a','repo_digests':[]}})
                if index:
                    item['environment'].pop('host_fingerprint')
                path=Path(directory)/f'{index}.json';path.write_text(json.dumps(item));paths.append(path)
            result=compare(paths)
        self.assertEqual(result['missing_environment_metadata'],['host_fingerprint'])
        self.assertFalse(result['environment_confirmed'])

    def test_different_running_container_image_identity_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[]
            for index, image_id in enumerate(('sha256:image-a','sha256:image-b')):
                item=report(f'run-{index}')
                item['environment'].update(host_fingerprint='host-a',host_cpu_count=8,
                    host_memory_bytes=1000,host_available_memory_bytes=800,
                    host_swap_total_bytes=200,host_swap_free_bytes=100,container_limits={'redis':{'memory_bytes':512,'cpu_cores':2}},
                    container_images={'redis-1':{'image_id':image_id,'repo_digests':[]}})
                path=Path(directory)/f'{index}.json';path.write_text(json.dumps(item));paths.append(path)
            result=compare(paths)
        self.assertFalse(result['environment_confirmed'])
        self.assertIn('container_images',result['differing_environment_metadata'])

    def test_different_workload_and_failed_evidence_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, item in enumerate((report('run-0001'), report('run-0002', workers=8))):
                path = Path(directory) / f'{index}.json'
                path.write_text(json.dumps(item)); paths.append(path)
            with self.assertRaisesRegex(ValueError, 'workload differs'):
                compare(paths)
            paths[1].write_text(json.dumps(report('run-0002', status='FAIL')))
            with self.assertRaisesRegex(ValueError, 'only PASS'):
                compare(paths)

    def test_pass_reports_require_successful_postcondition_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=[]
            for index in range(2):
                item=report(f'run-{index}')
                path=Path(directory)/f'{index}.json';path.write_text(json.dumps(item));paths.append(path)
            paths[1].write_text(json.dumps({**report('run-1'), 'verification': {'count_matches': False}}))
            with self.assertRaisesRegex(ValueError, 'successful boolean verification'):
                compare(paths)
            paths[1].write_text(json.dumps({key:value for key,value in report('run-1').items()
                                            if key != 'verification'}))
            with self.assertRaisesRegex(ValueError, 'successful boolean verification'):
                compare(paths)


if __name__ == '__main__':
    unittest.main()
