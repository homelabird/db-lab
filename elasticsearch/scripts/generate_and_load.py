#!/usr/bin/env python3
"""Generate realistic synthetic records on demand; stream to Bulk API by default."""
import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lablib import APIError, ESClient, INDICES, LAYOUT, ROOT, bulk_send, compact, write_json

GENERATOR_VERSION = 'seed-v3-complex'
DEFAULT_START = '2026-08-01T00:00:00Z'


def parse_start(text):
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('--start-date must include Z or a timezone offset')
    return value.astimezone(timezone.utc)


def config_from_args(args):
    if not math.isfinite(args.size_mb) or args.size_mb < 0.01:
        raise ValueError('--size-mb must be finite and >= 0.01 (MiB)')
    if args.days < 1 or args.payload_bytes < 0 or args.payload_bytes > 1024 * 1024:
        raise ValueError('--days >= 1; 0 <= --payload-bytes <= 1048576')
    if args.batch_size < 1 or args.max_batch_mb <= 0 or not math.isfinite(args.max_batch_mb):
        raise ValueError('--batch-size and --max-batch-mb must be positive')
    if args.max_docs_per_shard is not None and args.max_docs_per_shard < 1:
        raise ValueError('--max-docs-per-shard must be >= 1')
    if args.max_source_mb_per_shard is not None and (
            args.max_source_mb_per_shard <= 0 or not math.isfinite(args.max_source_mb_per_shard)):
        raise ValueError('--max-source-mb-per-shard must be finite and > 0')
    if args.retries < 0 or args.min_nodes < 1 or args.wait_seconds <= 0 or args.timeout <= 0:
        raise ValueError('Invalid retries, min-nodes, wait-seconds or timeout')
    config = {'generator_version': GENERATOR_VERSION, 'size_mib': args.size_mb,
              'seed': args.seed, 'start_date': parse_start(args.start_date).isoformat(),
              'days': args.days, 'payload_bytes': args.payload_bytes}
    config['total_target_source_bytes'] = int(args.size_mb * 1024 * 1024)
    config['max_docs_per_shard'] = args.max_docs_per_shard
    config['max_source_bytes_per_shard'] = (
        int(args.max_source_mb_per_shard * 1024 * 1024)
        if args.max_source_mb_per_shard is not None else None)
    config['layout'] = tuned_layout(config)
    return config


def layout_for(config, index):
    return config.get('layout', LAYOUT)[index]


def tuned_layout(config):
    """Increase primary shards so configured per-shard ceilings can be met."""
    layout = {index: list(LAYOUT[index]) for index in INDICES}
    if config.get('max_docs_per_shard') is None and config.get('max_source_bytes_per_shard') is None:
        return {index: tuple(values) for index, values in layout.items()}
    for index in INDICES:
        budget = budget_for(config, index)
        required = 1
        if config.get('max_source_bytes_per_shard'):
            required = max(required, math.ceil(budget / config['max_source_bytes_per_shard']))
        if config.get('max_docs_per_shard'):
            sample = list(documents(config, index))[:100]
            average = sum(len(source) + 1 for _, source in sample) / len(sample)
            required = max(required, math.ceil(math.ceil(budget / average) / config['max_docs_per_shard']))
        layout[index][0] = max(layout[index][0], required)
    return {index: tuple(values) for index, values in layout.items()}


def budget_for(config, index):
    total = config['total_target_source_bytes']
    if index == INDICES[2]:
        return total - total * 50 // 100 - total * 32 // 100
    return total * LAYOUT[index][2] // 100


def definition(config, index):
    mapping = json.loads((ROOT / 'mappings' / f'{index}.json').read_text(encoding='utf-8'))
    metadata = {'config': config, 'source_budget_bytes': budget_for(config, index),
                'index': index, 'shards': layout_for(config, index)[0], 'replicas': layout_for(config, index)[1],
                'mapping_sha256': hashlib.sha256(compact(mapping)).hexdigest()}
    signature = hashlib.sha256(compact(metadata)).hexdigest()
    mapping['_meta'] = {'seed_lab': {'signature': signature, **metadata}}
    return {'settings': {'number_of_shards': layout_for(config, index)[0], 'number_of_replicas': layout_for(config, index)[1],
                         'refresh_interval': '1s', 'index.unassigned.node_left.delayed_timeout': '45s'},
            'mappings': mapping}


def documents(config, index):
    """Each index has an independent RNG: batch-size/retries never change the dataset."""
    salt = int.from_bytes(hashlib.sha256(f'{config["seed"]}:{index}'.encode()).digest()[:8], 'big')
    rng = random.Random(salt)
    start = parse_start(config['start_date'])
    target, produced, i = budget_for(config, index), 0, 0
    while produced < target:
        category = i % 100
        user = 'user-00042' if category < 10 else f'user-{rng.randrange(5000):05d}'
        document_id = f'{index}-{i:010d}'
        timestamp = start + timedelta(seconds=rng.randrange(config['days'] * 86400))
        doc = {'@timestamp': timestamp.isoformat().replace('+00:00', 'Z'),
               'document_id': document_id, 'event_seq': i, 'user_id': user,
               'trace_id': f'trace-{i:010d}', 'src_ip': f'10.{rng.randrange(10,14)}.{rng.randrange(8)}.{rng.randrange(1,255)}',
               'tags': ['synthetic', 'training'], 'scenario': 'normal'}
        if index == INDICES[0]:
            high = category < 5
            risk = rng.randrange(850, 1001) if high else rng.randrange(0, 750)
            doc.update(transaction_id=f'TX{i:010d}', device_id=f'dev-{rng.randrange(12000):06d}',
                       institution=rng.choice(['alpha-card', 'beta-bank', 'gamma-pay', 'delta-life']),
                       channel='APP' if high else rng.choice(['WEB', 'APP', 'ATM', 'ARS']),
                       amount=rng.randrange(750000, 3000000) if high else rng.randrange(1000, 450000),
                       currency='KRW' if high or rng.random() < .9 else 'USD',
                       country=rng.choice(['US', 'JP']) if high else rng.choice(['KR'] * 8 + ['JP', 'US']),
                       merchant_category='crypto' if high else rng.choice(['travel', 'food', 'game', 'market', 'fashion', 'utility']),
                       risk_score=risk, is_fraud=high, decision='BLOCK' if high else 'APPROVE',
                       message='suspicious payment blocked after device change' if high else 'payment approved after risk evaluation')
            if high:
                doc.update(scenario='high-risk-payment', rule_id='RULE-DEVICE-VELOCITY',
                           tags=['synthetic', 'training', 'fraud', 'device_change'])
        elif index == INDICES[1]:
            failure = category < 5
            doc.update(request_id=f'REQ{i:010d}', method='POST' if failure else rng.choice(['GET', 'POST', 'PUT', 'DELETE']),
                       path='/api/pay' if failure else rng.choice(['/login', '/api/user', '/search', '/checkout', '/health']),
                       status=502 if failure else rng.choices([200, 201, 204, 301, 400, 401, 403, 404, 429], [65,4,4,2,4,5,4,8,4])[0],
                       latency_ms=rng.randrange(1500, 8000) if failure else rng.randrange(5, 800),
                       bytes=rng.randrange(100, 200000),
                       user_agent=rng.choice(['Chrome', 'Firefox', 'Edge', 'MobileApp/6.2', 'curl/8']),
                       service='payment' if failure else rng.choice(['gateway', 'auth', 'payment', 'user', 'search']),
                       message='payment gateway timeout while contacting upstream' if failure else 'request completed by application service')
            if failure:
                doc.update(error_code='UPSTREAM_TIMEOUT', scenario='payment-timeout')
        elif index == INDICES[2]:
            action = 'LOGIN' if category < 5 else ('ROLE_CHANGE' if category < 10 else rng.choice(['LOGIN', 'LOGOUT', 'ROLE_CHANGE', 'CONFIG_READ', 'CONFIG_WRITE', 'EXPORT', 'DELETE']))
            result = 'FAIL' if category < 5 else rng.choices(['SUCCESS', 'FAIL'], [95, 5])[0]
            privileged = action in ('ROLE_CHANGE', 'CONFIG_WRITE', 'DELETE')
            message = ('failed login from unfamiliar device' if category < 5 else
                       'administrator role changed after approval' if category < 10 else
                       f'audit {action.lower()} operation finished with {result.lower()}')
            doc.update(event_id=f'AUD{i:010d}', actor=user, action=action, result=result,
                       target=rng.choice(['customer', 'account', 'policy', 'server', 'index', 'dashboard']),
                       privileged=privileged, message=message)
            if category < 5:
                doc.update(scenario='failed-login', error_code='AUTH_FAILED')
            elif category < 10:
                doc['scenario'] = 'privileged-change'
        elif index == INDICES[3]:
            doc.update(order_id=f'ORD{i:010d}', customer_id=f'cust-{rng.randrange(20000):06d}',
                       order_status=rng.choice(['created', 'paid', 'packed', 'shipped', 'returned']),
                       total_amount=round(rng.uniform(10, 2500), 2),
                       shipping={'country': rng.choice(['KR', 'JP', 'US']),
                                 'postal_code': f'{rng.randrange(10000, 99999)}',
                                 'geo': {'lat': round(rng.uniform(33, 38), 5),
                                         'lon': round(rng.uniform(126, 130), 5)}},
                       items=[{'sku': f'SKU-{rng.randrange(1000):04d}',
                               'quantity': rng.randrange(1, 5),
                               'price': round(rng.uniform(3, 400), 2)}
                              for _ in range(1 + i % 3)],
                       payment={'method': rng.choice(['card', 'wallet', 'bank']),
                                'masked_pan': f'****{rng.randrange(1000,9999)}'},
                       promotion={'code': 'WELCOME10' if i % 11 == 0 else None,
                                  'discount': round(rng.uniform(0, 100), 2)})
            if i % 17 == 0:
                doc['scenario'] = 'multi-item-order'
        else:
            doc.update(metric_name=rng.choice(['jvm.gc.pause', 'http.request.duration',
                                               'db.pool.active', 'queue.depth']),
                       metric_value=round(rng.uniform(0, 1000), 4),
                       host={'name': f'app-{rng.randrange(1, 8):02d}',
                             'ip': f'10.20.{rng.randrange(1, 4)}.{rng.randrange(1, 255)}'},
                       service={'name': rng.choice(['checkout', 'catalog', 'search']),
                                'version': rng.choice(['1.4.2', '1.5.0', '2.0.0'])},
                       labels={'env': rng.choice(['dev', 'stage', 'prod']),
                               'region': rng.choice(['ap-northeast-2', 'us-east-1'])},
                       trace={'span_id': f'span-{i:012d}', 'sampled': i % 3 != 0},
                       histogram={'count': rng.randrange(1, 1000),
                                  'sum': round(rng.uniform(1, 10000), 2),
                                  'bounds': [10, 50, 100, 500, 1000]})
            if i % 29 == 0:
                doc.update(scenario='anomaly-spike', alert={'severity': 'high',
                                                            'rule': 'latency-p99'})
        if config['payload_bytes']:
            # Synthetic opaque payload, not a real token/secret. Stored in _source, not indexed.
            doc['payload'] = ''.join(hashlib.sha256(f'{salt}:{i}:{j}'.encode()).hexdigest()
                                     for j in range((config['payload_bytes'] + 63) // 64))[:config['payload_bytes']]
        source = compact(doc)
        action = compact({'index': {'_index': index, '_id': document_id}})
        produced += len(source) + 1
        yield action, source
        i += 1


def prepare_indices(client, config, recreate=False):
    """Preflight every index before deleting/creating anything. Never use wildcard DELETE."""
    definitions = {index: definition(config, index) for index in INDICES}
    existing = {}
    for index in INDICES:
        try:
            existing[index] = client.request('GET', f'/{index}')
        except APIError as exc:
            if exc.status != 404:
                raise
    if not recreate:
        for index, detail in existing.items():
            record = detail[index]
            meta = record.get('mappings', {}).get('_meta', {}).get('seed_lab', {})
            expected = definitions[index]['mappings']['_meta']['seed_lab']['signature']
            if meta.get('signature') != expected:
                raise RuntimeError(f'{index} is an older/different dataset. Nothing was deleted. '
                                   'Back up as needed, then use --recreate --yes (deletes only the 5 seed indices).')
            settings = record.get('settings', {}).get('index', {})
            if int(settings.get('number_of_shards', -1)) != layout_for(config, index)[0]:
                raise RuntimeError(f'Shard layout changed for {index}; use --recreate --yes.')
    for index in INDICES:
        if recreate and index in existing:
            client.request('DELETE', f'/{index}')
        if recreate or index not in existing:
            response = client.request('PUT', f'/{index}', definitions[index])
            if not response or not response.get('acknowledged'):
                raise RuntimeError(f'Index creation was not acknowledged: {index}. Inspect cluster state before retry.')
    return definitions


def seed_index(client, config, index, args, output=None):
    pending, pending_bytes, count, source_bytes, wire_bytes, max_source = [], 0, 0, 0, 0, 0
    limit = int(args.max_batch_mb * 1024 * 1024)
    previous_refresh = None
    if client:
        settings = client.request('GET', f'/{index}/_settings')[index]['settings']['index']
        previous_refresh = settings.get('refresh_interval', '1s')
    try:
        if client:
            client.request('PUT', f'/{index}/_settings', {'index': {'refresh_interval': '-1'}})
        for action, source in documents(config, index):
            size = len(action) + len(source) + 2
            if size > limit:
                raise ValueError(f'One document is larger than --max-batch-mb in {index}')
            if client and pending and (len(pending) >= args.batch_size or pending_bytes + size > limit):
                bulk_send(client, pending, args.retries)
                pending, pending_bytes = [], 0
            if client:
                pending.append((action, source))
                pending_bytes += size
            if output:
                output.write(action + b'\n' + source + b'\n')
            count += 1
            source_bytes += len(source) + 1
            wire_bytes += size
            max_source = max(max_source, len(source) + 1)
            if count % 10000 == 0:
                print(f'[{index}] generated={count:,} source={source_bytes / 1048576:.2f} MiB', flush=True)
        if client and pending:
            bulk_send(client, pending, args.retries)
    finally:
        if client and previous_refresh is not None:
            # Do not mask an earlier bulk exception if restoring settings also fails.
            original_error = sys.exc_info()[0] is not None
            try:
                client.request('PUT', f'/{index}/_settings', {'index': {'refresh_interval': previous_refresh}})
            except Exception as restore_error:
                if not original_error:
                    raise
                print(f'[WARNING] Restore refresh_interval={previous_refresh} manually for {index}: {restore_error}', file=sys.stderr)
    result = {'documents': count, 'target_source_bytes': budget_for(config, index),
              'source_bytes': source_bytes, 'bulk_wire_bytes': wire_bytes, 'largest_source_line_bytes': max_source,
              'primary_shards': layout_for(config, index)[0], 'replicas': layout_for(config, index)[1],
              'first_document_id': f'{index}-0000000000',
              'signature': definition(config, index)['mappings']['_meta']['seed_lab']['signature']}
    if client:
        refresh = client.request('POST', f'/{index}/_refresh')
        if refresh.get('_shards', {}).get('failed', 0):
            raise RuntimeError(f'Refresh failed for {index}: {refresh}')
        actual = client.request('GET', f'/{index}/_count')['count']
        result['actual_count'] = actual
        if actual != count:
            raise RuntimeError(f'{index}: expected {count} documents, found {actual}. '
                               'Stop live writers / remove extra practice docs, or explicitly recreate the seed indices.')
    print(f'[{index}] complete: {count:,} docs, source={source_bytes / 1048576:.3f} MiB', flush=True)
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--size-mb', type=float, default=float(os.getenv('SEED_SIZE_MB', '100')), help='Raw JSON _source size in MiB; default 100, not Lucene store size')
    p.add_argument('--seed', type=int, default=int(os.getenv('SEED', '42')),
                  help='Deterministic random seed; same settings produce the same IDs and records')
    p.add_argument('--start-date', default=os.getenv('SEED_START_DATE', DEFAULT_START),
                  help='UTC start timestamp, including Z or an offset (default: 2026-08-01T00:00:00Z)')
    p.add_argument('--days', type=int, default=31,
                   help='Number of calendar days used for synthetic @timestamp values (default: 31)')
    p.add_argument('--payload-bytes', type=int, default=256,
                   help='Extra non-indexed synthetic _source payload per document (default: 256)')
    p.add_argument('--max-docs-per-shard', type=int, default=None,
                   help='Tune primary shard count so each shard targets at most this many documents')
    p.add_argument('--max-source-mb-per-shard', type=float, default=None,
                   help='Tune primary shard count so each shard targets at most this MiB of generated _source')
    p.add_argument('--batch-size', type=int, default=int(os.getenv('BULK_SIZE', '500')),
                   help='Maximum documents per Bulk request (default: 500)')
    p.add_argument('--max-batch-mb', type=float, default=4,
                   help='Maximum Bulk request size in MiB (default: 4)')
    p.add_argument('--retries', type=int, default=4,
                   help='Retries for transient Bulk responses 429/502/503/504 (default: 4)')
    p.add_argument('--timeout', type=float, default=120,
                   help='Elasticsearch HTTP request timeout in seconds (default: 120)')
    p.add_argument('--min-nodes', type=int, default=5,
                   help='Minimum data nodes required before loading (default: 5)')
    p.add_argument('--wait-seconds', type=float, default=300,
                   help='Maximum cluster wait time in seconds (default: 300)')
    p.add_argument('--recreate', action='store_true',
                   help='Delete and recreate only the 5 known seed indices')
    p.add_argument('--yes', action='store_true',
                   help='Explicitly acknowledge the data deletion requested by --recreate')
    p.add_argument('--generate-only', action='store_true',
                   help='Write Bulk NDJSON locally without connecting to Elasticsearch')
    p.add_argument('--output-dir', type=Path, default=ROOT / 'datasets/generated',
                   help='Directory for --generate-only files')
    p.add_argument('--report', type=Path, default=ROOT / 'reports/seed-manifest.json',
                   help='Manifest JSON output path (default: reports/seed-manifest.json)')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.recreate and not args.yes:
        p.error('--recreate requires --yes; it deletes the 5 seed indices and their practice data')
    if args.generate_only and args.recreate:
        p.error('--generate-only cannot be combined with --recreate')
    config = config_from_args(args)
    started = time.monotonic()
    manifest = {'status': 'generating' if args.generate_only else 'loading', 'config': config, 'indices': {},
                'size_definition': 'UTF-8 compact JSON _source lines including LF; excludes bulk action metadata and replicas'}
    client = None
    report = args.report
    if args.generate_only:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        report = args.output_dir / 'manifest.json'
    else:
        client = ESClient(timeout=args.timeout)
        print(f'[connect] {client.url}; wait for >= {args.min_nodes} nodes', flush=True)
        client.wait(args.min_nodes, args.wait_seconds, require_yellow=True)
        info = client.assert_lab()
        manifest['cluster_uuid'] = info['cluster_uuid']
        manifest['cluster_name'] = info['cluster_name']
        prepare_indices(client, config, args.recreate)
    write_json(report, manifest)
    try:
        for index in INDICES:
            if args.generate_only:
                path = args.output_dir / f'{index}.bulk.ndjson'
                temp = path.with_suffix(path.suffix + '.partial')
                with temp.open('wb') as output:
                    result = seed_index(None, config, index, args, output)
                temp.replace(path)
            else:
                result = seed_index(client, config, index, args)
            manifest['indices'][index] = result
            write_json(report, manifest)
        manifest.update(status='generated' if args.generate_only else 'loaded',
                        source_bytes=sum(item['source_bytes'] for item in manifest['indices'].values()),
                        documents=sum(item['documents'] for item in manifest['indices'].values()),
                        elapsed_seconds=round(time.monotonic() - started, 3))
        if client:
            stats = client.request('GET', '/' + ','.join(INDICES) + '/_stats/store')
            manifest['primary_store_bytes'] = stats['_all']['primaries']['store']['size_in_bytes']
            manifest['total_store_bytes_including_replicas'] = stats['_all']['total']['store']['size_in_bytes']
        write_json(report, manifest)
    except BaseException as exc:
        manifest.update(status='failed', error=str(exc) or type(exc).__name__)
        write_json(report, manifest)
        raise
    print(f'[DONE] {manifest["documents"]:,} docs / {manifest["source_bytes"] / 1048576:.3f} MiB source')
    print(f'[report] {report}')
    if client:
        print('Run ./lab.sh verify for counts, mappings, shard layout and all query examples.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (APIError, RuntimeError, ValueError, OSError, urllib.error.URLError) as exc:
        sys.exit(f'[error] {exc}')
    except KeyboardInterrupt:
        sys.exit('[interrupted] Completed bulk requests remain. Rerun the same command to overwrite the same IDs safely.')
