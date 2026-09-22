#!/usr/bin/env python3
"""Container-only workload. Synthetic balances; not a financial application template."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import statistics
import sys
import threading
import time
import uuid

import pymysql

NODES = tuple(filter(None, os.environ.get('GALERA_NODES', 'galera1,galera2,galera3').split(',')))
RETRYABLE = {1047, 1062, 1205, 1213, 2002, 2003, 2006, 2013}
# A freshly recreated HAProxy can refuse SQL until its backend table converges.
CHECK_ATTEMPTS = int(os.environ.get('CHECK_ATTEMPTS', '60'))


def connect(host='proxy', port=3306, readonly=False):
    return pymysql.connect(host=host, port=port,
        user='readonly' if readonly else 'lab',
        password=os.environ['READONLY_PASSWORD' if readonly else 'LAB_PASSWORD'],
        database='lab_ops', charset='utf8mb4', autocommit=True,
        connect_timeout=3, read_timeout=12, write_timeout=12)


def checked_connect(host='proxy', port=3306, readonly=False):
    last = None
    for _ in range(CHECK_ATTEMPTS):
        conn = None
        try:
            conn = connect(host, port, readonly)
            with conn.cursor() as cur: cur.execute('SELECT 1')
            return conn
        except pymysql.MySQLError as exc:
            if conn is not None:
                try: conn.close()
                except pymysql.MySQLError: pass
            last = exc; time.sleep(1)
    raise last


def query(conn, sql, values=None):
    with conn.cursor() as cur:
        cur.execute(sql, values)
        return cur.fetchall()


def check():
    with checked_connect() as conn:
        print('writer:', query(conn, 'SELECT @@hostname')[0][0])
    with checked_connect(port=3307, readonly=True) as conn:
        query(conn, 'SET SESSION wsrep_sync_wait=1')
        print('reader:', query(conn, 'SELECT @@hostname')[0][0])
        try:
            query(conn, "INSERT INTO probe VALUES(%s,@@hostname,NOW(6),'must-be-denied')", (str(uuid.uuid4()),))
        except pymysql.MySQLError as exc:
            if exc.args[0] not in (1142, 1143): raise
            print('readonly write correctly denied:', exc.args[0])
        else: raise RuntimeError('Readonly user was incorrectly allowed to write.')


def routes():
    for label, port, readonly in (('writer',3306,False), ('reader',3307,True)):
        hosts = []
        for _ in range(6):
            with checked_connect(port=port, readonly=readonly) as conn:
                hosts.append(query(conn, 'SELECT @@hostname')[0][0])
        print(label + ' (six NEW connections): ' + ' -> '.join(hosts))
    print('A single persistent connection stays on its selected backend; this is not per-query routing.')


def transfer(conn, request_id, source, target, amount):
    """One atomic transfer. A stable request_id makes retry after an unknown COMMIT safe."""
    query(conn, 'SET SESSION wsrep_sync_wait=1')
    existing = query(conn, 'SELECT from_id,to_id,amount,handled_by FROM transfer WHERE request_id=%s', (request_id,))
    if existing:
        if tuple(existing[0][:3]) != (source,target,amount):
            raise RuntimeError('Idempotency key was reused with a different request.')
        return existing[0][3]
    query(conn, 'SET SESSION wsrep_sync_wait=0')
    conn.begin()
    try:
        hostname = query(conn, 'SELECT @@hostname')[0][0]
        balances = dict(query(conn, 'SELECT id,balance FROM account WHERE id IN (%s,%s) ORDER BY id FOR UPDATE',
                              (min(source,target),max(source,target))))
        if balances[source] < amount:
            conn.rollback(); return None
        query(conn, 'INSERT INTO transfer VALUES(%s,%s,%s,%s,%s,NOW(6))',
              (request_id,source,target,amount,hostname))
        query(conn, 'UPDATE account SET balance=balance-%s WHERE id=%s', (amount,source))
        query(conn, 'UPDATE account SET balance=balance+%s WHERE id=%s', (amount,target))
        conn.commit()
        return hostname
    except BaseException:
        try: conn.rollback()
        except pymysql.MySQLError: pass
        raise


def load(seconds, workers, target):
    if not 1 <= seconds <= 3600 or not 1 <= workers <= 32:
        raise ValueError('seconds: 1..3600, workers: 1..32')
    deadline = time.monotonic() + seconds
    counts, errors, hosts, latencies = Counter(), Counter(), Counter(), []
    lock = threading.Lock()
    def worker(index):
        rng = random.Random(20260917 + index)
        conn = None
        try:
            while time.monotonic() < deadline:
                request_id = str(uuid.uuid4())
                source, dest = rng.sample(range(1,101), 2)
                amount = rng.randint(1,100)
                started = time.monotonic()
                success = False
                for attempt in range(8):
                    try:
                        if conn is None:
                            host = 'proxy' if target == 'writer' else rng.choice(NODES)
                            conn = connect(host)
                        node = transfer(conn, request_id, source, dest, amount)
                        with lock:
                            counts['committed' if node else 'insufficient_funds'] += 1
                            if node: hosts[node] += 1
                            latencies.append((time.monotonic()-started)*1000)
                        success = True
                        break
                    except pymysql.MySQLError as exc:
                        code = exc.args[0] if exc.args else -1
                        with lock: errors[str(code)] += 1; counts['retries'] += 1
                        if conn:
                            try: conn.close()
                            except pymysql.MySQLError: pass
                        conn = None
                        if code not in RETRYABLE: raise
                        # Reuse the same request_id, including after a lost COMMIT response.
                        time.sleep(min(0.05 * 2**attempt, 1.5) + rng.random()*0.05)
                if not success:
                    with lock: counts['unresolved_requests'] += 1
                time.sleep(0.02)
        finally:
            if conn: conn.close()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, i) for i in range(workers)]
        for future in futures: future.result()
    ordered = sorted(latencies)
    report = {'counts':dict(counts), 'successful_backend':dict(hosts), 'errors_by_code':dict(errors),
              'latency_ms_p50':round(statistics.median(ordered),2) if ordered else None,
              'latency_ms_p95':round(ordered[min(len(ordered)-1,int(len(ordered)*.95))],2) if ordered else None}
    print(json.dumps(report, indent=2))
    with checked_connect() as conn:
        query(conn,'SET SESSION wsrep_sync_wait=1')
        total = int(query(conn,'SELECT SUM(balance) FROM account')[0][0])
        print('Balance invariant:',total,'expected=100000000')
        if total != 100000000: raise RuntimeError('Balance invariant violated.')
    if counts['unresolved_requests']:
        print('Some requests remain unresolved after retries; inspect transfer receipts before declaring them failed.', file=sys.stderr)
        return 2
    return 0


def conflict():
    a = b = None
    try:
        a = checked_connect('galera1')
        b = checked_connect('galera2')
        query(a,'SET SESSION wsrep_sync_wait=1')
        before = query(a,'SELECT value FROM counter WHERE id=1')[0][0]
        query(a,'SET SESSION wsrep_sync_wait=0')
        a.begin(); b.begin()
        print('Both nodes update the SAME primary key inside open local transactions.')
        query(a,'UPDATE counter SET value=value+1 WHERE id=1')
        query(b,'UPDATE counter SET value=value+1 WHERE id=1')
        results, acknowledged, rejected = [], 0, []
        for name, conn in (('galera1',a),('galera2',b)):
            try:
                conn.commit(); acknowledged += 1
                results.append(name + ': COMMIT acknowledged')
            except pymysql.MySQLError as exc:
                rejected.append(exc.args[0] if exc.args else None)
                conn.rollback(); results.append(name + ': rejected ' + str(exc.args))
        for result in results: print(result)
        query(a,'SET SESSION wsrep_sync_wait=1')
        after = query(a,'SELECT value FROM counter WHERE id=1')[0][0]
        print('Counter delta:',after-before)
        if acknowledged != 1 or rejected != [1213] or after - before != 1:
            raise RuntimeError('Conflict NOT reproduced correctly: expected one COMMIT, one deadlock/certification rejection (1213), and counter delta 1.')
        print('Inspect wsrep_local_cert_failures AND wsrep_local_bf_aborts: either can account for the rejection.')
    finally:
        for conn in (a,b):
            if conn is None: continue
            try: conn.rollback()
            except pymysql.MySQLError: pass
            finally:
                try: conn.close()
                except pymysql.MySQLError: pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','route','load','conflict'])
    parser.add_argument('--seconds',type=int,default=60)
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--target',choices=['writer','multi'],default='writer')
    args = parser.parse_args()
    if args.action == 'check': check()
    elif args.action == 'route': routes()
    elif args.action == 'conflict': conflict()
    else: return load(args.seconds,args.workers,args.target)
    return 0

if __name__ == '__main__':
    try: sys.exit(main())
    except (ValueError, RuntimeError, pymysql.MySQLError) as exc:
        print('ERROR:',exc,file=sys.stderr); sys.exit(1)
