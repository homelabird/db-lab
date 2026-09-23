#!/usr/bin/env python3
"""Compare repeated DB Lab benchmark-v1 runs without claiming a winner."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import shlex
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIRS = {
    'es7': ROOT / 'elasticsearch' / 'reports' / 'benchmarks',
    'es9': ROOT / 'elasticsearch-9' / 'reports' / 'benchmarks',
    'mariadb': ROOT / 'mariadb-ha-lab' / 'reports' / 'benchmarks',
    'kafka': ROOT / 'kafka-lab' / 'reports' / 'benchmarks',
}


def _number_metrics(value, prefix=''):
    found = {}
    if isinstance(value, dict):
        for key, item in value.items():
            found.update(_number_metrics(item, f'{prefix}.{key}' if prefix else key))
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        found[prefix] = float(value)
    return found


def _verification_checks(value):
    if isinstance(value, dict):
        return [check for item in value.values() for check in _verification_checks(item)]
    if isinstance(value, (list, tuple)):
        return [check for item in value for check in _verification_checks(item)]
    return [value] if isinstance(value, bool) else []


def compare(paths):
    if len(paths) < 2:
        raise ValueError('provide at least two report files')
    if len({Path(path).resolve() for path in paths}) != len(paths):
        raise ValueError('the same report file was provided more than once')
    runs = []
    for path in paths:
        report = json.loads(Path(path).read_text(encoding='utf-8'))
        if report.get('schema_version') != 1 or report.get('evidence_kind') != 'live_database':
            raise ValueError(f'{path}: expected a live_database benchmark schema_version 1 report')
        if report.get('status') != 'PASS':
            raise ValueError(f'{path}: only PASS reports can be compared')
        if not isinstance(report.get('database'), dict) or not isinstance(report.get('workload'), dict):
            raise ValueError(f'{path}: missing database/workload metadata')
        if not isinstance(report.get('metrics'), dict):
            raise ValueError(f'{path}: missing metrics')
        checks = _verification_checks(report.get('verification'))
        if not checks or not all(checks):
            raise ValueError(f'{path}: PASS report must contain successful boolean verification checks')
        database = report['database']
        if not database.get('version') and not database.get('configured_image_tag'):
            raise ValueError(f'{path}: missing observed database version or configured image tag')
        runs.append(report)

    reference = runs[0]
    run_ids = [run.get('run_id') for run in runs]
    if any(not isinstance(run_id, str) or not run_id for run_id in run_ids) or len(set(run_ids)) != len(run_ids):
        raise ValueError('each report must have a distinct non-empty run_id')
    for path, report in zip(paths[1:], runs[1:]):
        for section in ('database', 'workload'):
            if report[section] != reference[section]:
                raise ValueError(f'{path}: {section} differs; only identical database and workload settings are comparable')
        for key in ('container_engine', 'container_engine_version'):
            if report.get('environment', {}).get(key) != reference.get('environment', {}).get(key):
                raise ValueError(f'{path}: environment.{key} differs')

    flattened = [_number_metrics(run['metrics']) for run in runs]
    keys = sorted(set.intersection(*(set(row) for row in flattened))) if flattened else []
    summaries = {}
    for key in keys:
        values = [row[key] for row in flattened]
        mean = statistics.mean(values)
        sd = statistics.stdev(values) if len(values) >= 2 else 0.0
        summaries[key] = {
            'values': values,
            'median': statistics.median(values),
            'mean': mean,
            'sample_stdev': sd,
            'coefficient_of_variation_pct': round(sd * 100 / abs(mean), 2) if mean else None,
        }
    env = reference.get('environment', {})
    required_environment = ('container_engine', 'container_engine_version', 'host_fingerprint',
                            'host_cpu_count', 'host_memory_bytes', 'host_swap_total_bytes',
                            'container_limits', 'container_images')
    pressure_fields = ('host_load_1m', 'host_available_memory_bytes', 'host_swap_free_bytes')
    def metadata_present(run, key):
        value=run.get('environment', {}).get(key)
        if value is None:
            return False
        if key in ('host_memory_bytes', 'host_swap_total_bytes', 'host_cpu_count', *pressure_fields):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        if key == 'container_limits':
            return isinstance(value,dict) and bool(value) and all(isinstance(item,dict) for item in value.values())
        if key == 'container_images':
            return (isinstance(value,dict) and bool(value) and all(
                isinstance(item,dict) and isinstance(item.get('image_id'),str) and item['image_id']
                for item in value.values()))
        return bool(value)
    missing_environment = [key for key in required_environment if any(
        not metadata_present(run,key) for run in runs)]
    differing_environment = [key for key in required_environment if any(
        run.get('environment', {}).get(key) != env.get(key) for run in runs[1:])]
    missing_pressure_observations = [key for key in pressure_fields if any(
        not metadata_present(run,key) for run in runs)]
    def complete_runtime_observation(run):
        observation=run.get('environment', {}).get('runtime_observation')
        duration=run.get('duration_seconds')
        return (isinstance(observation,dict) and observation.get('scope')=='host'
                and isinstance(duration,(int,float)) and duration > 0
                and observation.get('sample_count',0) >= 2
                and observation.get('cpu_intervals_observed',0) >= 1
                and observation.get('host_cpu_busy_pct_mean') is not None
                and observation.get('coverage_seconds',0) >= duration * .8)
    incomplete_runtime_observations=[run['run_id'] for run in runs if not complete_runtime_observation(run)]
    short_runs=[run['run_id'] for run in runs
                if not isinstance(run.get('duration_seconds'),(int,float)) or run['duration_seconds']<30]
    dataset_state_unverified=[run['run_id'] for run in runs
                              if run.get('verification',{}).get('dataset_state_verified') is not True]
    environment_confirmed=not missing_environment and not differing_environment
    comparison_ready=(len(runs)>=3 and environment_confirmed and not incomplete_runtime_observations
                      and not short_runs and not dataset_state_unverified)
    return {
        'schema_version': 1,
        'database': reference['database'],
        'workload': reference['workload'],
        'run_ids': [run.get('run_id') for run in runs],
        'repeat_count': len(runs),
        'dispersion_interpretable': len(runs) >= 3,
        'configuration_comparable': True,
        'environment_confirmed': environment_confirmed,
        'performance_comparison_ready': comparison_ready,
        'runtime_contention_unverified': bool(incomplete_runtime_observations),
        'readiness_blockers': (['required host/container/image metadata is missing'] if missing_environment else [])
                              + (['required runtime/resource/image metadata differs'] if differing_environment else [])
                              + (['host pressure observations are missing'] if missing_pressure_observations else [])
                              + (['runtime pressure samples do not cover the full run: '+', '.join(incomplete_runtime_observations)]
                                 if incomplete_runtime_observations else [])
                              + (['each run must measure at least 30 seconds: '+', '.join(short_runs)]
                                 if short_runs else [])
                              + (['dataset state is not verified for every run: '+', '.join(dataset_state_unverified)]
                                 if dataset_state_unverified else [])
                              + (['at least three comparable repeats are required'] if len(runs)<3 else []),
        'host_load_1m_by_run': [run.get('environment', {}).get('host_load_1m') for run in runs],
        'host_pressure_by_run': [{key: run.get('environment', {}).get(key) for key in pressure_fields}
                                 for run in runs],
        'missing_pressure_observations': missing_pressure_observations,
        'incomplete_runtime_observations': incomplete_runtime_observations,
        'runs_below_minimum_duration': short_runs,
        'dataset_state_unverified': dataset_state_unverified,
        'missing_environment_metadata': missing_environment,
        'differing_environment_metadata': differing_environment,
        'metrics': summaries,
        'interpretation': 'Descriptive repeated-run statistics only. No winner or capacity/SLO claim is inferred.',
    }


def build_run_command(database, *, seconds=30, rate=None, workers=None, batch=None, keys=None, payload=None):
    if not 5 <= seconds <= 600:
        raise ValueError('seconds must be 5..600')
    if rate is not None and not 1 <= rate <= 10000:
        raise ValueError('rate must be 1..10000')
    if workers is not None and not 1 <= workers <= 64:
        raise ValueError('workers must be 1..64')
    if keys is not None and not 1 <= keys <= 50000:
        raise ValueError('keys must be 1..50000')
    if batch is not None and not 1 <= batch <= 1000:
        raise ValueError('batch must be 1..1000')
    if payload is not None and not 32 <= payload <= 8192:
        raise ValueError('payload must be 32..8192')
    if database in ('es7', 'es9'):
        if any(value is not None for value in (workers, keys, payload)):
            raise ValueError('ES supports --rate and --batch; the other options do not apply')
        rate, batch = rate or 100, batch or 100
    elif database == 'mariadb':
        if any(value is not None for value in (rate, batch, keys, payload)):
            raise ValueError('MariaDB supports --workers; rate, batch, keys, and payload do not apply')
        workers = workers or 4
    elif database == 'kafka':
        if any(value is not None for value in (workers, batch, keys)):
            raise ValueError('Kafka supports --rate and --payload; workers, batch, and keys do not apply')
        rate, payload = rate or 1000, payload or 256
    elif database == 'redis':
        if batch is not None:
            raise ValueError('Redis does not use --batch')
        rate, workers, keys, payload = rate or 300, workers or 8, keys or 2000, payload or 256
    routes = {
        'es7': (ROOT / 'elasticsearch', ['bash', 'lab.sh', 'benchmark', '--seconds', str(seconds),
                                         '--rate', str(rate), '--batch', str(batch)]),
        'es9': (ROOT / 'elasticsearch-9', ['bash', 'lab.sh', 'benchmark', '--seconds', str(seconds),
                                           '--rate', str(rate), '--batch', str(batch)]),
        'mariadb': (ROOT / 'mariadb-ha-lab', ['bash', 'lab.sh', 'load', '--seconds', str(seconds),
                                              '--workers', str(workers), '--target', 'writer']),
        'kafka': (ROOT / 'kafka-lab', ['bash', 'lab.sh', 'benchmark', '--kind', 'payments',
                                       '--duration', str(seconds), '--rate', str(rate),
                                       '--payload-bytes', str(payload), '--seed', '42']),
        'redis': (ROOT / 'redis-lab', ['bash', 'ops.sh', 'load', '--target', 'perf',
                                       '--seconds', str(seconds), '--rate', str(rate),
                                       '--workers', str(workers), '--keys', str(keys),
                                       '--payload', str(payload)]),
    }
    try:
        cwd, command = routes[database]
    except KeyError as exc:
        raise ValueError(f'unsupported database: {database}') from exc
    return cwd, command


def run_database(database, *, dry_run=False, **options):
    cwd, command = build_run_command(database, **options)
    print(f'Running in {cwd.relative_to(ROOT)}: {shlex.join(command)}', file=sys.stderr, flush=True)
    if dry_run:
        return 0
    return subprocess.run(command, cwd=cwd, check=False).returncode


def _reports(database):
    if database == 'redis':
        pointer = ROOT / 'redis-lab' / 'output' / 'ops-latest-export.json'
        if not pointer.is_file():
            return []
        try:
            output = ROOT / 'redis-lab' / json.loads(pointer.read_text(encoding='utf-8'))['directory']
            output = output.resolve()
            output_root = (ROOT / 'redis-lab' / 'output').resolve()
            if not output.is_relative_to(output_root):
                return []
            latest = json.loads((output / 'latest.json').read_text(encoding='utf-8'))
            run_id = latest.get('run_id')
            report = output / str(run_id) / 'benchmark.json'
            return [report] if run_id and report.is_file() else []
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return []
    report_dir = REPORT_DIRS[database]
    return sorted(report_dir.glob('*.json')) if report_dir.is_dir() else []


def _report_ids(paths):
    ids = set()
    for path in paths:
        try:
            run_id = json.loads(path.read_text(encoding='utf-8')).get('run_id')
            if isinstance(run_id, str) and run_id:
                ids.add(run_id)
        except (OSError, AttributeError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return ids


def repeat_database(database, *, repeat=3, dry_run=False, **options):
    if not 3 <= repeat <= 10:
        raise ValueError('repeat must be 3..10; use run for a single smoke measurement')
    if options.get('seconds', 30) < 30:
        raise ValueError('repeated comparisons require --seconds >= 30')
    if database not in (*REPORT_DIRS, 'redis'):
        raise ValueError(f'unsupported database: {database}')

    reports = []
    for index in range(repeat):
        before_reports = _reports(database)
        before = {path.resolve() for path in before_reports}
        before_ids = _report_ids(before_reports)
        result = run_database(database, dry_run=dry_run, **options)
        if result:
            return result
        if dry_run:
            continue
        if database == 'redis':
            export = subprocess.run(['bash', 'ops.sh', 'results'], cwd=ROOT / 'redis-lab', check=False)
            if export.returncode:
                print('[error] Redis report export failed; run data remains in its ops volume.', file=sys.stderr)
                return export.returncode
            found = _reports(database)
        else:
            found = [path for path in _reports(database) if path.resolve() not in before]
        if len(found) != 1:
            print(f'[error] Expected one fresh {database} report after repeat {index + 1}; found {len(found)}.',
                  file=sys.stderr)
            return 2
        try:
            report = json.loads(found[0].read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            print(f'[error] Could not read fresh benchmark report: {exc}', file=sys.stderr)
            return 2
        if report.get('run_id') in before_ids:
            print(f'[error] {database} repeated the previous run ID; refusing to compare stale evidence.',
                  file=sys.stderr)
            return 2
        if report.get('status') != 'PASS' or report.get('evidence_kind') != 'live_database':
            print(f"[error] Repeat {index + 1} did not produce a live_database PASS report: {found[0]}",
                  file=sys.stderr)
            return 1
        reports.append(found[0])
        print(f'[ok] repeat {index + 1}/{repeat}: {found[0]}', file=sys.stderr)

    if dry_run:
        return 0
    summary = compare(reports)
    summary['report_paths'] = [str(path.resolve().relative_to(ROOT.resolve())) for path in reports]
    output = ROOT / 'reports' / 'benchmarks'
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f'{database}-comparison-{uuid.uuid4().hex[:12]}.json'
    fd, temporary = tempfile.mkstemp(prefix='.comparison-', dir=output)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f'[summary] {destination.relative_to(ROOT)}', file=sys.stderr)
    if summary['performance_comparison_ready']:
        print('[ready] Repeats meet the comparison evidence gates.', file=sys.stderr)
        return 0
    print('[not-ready] Statistics saved; inspect readiness_blockers before treating runs as comparable.',
          file=sys.stderr)
    return 2


def run_main(argv):
    parser = argparse.ArgumentParser(description='Run an existing DB Lab bounded benchmark. It does not start or reset the lab.')
    parser.add_argument('database', choices=('es7', 'es9', 'mariadb', 'kafka', 'redis'))
    parser.add_argument('--seconds', type=int, default=30, help='bounded workload duration (5..600)')
    parser.add_argument('--rate', type=int, help='target rate for ES, Kafka, or Redis')
    parser.add_argument('--workers', type=int, help='MariaDB or Redis worker count')
    parser.add_argument('--batch', type=int, help='ES documents per bulk request')
    parser.add_argument('--keys', type=int, help='Redis keyspace size')
    parser.add_argument('--payload', type=int, help='Kafka/Redis payload bytes')
    parser.add_argument('--repeat', type=int, default=1,
                        help='repeat sequentially 3..10 times and save a comparison (requires >=30 seconds)')
    parser.add_argument('--dry-run', action='store_true', help='print the child command without running it')
    args = parser.parse_args(argv)
    try:
        options = vars(args).copy()
        options.pop('database')
        repeat = options.pop('repeat')
        if repeat == 1:
            return run_database(args.database, **options)
        return repeat_database(args.database, repeat=repeat, **options)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'run':
        return run_main(argv[1:])
    if argv and argv[0] in ('-h', '--help'):
        print('Usage: scripts/benchmarks.py [REPORT.json ...] | run <database> [options]\n'
              'Compare repeated live reports, or run a bounded benchmark against an already-running lab.\n'
              'Use --repeat 3 (minimum) to run, validate, and compare sequential reports.\n'
              'Run targets: es7, es9, mariadb, kafka, redis. No lab is started or reset.')
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(compare(args.reports), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'benchmark comparison failed: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
