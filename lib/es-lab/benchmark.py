#!/usr/bin/env python3
"""Run a bounded synthetic bulk-write benchmark against the owned ES lab."""
import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
import uuid

from lablib_core import APIError, ESClient, write_json

ROOT = Path(__file__).resolve().parents[2]
LAB_ROOT = Path(os.environ.get('LAB_ROOT', ROOT / 'elasticsearch')).resolve()
INDEX = 'lab-benchmark-v1'
sys.path.insert(0,str(ROOT/'lib'))
from db_lab_benchmark import HostPressureSampler, environment as host_environment


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * p)))]


def run(seconds, rate, batch, seed=42, client=None):
    client = client or ESClient(timeout=30)
    cluster = client.assert_lab()
    try:
        client.request('GET', f'/{INDEX}/_settings')
    except APIError as exc:
        if exc.status != 404:
            raise
    else:
        raise RuntimeError(f'{INDEX} already exists; inspect it before retrying. Existing data will not be overwritten.')
    client.request('PUT', f'/{INDEX}', {
        'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
        'mappings': {'properties': {'@timestamp': {'type': 'date'},
                                    'transaction_id': {'type': 'keyword'},
                                    'user_id': {'type': 'keyword'}, 'device_id': {'type': 'keyword'},
                                    'institution': {'type': 'keyword'}, 'channel': {'type': 'keyword'},
                                    'amount': {'type': 'scaled_float', 'scaling_factor': 100},
                                    'currency': {'type': 'keyword'}, 'country': {'type': 'keyword'},
                                    'risk_score': {'type': 'integer'}, 'is_fraud': {'type': 'boolean'}}}})
    run_id = uuid.uuid4().hex[:16]
    pressure_sampler = HostPressureSampler(interval_seconds=1.0)
    rng = random.Random(seed)
    started = datetime.now(timezone.utc)
    start = time.monotonic()
    pressure_sampler.sample(force=True)
    deadline = start + seconds
    queued = indexed = failed = uncertain = sequence = scheduled = scheduled_skipped = 0
    latencies = deque(maxlen=100_000)
    errors = {}
    while time.monotonic() < deadline:
        pressure_sampler.sample()
        due = start + scheduled / rate
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, deadline - time.monotonic()))
        now = time.monotonic()
        if now >= deadline:
            break
        if now > due:
            missed = int((now - due) * rate)
            scheduled += missed
            scheduled_skipped += missed
            due = start + scheduled / rate
        count = min(batch, max(1, int((deadline - due) * rate)))
        lines = []
        for offset in range(count):
            doc_id = f'BENCH-{run_id}-{sequence + offset:09d}'
            lines.append(json.dumps({'index': {'_index': INDEX, '_id': doc_id}}, separators=(',', ':')))
            lines.append(json.dumps({
                '@timestamp': datetime.now(timezone.utc).isoformat(), 'transaction_id': doc_id,
                'user_id': f'user-{rng.randrange(5000):05d}',
                'device_id': f'dev-{rng.randrange(12000):05d}',
                'institution': rng.choice(['alpha-card', 'beta-bank', 'gamma-pay', 'delta-life']),
                'channel': rng.choice(['WEB', 'APP', 'ATM', 'ARS']),
                'amount': round(rng.uniform(1000, 2000000), 2), 'currency': 'KRW',
                'country': 'KR', 'risk_score': rng.randrange(1001), 'is_fraud': False,
            }, separators=(',', ':')))
        payload = ('\n'.join(lines) + '\n').encode()
        queued += count
        scheduled += count
        sequence += count
        before = time.monotonic()
        try:
            result = client.request('POST', '/_bulk', payload, 'application/x-ndjson')
        except Exception as exc:
            uncertain += count
            errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + count
            break
        latencies.append(round((time.monotonic() - before) * 1000, 3))
        items = result.get('items', []) if isinstance(result, dict) else []
        if len(items) != count:
            uncertain += count
            errors['invalid_bulk_response'] = errors.get('invalid_bulk_response', 0) + count
            break
        for item in items:
            outcome = item.get('index', {})
            if 200 <= outcome.get('status', 0) < 300 and not outcome.get('error'):
                indexed += 1
            else:
                failed += 1
                kind = str(outcome.get('error', {}).get('type', 'unknown'))
                errors[kind] = errors.get(kind, 0) + 1
    elapsed = round(time.monotonic() - start, 3)
    count_verified = False
    cleanup_ok = False
    try:
        client.request('POST', f'/{INDEX}/_refresh')
        observed = client.request('GET', f'/{INDEX}/_count').get('count')
        count_verified = observed == indexed
        if not count_verified:
            errors['post_write_count_mismatch'] = 1
    except Exception as exc:
        errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
    try:
        client.request('DELETE', f'/{INDEX}')
        cleanup_ok = True
    except Exception as exc:
        errors['cleanup_' + type(exc).__name__] = 1
    finished = datetime.now(timezone.utc)
    pressure_sampler.sample(force=True)
    revision, source_dirty = _revision_state()
    prefix=os.environ.get('LAB_CONTAINER_PREFIX','')
    containers=[prefix+node for node in os.environ.get('LAB_ES_NODES','es01 es02 es03 es04 es05').split()]
    report = {
        'schema_version': 1, 'run_id': run_id,
        'status': 'PASS' if indexed > 0 and failed == 0 and uncertain == 0 and count_verified and cleanup_ok else 'FAIL',
        'evidence_kind': 'live_database', 'started_utc': started.isoformat(),
        'finished_utc': finished.isoformat(), 'duration_seconds': elapsed,
        'database': {'product': 'Elasticsearch', 'version': cluster.get('version', {}).get('number'),
                     'cluster_name': cluster.get('cluster_name')},
        'workload': {'name': 'synthetic-bulk-index', 'parameters': {
            'index': INDEX, 'requested_seconds': seconds, 'target_documents_per_second': rate,
            'batch_documents': batch, 'seed': seed}},
        'environment': {**host_environment(os.environ.get('RUNTIME'),containers),
                        'runtime_observation': pressure_sampler.summary(),
                        'revision': revision, 'source_dirty': source_dirty,
                         'container_engine': os.environ.get('RUNTIME'),
                         'container_engine_version': _engine_version()},
        'metrics': {'documents_queued': queued, 'documents_indexed': indexed,
                    'documents_failed': failed, 'documents_uncertain': uncertain,
                    'scheduled_documents_skipped': scheduled_skipped,
                    'indexed_per_second': round(indexed / elapsed, 2) if elapsed else 0,
                    'bulk_batch_latency_p50_ms': percentile(latencies, .50),
                    'bulk_batch_latency_p95_ms': percentile(latencies, .95),
                    'bulk_batch_latency_p99_ms': percentile(latencies, .99),
                    'errors': errors},
        'verification': {'bulk_acknowledged_all_documents': queued == indexed and failed == 0 and uncertain == 0,
                         'refreshed_document_count_matches_acknowledgements': count_verified,
                         'temporary_index_deleted': cleanup_ok,
                         'dataset_state_verified': count_verified and cleanup_ok and queued == indexed},
        'note': 'Bulk acknowledgement and batch latency only; data is written to and verified in a dedicated temporary index which is then deleted. This does not measure search latency, durability, host equivalence, or production capacity.'}
    output = LAB_ROOT / 'reports' / 'benchmarks'
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / f'{run_id}.json', report)
    return report


def _revision_state():
    try:
        revision = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True,
                                  text=True, timeout=3, check=True).stdout.strip()
        dirty = bool(subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain'], capture_output=True,
                                    text=True, timeout=3, check=True).stdout.strip())
        return revision, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def _engine_version():
    runtime = os.environ.get('RUNTIME')
    if runtime not in ('docker', 'podman'):
        return None
    try:
        result = subprocess.run([runtime, '--version'], capture_output=True, text=True,
                                timeout=3, check=True)
        return result.stdout.strip()[:160]
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=30, choices=range(5, 601))
    parser.add_argument('--rate', type=int, default=100, choices=range(1, 10001))
    parser.add_argument('--batch', type=int, default=100, choices=range(1, 1001))
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args(argv)
    try:
        report = run(args.seconds, args.rate, args.batch, args.seed)
        print('BENCHMARK_JSON=' + json.dumps(report, separators=(',', ':'), allow_nan=False))
        print(f"Benchmark report: reports/benchmarks/{report['run_id']}.json")
        return 0 if report['status'] == 'PASS' else 1
    except (APIError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f'[error] benchmark failed: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
