#!/usr/bin/env python3
"""Redis Sentinel lab client. All traffic stays on the lab bridge network."""
from __future__ import annotations
import argparse
import json
import os
import random
import signal
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from redis.sentinel import Sentinel
from core import endpoint_list, safe_run_id, payload_for, classify_error, verification_bucket, read_run

PASSWORD = os.environ.get('REDIS_PASSWORD', '')
SENTINEL_PASSWORD = os.environ.get('SENTINEL_PASSWORD', '')
MASTER_NAME = os.environ.get('MASTER_NAME', 'mymaster')
DEPLOYMENT_MODE = os.environ.get('DEPLOYMENT_MODE', 'sentinel')
CLUSTER_NODE_COUNT = int(os.environ.get('CLUSTER_NODE_COUNT', '6'))
NODES = endpoint_list(os.environ.get('REDIS_NODES', '10.89.77.11:6379,10.89.77.12:6379,10.89.77.13:6379'))
CLUSTER_NODES = endpoint_list(os.environ.get('CLUSTER_NODES', os.environ.get('REDIS_NODES', '10.89.77.11:6379,10.89.77.12:6379,10.89.77.13:6379')))[:CLUSTER_NODE_COUNT]
SENTINELS = endpoint_list(os.environ.get('SENTINEL_NODES', '10.89.77.21:26379,10.89.77.22:26379,10.89.77.23:26379'))
RESULTS = Path(os.environ.get('RESULTS_DIR', '/results'))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def clean_error(exc: Exception) -> str:
    text = f'{type(exc).__name__}: {exc}'
    for secret in (PASSWORD, SENTINEL_PASSWORD):
        if secret:
            text = text.replace(secret, '<redacted>')
    return text[:600]


def kwargs(password: str = PASSWORD) -> dict:
    return dict(password=password, decode_responses=True,
                socket_connect_timeout=1.0, socket_timeout=1.5,
                retry=Retry(NoBackoff(), 0), retry_on_error=[],
                health_check_interval=0)


def direct(endpoint: tuple[str, int], password: str = PASSWORD) -> redis.Redis:
    return redis.Redis(host=endpoint[0], port=endpoint[1], **kwargs(password))


def sentinel() -> Sentinel:
    return Sentinel(SENTINELS, sentinel_kwargs=kwargs(SENTINEL_PASSWORD), **kwargs())


def master_client() -> redis.Redis:
    if DEPLOYMENT_MODE == 'cluster':
        from redis.cluster import ClusterNode, RedisCluster
        return RedisCluster(
            startup_nodes=[ClusterNode(host, port) for host, port in CLUSTER_NODES],
            password=PASSWORD, **kwargs())
    return sentinel().master_for(MASTER_NAME)


def node_name(address: str) -> str:
    for i, (host, _) in enumerate(NODES, 1):
        if address in (host, f'redis-{i}'):
            return f'redis-{i}'
    raise ValueError(f'Sentinel returned a non-lab address: {address!r}')


def current_master() -> tuple[str, int]:
    if DEPLOYMENT_MODE == 'cluster':
        raise RuntimeError('Cluster mode has multiple primaries; use status to inspect shard primaries.')
    host, port = sentinel().discover_master(MASTER_NAME)
    node_name(host)
    if port != 6379:
        raise ValueError('Unexpected master port')
    with direct((host, port)) as connection:
        if connection.role()[0] != 'master':
            raise RuntimeError('Sentinel view is changing; discovered node is not currently master')
    return host, port


def snapshot() -> dict:
    if DEPLOYMENT_MODE == 'cluster':
        result = {'time_utc': now(), 'mode': 'cluster', 'redis': {}, 'cluster': {}}
        for i, endpoint in enumerate(CLUSTER_NODES, 1):
            name = f'redis-cluster-{i}'
            try:
                with direct(endpoint) as connection:
                    info = connection.info()
                    result['redis'][name] = {'reachable': True, 'address': endpoint[0],
                        **{k: info.get(k) for k in ('role', 'master_host', 'master_link_status',
                           'used_memory_human', 'used_memory', 'connected_clients',
                           'rejected_connections', 'aof_enabled')},
                        'dbsize': connection.dbsize()}
                    cluster_info = connection.execute_command('CLUSTER', 'INFO')
                    if isinstance(cluster_info, dict):
                        result['cluster'][name] = cluster_info
                    else:
                        result['cluster'][name] = {}
            except (redis.RedisError, OSError) as exc:
                result['redis'][name] = {'reachable': False, 'error': clean_error(exc)}
        return result
    result = {'time_utc': now(), 'redis': {}, 'sentinel': {}}
    for i, endpoint in enumerate(NODES, 1):
        name = f'redis-{i}'
        try:
            with direct(endpoint) as connection:
                info = connection.info()
                result['redis'][name] = {'reachable': True, 'address': endpoint[0],
                    **{k: info.get(k) for k in (
                        'role', 'connected_slaves', 'master_host', 'master_link_status',
                        'master_sync_in_progress', 'master_repl_offset', 'slave_repl_offset',
                        'used_memory_human', 'used_memory', 'maxmemory_human', 'maxmemory',
                        'maxmemory_policy', 'mem_not_counted_for_evict', 'evicted_keys',
                        'connected_clients', 'blocked_clients', 'rejected_connections',
                        'rdb_last_bgsave_status', 'rdb_bgsave_in_progress',
                        'aof_enabled', 'aof_last_write_status', 'aof_rewrite_in_progress')},
                    'dbsize': connection.dbsize()}
        except (redis.RedisError, OSError) as exc:
            result['redis'][name] = {'reachable': False, 'error': clean_error(exc)}
    for i, endpoint in enumerate(SENTINELS, 1):
        name = f'sentinel-{i}'
        try:
            with direct(endpoint, SENTINEL_PASSWORD) as connection:
                state = connection.sentinel_master(MASTER_NAME)
                try:
                    quorum = connection.execute_command('SENTINEL', 'CKQUORUM', MASTER_NAME)
                except redis.RedisError as exc:
                    quorum = clean_error(exc)
                result['sentinel'][name] = {'reachable': True, 'master': state.get('ip'),
                    'flags': state.get('flags'), 'quorum': quorum,
                    'other_sentinels': state.get('num-other-sentinels', 0),
                    'replicas': state.get('num-slaves', 0)}
        except (redis.RedisError, OSError) as exc:
            result['sentinel'][name] = {'reachable': False, 'error': clean_error(exc)}
    return result


def topology_errors(state: dict) -> list[str]:
    if DEPLOYMENT_MODE == 'cluster':
        errors = []
        for name, data in state['redis'].items():
            if not data.get('reachable'):
                errors.append(f'{name}: unreachable')
        infos = [data for data in state.get('cluster', {}).values() if data]
        if not infos:
            return errors + ['No cluster info available']
        healthy = [info for info in infos if info.get('cluster_state') == 'ok']
        if len(healthy) != len(infos):
            errors.append('Cluster state is not ok on every reachable node')
        if any(info.get('cluster_slots_assigned') != '16384' or info.get('cluster_slots_ok') != '16384'
               for info in healthy):
            errors.append('Cluster does not cover all 16384 slots')
        if any(int(info.get('cluster_slots_fail', 0)) or int(info.get('cluster_slots_pfail', 0))
               for info in healthy):
            errors.append('Cluster has failed or pfail slots')
        return errors
    errors = []
    masters = [(name, d) for name, d in state['redis'].items() if d.get('role') == 'master']
    if len(masters) != 1:
        errors.append(f'Master count={len(masters)} (expected 1)')
    primary = masters[0][1].get('address') if len(masters) == 1 else None
    for name, data in state['redis'].items():
        if not data.get('reachable'):
            errors.append(f'{name}: unreachable')
        elif data.get('role') == 'slave':
            if data.get('master_link_status') != 'up' or data.get('master_host') != primary:
                errors.append(f'{name}: replication not linked to current Master')
            if data.get('master_sync_in_progress'):
                errors.append(f'{name}: synchronization in progress')
        elif data.get('role') == 'master' and data.get('connected_slaves', 0) < 2:
            errors.append(f'{name}: fewer than two connected replicas')
        elif data.get('role') not in ('master', 'slave'):
            errors.append(f'{name}: unexpected role')
    for name, data in state['sentinel'].items():
        if not data.get('reachable'):
            errors.append(f'{name}: unreachable')
            continue
        if data.get('master') != primary:
            errors.append(f'{name}: master view not converged')
        if not str(data.get('quorum', '')).startswith('OK'):
            errors.append(f'{name}: CKQUORUM failed')
        if int(data.get('other_sentinels', 0)) < 2 or int(data.get('replicas', 0)) < 2:
            errors.append(f'{name}: discovery not complete')
        if any(flag in str(data.get('flags', '')) for flag in ('s_down', 'o_down', 'disconnected', 'failover_in_progress')):
            errors.append(f'{name}: failure flags still present')
    return errors


def print_status(state: dict) -> None:
    print(f"UTC {state['time_utc']}")
    print(f"{'NODE':<13} {'ROLE':<9} {'LINK/MASTER':<26} {'MEMORY':<12} {'KEYS':>8}")
    for name, data in state['redis'].items():
        if not data.get('reachable'):
            print(f"{name:<13} DOWN      {data.get('error', '')}")
            continue
        link = (f"{data.get('master_link_status')}/{data.get('master_host')}" if data['role'] == 'slave'
                else f"replicas={data.get('connected_slaves')}")
        print(f"{name:<13} {data['role']:<9} {link:<26} {str(data.get('used_memory_human')):<12} {data['dbsize']:>8}")
    for name, data in state.get('sentinel', {}).items():
        if not data.get('reachable'):
            print(f"{name:<13} DOWN {data.get('error', '')}")
        else:
            print(f"{name:<13} master={data.get('master')} flags={data.get('flags')} peers={data.get('other_sentinels')} quorum={data.get('quorum')}")
    errors = topology_errors(state)
    print('FULL TOPOLOGY: ' + ('PASS' if not errors else 'NOT READY / DEGRADED'))
    for error in errors:
        print('  - ' + error)


def wait_ready(timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        state = snapshot()
        errors = topology_errors(state)
        if not errors:
            try:
                with master_client().client() as connection:
                    key = f'lab:health:{uuid.uuid4().hex}'
                    connection.set(key, 'ok', ex=60)
                    required_replicas = 1 if DEPLOYMENT_MODE == 'cluster' else 2
                    acknowledged = connection.wait(required_replicas, 1000)
                    if connection.get(key) == 'ok' and acknowledged >= required_replicas:
                        print_status(state)
                        print(f'PASS: authenticated write/read and WAIT {required_replicas} on the same connection')
                        return
                    errors = ['Write/read/WAIT 2 verification not complete']
            except (redis.RedisError, OSError) as exc:
                errors = [clean_error(exc)]
        message = '; '.join(errors)
        if message != last:
            print('Waiting: ' + message, flush=True)
            last = message
        time.sleep(2)
    raise RuntimeError(f'Readiness timeout ({timeout}s). Use ./lab.sh status and ./lab.sh logs NODE.')


SEED_PROFILES = ('users', 'sessions', 'catalog', 'events', 'geo', 'counters')


def parse_mix(raw: str) -> dict[str, float]:
    result = {}
    for item in raw.split(','):
        name, value = item.split(':', 1)
        if name not in ('read', 'write', 'delete') or name in result:
            raise ValueError('mix must use read,write,delete exactly once at most')
        result[name] = float(value)
    if not result or any(value < 0 for value in result.values()) or sum(result.values()) <= 0:
        raise ValueError('mix values must be non-negative and have a positive total')
    total = sum(result.values())
    return {name: value / total for name, value in result.items()}


def choose_operation(rng: random.Random, mix: dict[str, float]) -> str:
    point = rng.random()
    cumulative = 0.0
    for name, weight in mix.items():
        cumulative += weight
        if point < cumulative:
            return name
    return next(reversed(mix))


def _seed_batch(connection, profile: str, start: int, count: int, payload_bytes: int,
                rng: random.Random) -> int:
    with connection.pipeline(transaction=False) as pipe:
        for number in range(start, start + count):
            tag = f'{{{number}}}'
            if profile == 'users':
                pipe.hset(f'lab:user:{tag}', mapping={
                    'id': str(number), 'name': f'user-{number:08d}',
                    'email': f'user-{number:08d}@example.test',
                    'tier': ('basic', 'silver', 'gold', 'platinum')[number % 4],
                    'risk': str((number * 17) % 100),
                    'payload': 'x' * payload_bytes})
                pipe.sadd(f'lab:users:active:{tag}', str(number))
            elif profile == 'sessions':
                pipe.hset(f'lab:session:{tag}', mapping={
                    'user_id': str(number), 'token': f'token-{number:012d}',
                    'device': ('web', 'ios', 'android')[number % 3],
                    'ip': f'10.20.{number % 250}.{number % 251 + 1}'})
                pipe.expire(f'lab:session:{tag}', 900 + number % 1800)
            elif profile == 'catalog':
                pipe.hset(f'lab:product:{tag}', mapping={
                    'sku': f'SKU-{number:08d}', 'category': ('book', 'home', 'tech', 'sport')[number % 4],
                    'price_cents': str(499 + (number * 37) % 250000),
                    'stock': str((number * 13) % 500), 'payload': 'x' * min(payload_bytes, 2048)})
                pipe.zadd('lab:catalog:popular', {str(number): (number * 31) % 100000})
            elif profile == 'events':
                pipe.xadd(f'lab:events:{tag}', {
                    'event_id': uuid.uuid4().hex, 'user_id': str(number),
                    'kind': ('view', 'login', 'purchase', 'logout')[number % 4],
                    'amount': str((number * 97) % 100000)}, maxlen=100, approximate=True)
            elif profile == 'geo':
                pipe.geoadd('lab:locations', (127.0 + (number % 1000) / 10000,
                    36.0 + (number % 700) / 10000, f'location-{number}'))
            elif profile == 'counters':
                pipe.hincrby('lab:metrics:requests', f'endpoint-{number % 20}', 1 + number % 7)
                pipe.hincrby('lab:metrics:errors', f'code-{(number % 5) * 100}', number % 3)
            if number % 100 == 0:
                pipe.execute()
        pipe.execute()
    return count


def seed(users: int, payload_bytes: int, profiles: str = 'users') -> None:
    if not 1 <= users <= 100000 or not 0 <= payload_bytes <= 8192:
        raise ValueError('records=1..100000, payload-bytes=0..8192')
    selected = tuple(dict.fromkeys(p.strip() for p in profiles.split(',') if p.strip()))
    if not selected or any(p not in SEED_PROFILES for p in selected):
        raise ValueError('profiles must contain only: ' + ','.join(SEED_PROFILES))
    if len(selected) * users * (payload_bytes + 600) > 128 * 1024 * 1024:
        raise ValueError('Seed estimate exceeds the 128 MiB lab safety cap; reduce records or payload.')
    rng = random.Random(20260918)
    started = time.monotonic()
    counts = {}
    with master_client() as connection:
        for profile in selected:
            counts[profile] = _seed_batch(connection, profile, 1, users, payload_bytes, rng)
            print(f'Seed {profile}: {users} records', flush=True)
        connection.hset('lab:seed:last', mapping={
            'profiles': ','.join(selected), 'records': str(users),
            'time_utc': now(), 'mode': 'batch'})
        result = {'profiles': selected, 'records_per_profile': users, 'counts': counts,
                  'dbsize': connection.dbsize(), 'elapsed_seconds': round(time.monotonic() - started, 3)}
        print(json.dumps(result, ensure_ascii=False))


def simulate_seed(seconds: int, interval: float, rate: float, profiles: str,
                  payload_bytes: int, run_id: str | None, mix_raw: str = 'read:0,write:1,delete:0',
                  hotset: int = 0, jitter: float = 0, ttl: int = 0) -> None:
    if not 1 <= seconds <= 86400 or not 0.1 <= interval <= 3600 or not 0.1 <= rate <= 1000:
        raise ValueError('seconds=1..86400, interval=0.1..3600, rate=0.1..1000')
    if not 0 <= payload_bytes <= 8192:
        raise ValueError('payload-bytes=0..8192')
    if not 0 <= hotset <= 1000000 or not 0 <= jitter <= 0.9 or not 0 <= ttl <= 86400:
        raise ValueError('hotset=0..1000000, jitter=0..0.9, ttl=0..86400')
    selected = tuple(dict.fromkeys(p.strip() for p in profiles.split(',') if p.strip()))
    if not selected or any(p not in SEED_PROFILES for p in selected):
        raise ValueError('profiles must contain only: ' + ','.join(SEED_PROFILES))
    mix = parse_mix(mix_raw)
    RESULTS.mkdir(parents=True, exist_ok=True)
    run_id = safe_run_id(run_id) if run_id else datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    path = RESULTS / f'{run_id}-seed-simulation.jsonl'
    if path.exists():
        raise ValueError('Run ID already exists; choose a new --run-id.')
    stop = False
    def stopping(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    deadline = time.monotonic() + seconds
    sequence = 0
    record = 0
    totals = Counter()
    rng = random.Random(20260918)
    with master_client() as connection, path.open('x', encoding='utf-8', buffering=1) as stream:
        while time.monotonic() < deadline and not stop:
            sequence += 1
            batch = max(1, int(rate * interval))
            started = time.monotonic()
            counts = Counter()
            errors = Counter()
            for _ in range(batch):
                record += 1
                key_number = rng.randint(1, hotset) if hotset else record
                profile = selected[(record - 1) % len(selected)]
                operation = choose_operation(rng, mix)
                key = f'lab:sim:{profile}:{{{key_number}}}'
                try:
                    if operation == 'read':
                        connection.get(key)
                    elif operation == 'delete':
                        connection.delete(key)
                    else:
                        value = payload_for(run_id, record, payload_bytes)
                        connection.set(key, value, ex=ttl or None)
                    counts[operation] += 1
                    totals[operation] += 1
                except (redis.RedisError, OSError) as exc:
                    errors[type(exc).__name__] += 1
                    totals['errors'] += 1
            event = {'event': 'batch', 'sequence': sequence, 'time_utc': now(),
                     'records': batch, 'counts': dict(counts), 'errors': dict(errors),
                     'latency_ms': round((time.monotonic() - started) * 1000, 2),
                     'totals': dict(totals), 'hotset': hotset, 'ttl': ttl}
            stream.write(json.dumps(event) + '\n')
            print(json.dumps(event), flush=True)
            remaining = min(interval, max(0, deadline - time.monotonic()))
            if jitter:
                remaining = max(0, remaining * (1 + rng.uniform(-jitter, jitter)))
            time.sleep(remaining)
        summary = {'event': 'finish', 'time_utc': now(), 'batches': sequence,
                   'records': record, 'profiles': selected, 'mix': mix,
                   'totals': dict(totals), 'stopped': stop}
        stream.write(json.dumps(summary) + '\n')
        print(json.dumps(summary), flush=True)
    print(f'Seed simulation log: {path}')


def basic_demo() -> None:
    with master_client() as c:
        print('SET/GET:', c.set('lab:demo:string', 'hello'), c.get('lab:demo:string'))
        c.set('lab:demo:session', 'token', ex=120)
        print('TTL:', c.ttl('lab:demo:session'))
        c.hset('lab:demo:user', mapping={'name': 'tester', 'tier': 'silver'})
        print('HGETALL:', c.hgetall('lab:demo:user'))
        print('INCR:', c.incr('lab:demo:counter'))
        print('SADD:', c.sadd('lab:demo:unique', 'a', 'a', 'b'), c.smembers('lab:demo:unique'))
        c.zadd('lab:demo:rank', {'u1': 70, 'u2': 90, 'u3': 80})
        print('ZREVRANGE:', c.zrevrange('lab:demo:rank', 0, 2, withscores=True))
        print('XADD:', c.xadd('lab:demo:events', {'action': 'login'}, maxlen=100))
        print('SCAN (cursor must be iterated):', c.scan(0, match='lab:demo:*', count=100))


def workload(args) -> None:
    if not 1 <= args.seconds <= 3600 or not 0.2 <= args.rate <= 200 or not 64 <= args.size <= 8192:
        raise ValueError('seconds=1..3600, rate=0.2..200, size=64..8192')
    if args.seconds * args.rate * (args.size + 128) > 32 * 1024 * 1024:
        raise ValueError('Workload estimated key/value bytes exceed 32 MiB; reduce size/rate/time.')
    RESULTS.mkdir(parents=True, exist_ok=True)
    run_id = safe_run_id(args.run_id) if getattr(args, 'run_id', None) else (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    path = RESULTS / (run_id + '.jsonl')
    stop_file = RESULTS / (run_id + '.stop')
    if path.exists() or stop_file.exists():
        raise ValueError('Run ID already exists; choose a new --run-id.')
    (RESULTS / 'latest-run.txt').write_text(run_id + '\n')
    manager = sentinel() if DEPLOYMENT_MODE == 'sentinel' else None
    if DEPLOYMENT_MODE == 'cluster':
        c = master_client()
    elif args.mode == 'sentinel':
        c = manager.master_for(MASTER_NAME)
    else:
        c = direct(NODES[int(args.fixed_node[-1]) - 1])
    stop = False
    def stopping(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    deadline = time.monotonic() + args.seconds
    counters = Counter()
    print(f'RUN_ID={run_id} mode={args.mode} path={path}', flush=True)
    with path.open('x', encoding='utf-8', buffering=1) as stream:
        stream.write(json.dumps({'event': 'start', 'time_utc': now(), 'run_id': run_id,
            'mode': args.mode, 'fixed_node': args.fixed_node, 'wait_replicas': args.wait_replicas,
            'note': 'Requests are not automatically replayed; SET uses unique keys.'}) + '\n')
        sequence = 0
        while time.monotonic() < deadline and not stop and not stop_file.exists():
            sequence += 1
            key = f'lab:workload:{run_id}:{sequence}'
            value = payload_for(run_id, sequence, args.size)
            row = {'event': 'request', 'time_utc': now(), 'sequence': sequence,
                   'key': key, 'value': value, 'write_ack': False, 'outcome': 'uncertain'}
            started = time.monotonic()
            stage = 'connect'
            try:
                # One dedicated connection for INFO -> SET -> WAIT -> GET.
                with c.client() as connection:
                    stage = 'identify'
                    server = connection.info('server')
                    row['server_run_id'] = server.get('run_id')
                    row['server_port'] = server.get('tcp_port')
                    # Redis node identity is read on the SAME physical connection.
                    row['node_ip'] = connection.config_get('replica-announce-ip').get('replica-announce-ip')
                    stage = 'write'
                    connection.set(key, value)
                    row['write_ack'] = True
                    row['outcome'] = 'acknowledged'
                    if args.wait_replicas:
                        stage = 'wait'
                        row['replicas_acknowledged'] = connection.wait(args.wait_replicas, 750)
                    stage = 'readback'
                    row['readback_matches'] = connection.get(key) == value
            except (redis.RedisError, OSError) as exc:
                row['error'] = clean_error(exc)
                row['error_kind'] = classify_error(type(exc).__name__, str(exc))
                # Only an explicit server rejection at SET proves rejection.
                if not row['write_ack'] and stage == 'write' and isinstance(exc, redis.ResponseError):
                    row['outcome'] = 'rejected'
                elif not row['write_ack'] and stage in ('connect', 'identify'):
                    row['outcome'] = 'not-sent'
            row['stage'] = stage
            row['latency_ms'] = round((time.monotonic() - started) * 1000, 2)
            counters[row['outcome']] += 1
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            print(json.dumps({k: v for k, v in row.items() if k not in ('value', 'key', 'event')}, ensure_ascii=False), flush=True)
            time.sleep(max(0, 1 / args.rate - (time.monotonic() - started)))
        summary = {'event': 'finish', 'time_utc': now(), 'run_id': run_id, 'counts': dict(counters)}
        stream.write(json.dumps(summary) + '\n')
        print(json.dumps(summary), flush=True)
    c.close()
    stop_file.unlink(missing_ok=True)


def verify(run_id: str) -> None:
    if run_id == 'latest':
        run_id = (RESULTS / 'latest-run.txt').read_text().strip()
    run_id = safe_run_id(run_id)
    report_path = RESULTS / (run_id + '-verification.json')
    report_path.write_text(json.dumps({'run_id': run_id, 'time_utc': now(),
                                      'status': 'RUNNING', 'passed': False}) + '\n')
    try:
        _verify_run(run_id)
    except BaseException as exc:
        data = json.loads(report_path.read_text())
        data.update(status='FAIL', passed=False, error=clean_error(exc))
        report_path.write_text(json.dumps(data, indent=2) + '\n')
        raise


def _verify_run(run_id: str) -> None:
    if run_id == 'latest':
        run_id = (RESULTS / 'latest-run.txt').read_text().strip()
    path = RESULTS / (safe_run_id(run_id) + '.jsonl')
    with path.open(encoding='utf-8') as stream:
        last_nonempty = ''
        for line in stream:
            if line.strip(): last_nonempty = line
    if not last_nonempty or json.loads(last_nonempty).get('event') != 'finish':
        raise ValueError('Workload has not finished cleanly. Stop/finish it before verifying.')
    counts = Counter()
    examples = []
    with master_client() as c:
        for row in read_run(path):
            actual = c.get(row['key'])
            bucket = verification_bucket(row, actual)
            counts[bucket] += 1
            if bucket in ('acknowledged_missing_or_changed', 'rejected_but_present', 'not_sent_but_present') and len(examples) < 20:
                examples.append(row['key'])
    total = sum(counts.values())
    acknowledged = counts['acknowledged_present'] + counts['acknowledged_missing_or_changed']
    anomalies = counts['acknowledged_missing_or_changed'] + counts['rejected_but_present'] + counts['not_sent_but_present']
    passed = total > 0 and acknowledged > 0 and anomalies == 0
    result = {'run_id': run_id, 'time_utc': now(), 'passed': passed, 'status': 'PASS' if passed else 'FAIL',
              'checked_requests': total, 'acknowledged_requests': acknowledged,
              'counts': dict(counts), 'examples': examples,
              'note': 'Data check only; this is not a service-availability or loss-free-failover guarantee.'}
    (RESULTS / (run_id + '-verification.json')).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    if total == 0:
        raise RuntimeError('No requests were recorded; there is no data to verify.')
    if acknowledged == 0:
        raise RuntimeError('No write was acknowledged. The workload did not demonstrate a working service.')
    if anomalies:
        raise RuntimeError('Verification found missing/changed acknowledged data or an unexpected write.')


def sandbox_oom() -> None:
    host = os.environ['SANDBOX_HOST']
    with direct((host, 6379)) as c:
        used = c.info('memory')['used_memory']
        limit = used + 2 * 1024 * 1024
        if limit > 80 * 1024 * 1024:
            raise ValueError('Sandbox data too large. Restore/clean the sandbox before this scenario.')
        old = c.config_get('maxmemory', 'maxmemory-policy')
        # Save only the original memory policy, never credentials.
        key = 'lab:sandbox:oom:original'
        if c.exists(key):
            raise ValueError('OOM scenario is already active; run sandbox-recover first.')
        c.set(key, json.dumps(old))
        c.config_set('maxmemory-policy', 'noeviction')
        c.config_set('maxmemory', limit)
        got_oom = False
        for i in range(1024):
            try:
                c.set(f'lab:sandbox:oom:fill:{i}', 'x' * 16384)
            except redis.ResponseError as exc:
                if isinstance(exc, redis.exceptions.OutOfMemoryError) or 'OOM' in str(exc).upper():
                    print('EXPECTED WRITE REJECTION:', clean_error(exc))
                    got_oom = True
                    break
                raise
        if not got_oom:
            raise RuntimeError('OOM was not observed within the bounded 16 MiB fill; recover and inspect.')
        print('PING still works:', c.ping())
        print(json.dumps(c.info('memory'), indent=2))
        print('Recovery: ./lab.sh sandbox-recover (only scenario filler keys are removed)')


def sandbox_recover() -> None:
    with direct((os.environ['SANDBOX_HOST'], 6379)) as c:
        key = 'lab:sandbox:oom:original'
        saved = c.get(key)
        if saved:
            original = json.loads(saved)
            # Removing keys is allowed under noeviction OOM.
            for entry in c.scan_iter(match='lab:sandbox:oom:fill:*', count=100):
                c.unlink(entry)
            c.delete(key)
            c.config_set('maxmemory', original['maxmemory'])
            c.config_set('maxmemory-policy', original['maxmemory-policy'])
        print('Sandbox memory scenario settings restored.')


def marker() -> None:
    key = 'lab:backup:marker:' + uuid.uuid4().hex
    value = uuid.uuid4().hex
    with master_client().client() as c:
        c.set(key, value)
        replicas = c.wait(2, 1000)
    print(json.dumps({'marker_key': key, 'marker_value': value, 'replicas_acknowledged': replicas, 'time_utc': now()}))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('idle')
    status = sub.add_parser('status'); status.add_argument('--json', action='store_true')
    wait = sub.add_parser('wait'); wait.add_argument('--timeout', type=int, default=180)
    sub.add_parser('master')
    s = sub.add_parser('seed')
    s.add_argument('--users', '--records', dest='users', type=int, default=2000)
    s.add_argument('--payload-bytes', type=int, default=256)
    s.add_argument('--profiles', default='users',
                   help='comma-separated: users,sessions,catalog,events,geo,counters')
    sim = sub.add_parser('seed-simulate')
    sim.add_argument('--seconds', type=int, default=300)
    sim.add_argument('--interval', type=float, default=5)
    sim.add_argument('--rate', type=float, default=10)
    sim.add_argument('--payload-bytes', type=int, default=128)
    sim.add_argument('--profiles', default='events,sessions,counters')
    sim.add_argument('--run-id')
    sim.add_argument('--mix', default='read:0,write:1,delete:0',
                     help='operation weights, e.g. read:70,write:25,delete:5')
    sim.add_argument('--hotset', type=int, default=0,
                     help='number of hot keys; 0 means monotonically new keys')
    sim.add_argument('--jitter', type=float, default=0,
                     help='interval jitter ratio, 0..0.9')
    sim.add_argument('--ttl', type=int, default=0,
                     help='write TTL in seconds; 0 means persistent')
    sub.add_parser('basic-demo')
    w = sub.add_parser('workload'); w.add_argument('--seconds', type=int, default=120); w.add_argument('--rate', type=float, default=5)
    w.add_argument('--size', type=int, default=64); w.add_argument('--mode', choices=['sentinel', 'fixed'], default='sentinel')
    w.add_argument('--fixed-node', choices=['redis-1', 'redis-2', 'redis-3'], default='redis-1')
    w.add_argument('--wait-replicas', type=int, choices=[0, 1, 2], default=0)
    w.add_argument('--run-id', help='Unique ID for automation; letters, digits, underscore and hyphen only')
    v = sub.add_parser('verify'); v.add_argument('run_id', nargs='?', default='latest')
    sub.add_parser('sandbox-oom'); sub.add_parser('sandbox-recover'); sub.add_parser('marker')
    args = p.parse_args()
    if args.command == 'idle':
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        print('lab-client ready; use ./lab.sh help', flush=True)
        while True: signal.pause()
    elif args.command == 'status':
        state = snapshot()
        print(json.dumps(state, indent=2)) if args.json else print_status(state)
    elif args.command == 'wait': wait_ready(args.timeout)
    elif args.command == 'master':
        print('cluster' if DEPLOYMENT_MODE == 'cluster' else node_name(current_master()[0]))
    elif args.command == 'seed': seed(args.users, args.payload_bytes, args.profiles)
    elif args.command == 'seed-simulate':
        simulate_seed(args.seconds, args.interval, args.rate, args.profiles, args.payload_bytes, args.run_id,
                      args.mix, args.hotset, args.jitter, args.ttl)
    elif args.command == 'basic-demo': basic_demo()
    elif args.command == 'workload': workload(args)
    elif args.command == 'verify': verify(args.run_id)
    elif args.command == 'sandbox-oom': sandbox_oom()
    elif args.command == 'sandbox-recover': sandbox_recover()
    elif args.command == 'marker': marker()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print('ERROR: ' + clean_error(exc), file=sys.stderr)
        raise SystemExit(1)
