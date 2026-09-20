#!/usr/bin/env python3
"""Generate synthetic SQL traffic; API latency fields are simulated, not measured."""
import argparse
import json
import os
import random
import socket
import time
import math
import uuid
from collections import Counter

def connect():
    import pymysql
    return pymysql.connect(host='proxy', port=3306, user='lab',
        password=os.environ['LAB_PASSWORD'], database='commerce_lab',
        charset='utf8mb4', autocommit=True, connect_timeout=3,
        read_timeout=12, write_timeout=12)


def percentiles(values):
    """Nearest-rank percentiles, in milliseconds; empty samples are explicit nulls."""
    ordered = sorted(values)
    def at(q):
        return round(ordered[max(0, math.ceil(q * len(ordered)) - 1)], 3) if ordered else None
    return {'samples': len(ordered), 'p50': at(.5), 'p95': at(.95), 'p99': at(.99)}


def run(seconds, rate, seed, mode):
    import pymysql
    if not 1 <= seconds <= 86400 or not 1 <= rate <= 2000:
        raise ValueError('seconds: 1..86400, rate: 1..2000')
    if mode not in ('api', 'orders', 'mixed'):
        raise ValueError('mode must be api, orders, or mixed')
    rng = random.Random(seed)
    counts = Counter()
    sql_latencies, failed_latencies, attempt_latencies, reconnect_latencies = [], [], [], []
    host = socket.gethostname()
    run_id = uuid.uuid4().hex
    setup_started = time.monotonic()
    conn = None
    try:
        conn = connect()
        with conn.cursor() as cur:
            cur.execute('SELECT COALESCE(MAX(order_id),0) FROM orders')
            max_order = int(cur.fetchone()[0])
        if mode in ('orders', 'mixed') and max_order == 0:
            raise ValueError('orders mode requires a seeded commerce_lab.orders table')
        started = time.monotonic()
        deadline = started + seconds
        next_tick = started
        interval = 1.0 / rate
        slot = 0
        sleep_seconds = 0.0
        while True:
            now = time.monotonic()
            if now >= deadline or slot >= seconds * rate:
                break
            if now < next_tick:
                delay = min(next_tick, deadline) - now
                time.sleep(delay)
                sleep_seconds += delay
            # A batch cannot overshoot the deadline: there is no batch loop.
            if time.monotonic() >= deadline:
                break
            started_op = time.monotonic()
            counts['attempted'] += 1
            operation = 'api' if mode == 'api' or (mode == 'mixed' and rng.random() < 0.65) else 'order'
            try:
                if conn is None:
                    reconnect_started = time.monotonic()
                    counts['reconnect_attempted'] += 1
                    try:
                        conn = connect()
                    except pymysql.MySQLError:
                        counts['reconnect_failed'] += 1
                        raise
                    finally:
                        reconnect_latencies.append((time.monotonic() - reconnect_started) * 1000)
                sql_started = time.monotonic()
                with conn.cursor() as cur:
                    if operation == 'api':
                        event_id = uuid.uuid4().int & ((1 << 63) - 1)
                        customer = rng.randint(1, 100)
                        cur.execute(
                            '''INSERT INTO api_request_log
                            (log_id,request_at,customer_id,method,endpoint,status_code,
                             latency_ms,remote_ip,user_agent,request_meta)
                            VALUES (%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s)''',
                            (event_id, customer, rng.choice(('GET', 'POST', 'PUT', 'DELETE')),
                             rng.choice(('/api/catalog', '/api/orders', '/api/search',
                                         '/api/checkout', '/api/profile')),
                             rng.choices((200, 201, 400, 404, 500), (72, 12, 8, 5, 3))[0],
                             max(1, int(rng.lognormvariate(4.2, 0.65))),
                             '10.0.%d.%d' % (rng.randint(0, 255), rng.randint(1, 254)),
                             'lab-simulator/1.0', json.dumps({'source': host, 'seed': seed, 'run_id': run_id, 'latency_kind': 'synthetic_api'})))
                        counts['api_log_inserted'] += 1
                    else:
                        order_id = rng.randint(1, max_order)
                        if rng.random() < 0.25:
                            cur.execute('SELECT status FROM orders WHERE order_id=%s', (order_id,))
                            cur.fetchone()
                            counts['order_reads'] += 1
                        else:
                            status = rng.choice(('PAID', 'PACKING', 'SHIPPED', 'DELIVERED'))
                            cur.execute('UPDATE orders SET status=%s,updated_at=NOW() WHERE order_id=%s',
                                        (status, order_id))
                            counts['order_updates'] += cur.rowcount
                            counts['order_noops'] += cur.rowcount == 0
                sql_latencies.append((time.monotonic() - sql_started) * 1000)
                counts['succeeded'] += 1
            except pymysql.MySQLError:
                counts['errors'] += 1
                failed_latencies.append((time.monotonic() - started_op) * 1000)
                if conn is not None:
                    try:
                        conn.close()
                    except pymysql.MySQLError:
                        pass
                    conn = None
            finally:
                attempt_latencies.append((time.monotonic() - started_op) * 1000)
            # Skip missed schedule slots rather than sending a catch-up burst.
            # The next start is always >= one interval after the previous start.
            slot += 1
            now = time.monotonic()
            next_slot = max(slot, math.ceil((now - started) * rate - 1e-9))
            counts['skipped_schedule_slots'] += next_slot - slot
            slot = next_slot
            next_tick = max(started + slot * interval, started_op + interval)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
            sleep_seconds += remaining
        elapsed = time.monotonic() - started
        summary = {
            'run_id': run_id, 'seconds': seconds, 'rate': rate, 'mode': mode,
            'seed': seed, 'source': host, 'operations': dict(counts),
            'setup_seconds': round(started - setup_started, 6),
            'elapsed_seconds': round(elapsed, 6),
            'deadline_overrun_seconds': round(max(0, elapsed - seconds), 6),
            'attempted': counts['attempted'], 'succeeded': counts['succeeded'],
            'failed': counts['errors'],
            'actual_attempts_per_second': round(counts['attempted'] / elapsed, 6) if elapsed else 0,
            'sleep_seconds': round(sleep_seconds, 6), 'pacing_policy': 'skip-missed-slots; no catch-up burst',
            'successful_sql_latency_ms': percentiles(sql_latencies),
            'failed_attempt_latency_ms': percentiles(failed_latencies),
            'all_attempt_latency_ms': percentiles(attempt_latencies),
            'reconnect_latency_ms': percentiles(reconnect_latencies),
            'stored_api_latency': 'synthetic random value, NOT an API response-time measurement',
            'deadline_policy': 'no new attempt after deadline; an in-flight SQL/connect may finish after it',
            # Backward-compatible aliases: successful SQL operations ONLY.
            'latency_ms_p50': percentiles(sql_latencies)['p50'],
            'latency_ms_p95': percentiles(sql_latencies)['p95'],
        }
        print(json.dumps(summary, indent=2))
        return summary
    finally:
        if conn is not None:
            conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seconds', type=int, default=300)
    p.add_argument('--rate', type=int, default=10, help='events per second')
    p.add_argument('--seed', type=int, default=20260918)
    p.add_argument('--mode', choices=('api', 'orders', 'mixed'), default='mixed')
    a = p.parse_args()
    run(a.seconds, a.rate, a.seed, a.mode)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        print('ERROR:', exc, file=__import__('sys').stderr)
        raise SystemExit(1)
