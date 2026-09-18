#!/usr/bin/env python3
"""Continuously generate realistic commerce API and order traffic in the lab."""
import argparse
import json
import os
import random
import socket
import time
from collections import Counter

def connect():
    import pymysql
    return pymysql.connect(host='proxy', port=3306, user='lab',
        password=os.environ['LAB_PASSWORD'], database='commerce_lab',
        charset='utf8mb4', autocommit=True, connect_timeout=3,
        read_timeout=12, write_timeout=12)


def run(seconds, rate, seed, mode):
    import pymysql
    if not 1 <= seconds <= 86400 or not 1 <= rate <= 2000:
        raise ValueError('seconds: 1..86400, rate: 1..2000')
    if mode not in ('api', 'orders', 'mixed'):
        raise ValueError('mode must be api, orders, or mixed')
    rng = random.Random(seed)
    started = time.monotonic()
    next_tick = started
    counts = Counter()
    latencies = []
    host = socket.gethostname()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COALESCE(MAX(order_id),0) FROM orders')
            max_order = int(cur.fetchone()[0])
        if mode in ('orders', 'mixed') and max_order == 0:
            raise ValueError('orders mode requires a seeded commerce_lab.orders table')
        while time.monotonic() - started < seconds:
            now = time.time()
            if now < next_tick:
                time.sleep(next_tick - now)
            batch = min(rate, max(1, rate // 4))
            base = int(time.time() * 1000000) * 100
            for offset in range(batch):
                started_op = time.monotonic()
                operation = 'api' if mode == 'api' or (mode == 'mixed' and rng.random() < 0.65) else 'order'
                try:
                    with conn.cursor() as cur:
                        if operation == 'api':
                            event_id = base + offset
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
                                 'lab-simulator/1.0', json.dumps({'source': host, 'seed': seed})))
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
                    latencies.append((time.monotonic() - started_op) * 1000)
                except pymysql.MySQLError:
                    counts['errors'] += 1
                    try:
                        conn.close()
                    except pymysql.MySQLError:
                        pass
                    conn = connect()
                next_tick += 1.0 / rate
        ordered = sorted(latencies)
        print(json.dumps({'seconds': seconds, 'rate': rate, 'mode': mode,
                          'operations': dict(counts), 'seed': seed, 'source': host,
                          'latency_ms_p50': round(ordered[len(ordered)//2], 2) if ordered else None,
                          'latency_ms_p95': round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 2)
                          if ordered else None}, indent=2))
    finally:
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
