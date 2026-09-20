"""Two transactions lock two run-owned synthetic rows in opposite orders; ALWAYS rollback."""
from __future__ import annotations
from .observability import safe_error
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading
from .adapters import Settings, SQL
from .core import identifier


def deadlock(repo, order_ids, run_id):
    if len(order_ids) != 2 or len(set(order_ids)) != 2 or not re.fullmatch(r'sim-[a-f0-9]{12}', run_id):
        raise ValueError('Two distinct synthetic orders and a study run are required')
    for oid in order_ids:
        identifier(oid)
    barrier = threading.Barrier(2, timeout=5)

    def transaction(first, second):
        conn = repo.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SET SESSION innodb_lock_wait_timeout=3')
                # Prove both rows are synthetic before taking ANY row lock.
                for oid in (first, second):
                    cur.execute('SELECT item FROM orders WHERE id=%s', (oid,))
                    row = cur.fetchone()
                    if not row or not row['item'].startswith(run_id + '-'):
                        raise ValueError('Unrelated row refused')
                cur.execute('SELECT item FROM orders WHERE id=%s FOR UPDATE', (first,))
                if not cur.fetchone()['item'].startswith(run_id + '-'):
                    raise ValueError('Synthetic row changed before locking')
                barrier.wait()
                cur.execute('SELECT item FROM orders WHERE id=%s FOR UPDATE', (second,))
                return {'outcome': 'acquired_both', 'code': 0}
        except Exception as exc:
            barrier.abort()
            code = exc.args[0] if exc.args and type(exc.args[0]) is int else None
            return {'outcome': 'deadlock_victim' if code == 1213 else 'not_deadlock',
                    'code': code, 'error': type(exc).__name__}
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(transaction, *order_ids), pool.submit(transaction, *reversed(order_ids))]
        outcomes = [f.result() for f in futures]
    observed = sorted(r['code'] for r in outcomes if r['code'] is not None) == [0, 1213]
    return {'observed_deadlock': observed, 'outcomes': outcomes,
            'scope': 'two synthetic row locks; no business UPDATE or global settings; both transactions rolled back'}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run-id', required=True)
    p.add_argument('--orders', nargs=2, required=True)
    a = p.parse_args()
    try:
        result = deadlock(SQL(Settings.load()), a.orders, a.run_id)
        print(json.dumps(result))
        return 0 if result['observed_deadlock'] else 2
    except Exception as exc:
        print(json.dumps({**safe_error(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
