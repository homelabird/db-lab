#!/usr/bin/env python3
"""Redis Sentinel lab client. All traffic stays on the lab bridge network."""
from __future__ import annotations
import argparse
import json
import os
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
NODES = endpoint_list(os.environ.get('REDIS_NODES', '10.89.77.11:6379,10.89.77.12:6379,10.89.77.13:6379'))
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
    return sentinel().master_for(MASTER_NAME)


def node_name(address: str) -> str:
    for i, (host, _) in enumerate(NODES, 1):
        if address in (host, f'redis-{i}'):
            return f'redis-{i}'
    raise ValueError(f'Sentinel returned a non-lab address: {address!r}')


def current_master() -> tuple[str, int]:
    host, port = sentinel().discover_master(MASTER_NAME)
    node_name(host)
    if port != 6379:
        raise ValueError('Unexpected master port')
    with direct((host, port)) as connection:
        if connection.role()[0] != 'master':
            raise RuntimeError('Sentinel view is changing; discovered node is not currently master')
    return host, port


def snapshot() -> dict:
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
    for name, data in state['sentinel'].items():
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
                    acknowledged = connection.wait(2, 1000)
                    if connection.get(key) == 'ok' and acknowledged == 2:
                        print_status(state)
                        print('PASS: authenticated write/read and WAIT 2 on the same connection')
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


def seed(users: int, payload_bytes: int) -> None:
    if not 1 <= users <= 100000 or not 0 <= payload_bytes <= 8192:
        raise ValueError('users=1..100000, payload-bytes=0..8192')
    if users * (payload_bytes + 500) > 64 * 1024 * 1024:
        raise ValueError('Seed estimated payload exceeds the 64 MiB lab safety cap; reduce inputs.')
    with master_client() as connection:
        with connection.pipeline(transaction=False) as pipe:
            for i in range(1, users + 1):
                pipe.hset(f'lab:user:{i}', mapping={'id': str(i), 'name': f'user-{i:06d}',
                    'tier': ('basic', 'silver', 'gold')[i % 3], 'risk': str((i * 17) % 100),
                    'payload': 'x' * payload_bytes})
                pipe.set(f'lab:session:{i}', f'token-{i:08d}', ex=900 + i % 900)
                pipe.zadd('lab:ranking:risk', {str(i): (i * 17) % 100})
                pipe.sadd('lab:users:active', str(i))
                pipe.xadd('lab:events:transactions', {'uid': i, 'amount': 1000 + i % 100000,
                    'kind': 'synthetic'}, maxlen=10000, approximate=True)
                if i % 100 == 0:
                    pipe.execute()
                    print(f'Seed {i}/{users}', flush=True)
            pipe.execute()
        connection.set('lab:seed:users', users)
        print(json.dumps({'users': users, 'dbsize': connection.dbsize(),
                          'used_memory_human': connection.info('memory')['used_memory_human']}, ensure_ascii=False))


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
    manager = sentinel()
    if args.mode == 'sentinel':
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
    s = sub.add_parser('seed'); s.add_argument('--users', type=int, default=2000); s.add_argument('--payload-bytes', type=int, default=256)
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
    elif args.command == 'master': print(node_name(current_master()[0]))
    elif args.command == 'seed': seed(args.users, args.payload_bytes)
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
