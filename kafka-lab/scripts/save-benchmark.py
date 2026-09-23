#!/usr/bin/env python3
"""Persist a sanitized Kafka seed result as a local benchmark run report."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('DB_LAB_LIB', str(ROOT.parent/'lib')))
from db_lab_benchmark import environment as host_environment


def save(native, status, output_dir, image_tag, mode, brokers, engine, engine_version,
         runtime_observation=None):
    run_id = native.get('run_id', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', run_id):
        raise ValueError('Kafka result has an invalid run ID')
    if status not in ('PASS', 'FAIL'):
        raise ValueError('Invalid workload status')
    counts = {key: native.get(key, 0) for key in ('queued', 'delivered', 'failed', 'pending', 'logical_bytes')}
    if any(not isinstance(value, int) or value < 0 for value in counts.values()):
        raise ValueError('Invalid Kafka workload counters')
    finished = datetime.now(timezone.utc)
    temporary_topic = bool(re.fullmatch(r'lab\.benchmark\.[a-f0-9]{32}', str(native.get('topic', ''))))
    cleanup_verified = native.get('benchmark_cleanup_verified') is True and temporary_topic
    dataset_verified = (status == 'PASS' and native.get('benchmark_dataset_state_verified') is True
                        and cleanup_verified)
    try:
        revision = subprocess.run(['git', '-C', str(ROOT.parent), 'rev-parse', 'HEAD'],
                                  capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        dirty = bool(subprocess.run(['git', '-C', str(ROOT.parent), 'status', '--porcelain'],
                                    capture_output=True, text=True, timeout=5, check=True).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        revision, dirty = None, None
    report = {
        'schema_version': 1, 'run_id': run_id, 'status': status,
        'evidence_kind': 'live_database',
        'started_utc': native.get('started_utc'), 'finished_utc': finished.isoformat(),
        'duration_seconds': native.get('elapsed_seconds'),
        'database': {'product': 'Kafka', 'configured_image_tag': image_tag,
                     'mode': mode, 'broker_count': brokers},
        'workload': {'name': 'synthetic-produce', 'parameters': native.get('parameters', {})},
        'dataset': {'topic': native.get('topic'), 'temporary_topic': temporary_topic,
                    'partition_count': native.get('benchmark_topic_partitions'),
                    'isolated_and_deleted': dataset_verified},
        'environment': {**host_environment(engine,[f'{os.environ.get("LAB_NAME","kzk-lab")}-kafka{i}'
                                                   for i in range(1,brokers+1)]),
                        'revision': revision, 'source_dirty': dirty,
                        'container_engine': engine, 'container_engine_version': engine_version,
                        'client_library_version': native.get('client_library_version'),
                        'runtime_observation': runtime_observation},
        'metrics': {'messages_queued': counts['queued'], 'messages_delivered': counts['delivered'],
                    'messages_failed': counts['failed'], 'pending': counts['pending'],
                    'logical_bytes': counts['logical_bytes'],
                    'delivered_per_second': native.get('delivered_per_second'),
                    'ack_latency_p95_ms_last_10000': native.get('ack_latency_p95_ms_last_10000'),
                    'errors': native.get('errors', {})},
        'verification': {'all_queued_delivered': status == 'PASS' and counts['pending'] == 0
                         and counts['failed'] == 0 and counts['delivered'] == counts['queued'],
                         'benchmark_topic_cleanup_verified': cleanup_verified,
                         'dataset_state_verified': dataset_verified},
        'note': 'Kafka benchmark uses an empty run-scoped topic, verifies delivery against its offset span, then deletes that topic. Not production capacity evidence.'}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / (run_id + '.json')
    fd, temporary = tempfile.mkstemp(prefix='.benchmark-', dir=output_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', choices=('PASS', 'FAIL'), required=True)
    parser.add_argument('--image-tag', required=True)
    parser.add_argument('--mode', choices=('zk', 'kraft'), required=True)
    parser.add_argument('--brokers', type=int, required=True)
    parser.add_argument('--engine', choices=('docker', 'podman'), required=True)
    parser.add_argument('--engine-version', default='unknown')
    parser.add_argument('--runtime-observation-file', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=ROOT/'reports'/'benchmarks')
    args = parser.parse_args()
    native = json.load(sys.stdin)
    observation = json.loads(args.runtime_observation_file.read_text(encoding='utf-8'))
    if not isinstance(observation, dict):
        parser.error('runtime observation must be a JSON object')
    target, report = save(native, args.status, args.output_dir, args.image_tag,
                          args.mode, args.brokers, args.engine, args.engine_version, observation)
    print(json.dumps({'report': str(target), 'run_id': report['run_id'], 'status': report['status']}))


if __name__ == '__main__':
    main()
