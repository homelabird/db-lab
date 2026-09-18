#!/usr/bin/env python3
"""Container-side incident exercises. Sent through exec by scenarios.py; no rebuild needed.
Only synthetic incident_lab tables are changed. Never import this module to execute it.
"""
import datetime
import decimal
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

import pymysql

SCHEMA = 'incident_lab'
SIGNATURE = 'mariadb-ha-incidents-v1'
TABLES = {'slow_orders', 'fragmented_events', 'lock_accounts', 'fault_probe'}
LOG_KEYS = ('slow_query_log', 'log_output')


def connection(proxy=False, timeout=300):
    kwargs = dict(user='lab' if proxy else 'root',
                  password=os.environ['LAB_PASSWORD' if proxy else 'MARIADB_ROOT_PASSWORD'],
                  charset='utf8mb4', autocommit=True, connect_timeout=3,
                  read_timeout=timeout, write_timeout=timeout,
                  cursorclass=pymysql.cursors.DictCursor)
    if proxy:
        kwargs.update(host='proxy', port=3306, database=SCHEMA)
    else:
        kwargs['unix_socket'] = '/run/mysqld/mysqld.sock'
    conn = pymysql.connect(**kwargs)
    query(conn, 'SET SESSION lock_wait_timeout=30, innodb_lock_wait_timeout=15, max_statement_time=180')
    if not proxy:
        query(conn, "SET SESSION wsrep_OSU_method='TOI'")
    return conn


def query(conn, sql, args=None):
    with conn.cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchall()


def scalar(conn, sql, args=None):
    rows = query(conn, sql, args)
    return next(iter(rows[0].values())) if rows else None


def bounds(value, low, high, label):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError('%s must be %d..%d' % (label, low, high))
    return value


def validate_request(req):
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,39}', req.get('owner', '')):
        raise ValueError('Invalid project owner')
    if not re.fullmatch(r'[a-f0-9]{24}', req.get('token', '')):
        raise ValueError('Invalid execution token')


def ensure_schema(conn, owner, create=True):
    if scalar(conn, 'SELECT @@SESSION.wsrep_on') != 1:
        raise ValueError('This worker requires a wsrep-enabled lab session')
    if create:
        query(conn, "SET SESSION wsrep_OSU_method='TOI'")
    exists = scalar(conn, "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s", (SCHEMA,))
    if not exists:
        if not create:
            raise ValueError('Run a scenario first: incident_lab does not exist')
        query(conn, 'CREATE DATABASE incident_lab CHARACTER SET utf8mb4')
        query(conn, '''CREATE TABLE incident_lab.__lab_marker (
            id INT PRIMARY KEY, signature VARCHAR(80) NOT NULL, owner VARCHAR(40) NOT NULL
        ) ENGINE=InnoDB''')
        query(conn, 'INSERT INTO incident_lab.__lab_marker VALUES(1,%s,%s)', (SIGNATURE, owner))
    try:
        marker = query(conn, 'SELECT signature,owner FROM incident_lab.__lab_marker WHERE id=1')
    except pymysql.MySQLError as exc:
        raise ValueError('Existing incident_lab lacks the ownership marker; refusing modifications') from exc
    if marker != [{'signature': SIGNATURE, 'owner': owner}]:
        raise ValueError('incident_lab ownership mismatch; refusing modifications')
    # Existing users from the base project; do not create or replace passwords.
    if create:
        query(conn, "GRANT SELECT,INSERT,UPDATE,DELETE ON incident_lab.* TO 'lab'@'%'")
        query(conn, "GRANT SELECT ON incident_lab.* TO 'readonly'@'%'")
    query(conn, 'USE incident_lab')


def batches(rows, batch=500):
    for start in range(1, rows + 1, batch):
        yield start, min(start + batch - 1, rows)


def log_state(conn):
    return {key: scalar(conn, 'SELECT @@GLOBAL.' + key) for key in LOG_KEYS}


def restore_logging(conn, state):
    if set(state) != set(LOG_KEYS):
        raise ValueError('Invalid slow log restore snapshot')
    if str(state['slow_query_log']) not in ('0', '1'):
        raise ValueError('Invalid slow_query_log state')
    if any(x not in ('NONE', 'FILE', 'TABLE') for x in str(state['log_output']).split(',')):
        raise ValueError('Invalid log_output state')
    # Keep changes local; GLOBAL settings are node-local, not replicated DML.
    query(conn, 'SET GLOBAL log_output=%s', (state['log_output'],))
    query(conn, 'SET GLOBAL slow_query_log=%s', (int(state['slow_query_log']),))
    return log_state(conn)


def measure(conn, sql, repeats=3):
    explain = query(conn, 'EXPLAIN ' + sql)
    samples = []
    for _ in range(repeats):
        before = {r['Variable_name']: int(r['Value']) for r in query(conn,
            "SHOW SESSION STATUS WHERE Variable_name IN ('Handler_read_rnd_next','Handler_read_key','Handler_read_next')")}
        started = time.perf_counter()
        result = query(conn, sql)
        elapsed = (time.perf_counter() - started) * 1000
        after = {r['Variable_name']: int(r['Value']) for r in query(conn,
            "SHOW SESSION STATUS WHERE Variable_name IN ('Handler_read_rnd_next','Handler_read_key','Handler_read_next')")}
        samples.append({'elapsed_ms': round(elapsed, 3), 'result': result,
                        'handler_delta': {k: after[k] - v for k, v in before.items()}})
    return {'sql': sql, 'explain': explain, 'samples': samples}


def build_slow(conn, req):
    rows = bounds(req.get('rows', 50000), 1000, 500000, 'rows')
    query(conn, 'DROP TABLE IF EXISTS slow_orders')
    query(conn, '''CREATE TABLE slow_orders (
        order_id BIGINT PRIMARY KEY, customer_id INT NOT NULL,
        created_at DATETIME NOT NULL, amount DECIMAL(12,2) NOT NULL,
        detail VARCHAR(256) NOT NULL
    ) ENGINE=InnoDB''')
    for lo, hi in batches(rows):
        query(conn, f'''INSERT INTO slow_orders
            SELECT seq, MOD(seq,1000), TIMESTAMPADD(SECOND,seq,'2025-01-01'),
            10 + MOD(seq,10000)/100, RPAD(SHA2(CAST(seq AS CHAR),256),256,'x')
            FROM seq_{lo}_to_{hi}''')
    query(conn, 'ANALYZE TABLE slow_orders')


def slow_work(conn, req, create=True):
    if create:
        build_slow(conn, req)
    original = log_state(conn)
    tag = 'lab_slow_' + req['token']
    query_sql = "SELECT SQL_NO_CACHE /* %s */ SUM(amount) AS total FROM slow_orders WHERE customer_id=42" % tag
    report = {'logging_before': original, 'kind': 'slow', 'synthetic_delay_seconds': 0.3}
    try:
        output = set(str(original['log_output']).split(',')) - {'NONE'}
        output.add('TABLE')
        query(conn, 'SET GLOBAL log_output=%s', (','.join(sorted(output)),))
        query(conn, 'SET GLOBAL slow_query_log=1')
        # These are SESSION settings. Existing application sessions keep their thresholds.
        query(conn, "SET SESSION slow_query_log=1, long_query_time=0.05, min_examined_row_limit=0, log_slow_rate_limit=1, log_slow_filter=''")
        query(conn, 'SELECT /* ' + tag + '_sleep */ SLEEP(0.3)')
        report['before'] = measure(conn, query_sql)
        if not req.get('leave_broken', False):
            index = scalar(conn, "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=%s AND table_name='slow_orders' AND index_name='idx_customer'", (SCHEMA,))
            if not index:
                query(conn, 'ALTER TABLE slow_orders ADD INDEX idx_customer(customer_id)')
            query(conn, 'ANALYZE TABLE slow_orders')
            report['after'] = measure(conn, query_sql)
            report['result_unchanged'] = report['before']['samples'][0]['result'] == report['after']['samples'][0]['result']
            if not report['result_unchanged']:
                raise RuntimeError('Query result changed during index comparison; stop other writers')
        # Read only. Never truncate the system log table or delete another session's logs.
        query(conn, 'SET SESSION slow_query_log=0')
        report['slow_log'] = query(conn, '''SELECT start_time, query_time, lock_time,
            rows_sent, rows_examined, sql_text FROM mysql.slow_log
            WHERE LOCATE(%s,sql_text)>0 ORDER BY start_time DESC LIMIT 30''', (tag,))
        report['slow_log_observed'] = any('_sleep' in str(r['sql_text']) for r in report['slow_log'])
        if not report['slow_log_observed']:
            raise RuntimeError('The synthetic delay was not found in mysql.slow_log; inspect log filters/settings')
    finally:
        report['logging_restored'] = restore_logging(conn, original)
    report['left_unindexed'] = bool(req.get('leave_broken', False))
    return report


def file_stats(conn):
    datadir = Path(str(scalar(conn, 'SELECT @@datadir'))).resolve()
    path = datadir / SCHEMA / 'fragmented_events.ibd'
    if not path.exists():
        return {'path': str(path), 'exists': False, 'logical_bytes': None, 'allocated_bytes': None}
    st = path.stat()
    return {'path': str(path), 'exists': True, 'logical_bytes': st.st_size,
            'allocated_bytes': st.st_blocks * 512 if hasattr(st, 'st_blocks') else None}


def frag_snapshot(conn, label):
    query(conn, 'ANALYZE TABLE fragmented_events')
    stats = query(conn, '''SELECT TABLE_ROWS AS estimated_rows, DATA_LENGTH AS data_bytes,
        INDEX_LENGTH AS index_bytes, DATA_FREE AS free_extent_bytes
        FROM information_schema.tables WHERE TABLE_SCHEMA=%s AND TABLE_NAME='fragmented_events' ''', (SCHEMA,))[0]
    stats.update(query(conn, 'SELECT COUNT(*) AS exact_rows, COALESCE(SUM(OCTET_LENGTH(payload)),0) AS payload_bytes FROM fragmented_events')[0])
    stats['file'] = file_stats(conn)
    stats['stage'] = label
    # A measurement, NOT a complete fragmentation percentage or a speed guarantee.
    stats['scan'] = measure(conn, 'SELECT SQL_NO_CACHE SUM(OCTET_LENGTH(payload)) AS total FROM fragmented_events', repeats=1)
    return stats


def frag_work(conn, req, create=True):
    if not scalar(conn, 'SELECT @@innodb_file_per_table'):
        raise ValueError('This exercise requires innodb_file_per_table=ON; no GLOBAL change was made')
    snapshots = []
    if create:
        rows = bounds(req.get('rows', 50000), 1000, 500000, 'rows')
        payload = bounds(req.get('payload_bytes', 1024), 128, 4096, 'payload_bytes')
        if rows * payload > 512 * 1024 * 1024:
            raise ValueError('Bounded lab: rows * payload_bytes must be <= 512 MiB per node')
        datadir = Path(str(scalar(conn, 'SELECT @@datadir')))
        sv = os.statvfs(str(datadir))
        required = max(512 * 1024 * 1024, rows * payload * 9)
        if sv.f_bavail * sv.f_frsize < required:
            raise ValueError('Insufficient free space on this datadir filesystem; reserve 3 copies, rebuild and logs')
        query(conn, 'DROP TABLE IF EXISTS fragmented_events')
        query(conn, '''CREATE TABLE fragmented_events (
            id BIGINT PRIMARY KEY, tenant_id INT NOT NULL, payload VARBINARY(4096) NOT NULL,
            KEY idx_tenant(tenant_id)
        ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC''')
        for lo, hi in batches(rows):
            query(conn, f'''INSERT INTO fragmented_events
                SELECT seq, MOD(seq,100), RPAD(SHA2(CAST(seq AS CHAR),256),{payload},'q')
                FROM seq_{lo}_to_{hi}''')
        snapshots.append(frag_snapshot(conn, 'before_delete'))
        # Delete 80% spread across the key range, not TRUNCATE. Small committed writesets.
        for lo, hi in batches(rows):
            query(conn, 'DELETE FROM fragmented_events WHERE id BETWEEN %s AND %s AND MOD(id,5)<>0', (lo, hi))
        time.sleep(2)  # Purge is asynchronous; DATA_FREE need not change immediately.
        snapshots.append(frag_snapshot(conn, 'after_delete'))
    else:
        snapshots.append(frag_snapshot(conn, 'before_rebuild'))
    result = {'kind': 'fragmentation', 'snapshots': snapshots, 'left_fragmented': bool(req.get('leave_broken', False))}
    if not req.get('leave_broken', False):
        before = snapshots[-1]
        # FORCE explicitly requests rebuild; no obsolete innodb_defragment dependency.
        # wsrep remains ON and the base lab uses TOI. This can block all cluster writers.
        result['rebuild_sql'] = 'ALTER TABLE incident_lab.fragmented_events FORCE'
        started = time.monotonic()
        query(conn, result['rebuild_sql'])
        result['rebuild_seconds'] = round(time.monotonic() - started, 3)
        after = frag_snapshot(conn, 'after_rebuild')
        snapshots.append(after)
        result['data_unchanged_by_rebuild'] = (before['exact_rows'], before['payload_bytes']) == (after['exact_rows'], after['payload_bytes'])
        if not result['data_unchanged_by_rebuild']:
            raise RuntimeError('Rebuild row/payload invariant failed; stop other writers')
        old, new = before['file']['logical_bytes'], after['file']['logical_bytes']
        result['file_bytes_reclaimed'] = old - new if old is not None and new is not None else None
    return result


def lock_work(conn, req):
    hold = bounds(req.get('hold', 12), 6, 120, 'hold')
    query(conn, 'CREATE TABLE IF NOT EXISTS lock_accounts(id INT PRIMARY KEY, balance INT NOT NULL) ENGINE=InnoDB')
    query(conn, 'REPLACE INTO lock_accounts VALUES(1,1000),(2,1000)')
    a, b = connection(timeout=hold + 20), connection(timeout=hold + 20)
    result = {'kind': 'row-lock', 'scope': 'two sessions on the SAME node', 'hold_seconds': hold}
    finished = threading.Event()
    try:
        a.begin()
        query(a, 'UPDATE incident_lab.lock_accounts SET balance=balance+1 WHERE id=1')
        result['blocker_connection_id'] = scalar(a, 'SELECT CONNECTION_ID()')
        result['waiter_connection_id'] = scalar(b, 'SELECT CONNECTION_ID()')
        query(b, 'SET SESSION innodb_lock_wait_timeout=%s', (hold + 5,))
        def waiter():
            start = time.monotonic()
            try:
                b.begin()
                query(b, 'UPDATE incident_lab.lock_accounts SET balance=balance-1 WHERE id=1')
                result['waiter'] = {'completed': True, 'elapsed_seconds': round(time.monotonic() - start, 3)}
            except Exception as exc:
                result['waiter'] = {'completed': False, 'error': str(exc), 'elapsed_seconds': round(time.monotonic() - start, 3)}
            finally:
                try: b.rollback()
                except Exception: pass
                finished.set()
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(1)
        result['lock_waits'] = query(conn, 'SELECT * FROM information_schema.INNODB_LOCK_WAITS')
        result['transactions'] = query(conn, '''SELECT trx_id,trx_state,trx_started,trx_wait_started,
            trx_mysql_thread_id,trx_query FROM information_schema.INNODB_TRX''')
        result['processlist'] = query(conn, '''SELECT ID,USER,DB,COMMAND,TIME,STATE,INFO
            FROM information_schema.PROCESSLIST WHERE ID IN (%s,%s)''',
            (result['blocker_connection_id'], result['waiter_connection_id']))
        result['lock_wait_observed'] = bool(result['lock_waits'])
        time.sleep(max(0, hold - 1))
    finally:
        try: a.rollback()
        finally: a.close()
        if 'thread' in locals():
            finished.wait(hold + 8)
            thread.join(timeout=1)
        b.close()
    result['final_balance'] = scalar(conn, 'SELECT balance FROM lock_accounts WHERE id=1')
    if result['final_balance'] != 1000:
        raise RuntimeError('Lock demo rollback invariant failed')
    if not result.get('waiter', {}).get('completed'):
        raise RuntimeError('Waiter did not complete after releasing the lock; inspect timeouts')
    if not result.get('lock_wait_observed'):
        raise RuntimeError('No row lock wait was observed; do not claim a successful reproduction')
    return result


def prepare_probe(conn):
    query(conn, '''CREATE TABLE IF NOT EXISTS fault_probe (
        request_id VARCHAR(80) PRIMARY KEY, run_id CHAR(24) NOT NULL,
        handled_by VARCHAR(64) NOT NULL, created_at DATETIME(6) NOT NULL,
        KEY idx_run(run_id)
    ) ENGINE=InnoDB''')


def probe(req):
    request_id = req.get('request_id', '')
    if not re.fullmatch(r'[a-f0-9]{24}-[0-9]{1,8}', request_id):
        raise ValueError('Invalid request id')
    started = time.monotonic()
    try:
        with connection(proxy=True, timeout=3) as conn:
            hostname = scalar(conn, 'SELECT @@hostname')
            query(conn, 'INSERT INTO fault_probe VALUES(%s,%s,%s,NOW(6))', (request_id, req['token'], hostname))
        return {'ok': True, 'acknowledged': True, 'request_id': request_id, 'backend': hostname,
                'elapsed_ms': round((time.monotonic()-started)*1000, 2)}
    except pymysql.MySQLError as exc:
        # A lost response is NOT proof that a COMMIT did not happen. Reconcile afterwards.
        return {'ok': True, 'acknowledged': False, 'request_id': request_id,
                'commit_status': 'unknown-until-reconciled', 'error_code': exc.args[0] if exc.args else None,
                'error': str(exc), 'elapsed_ms': round((time.monotonic()-started)*1000, 2)}


def json_default(value):
    if isinstance(value, (datetime.datetime, datetime.date, datetime.timedelta, decimal.Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return value.decode('utf8', errors='replace')
    raise TypeError(type(value).__name__)


def cancel_worker(req):
    path = Path('/tmp') / ('mariadb-incident-' + req['token'] + '.json')
    if not path.exists():
        return {'cancelled': False, 'reason': 'no active worker lease'}
    record = json.loads(path.read_text())
    pid = record['pid']
    try:
        cmdline = Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0')
    except FileNotFoundError:
        path.unlink(missing_ok=True)
        return {'cancelled': False, 'reason': 'worker exited'}
    if req['token'].encode() not in cmdline or b'--mariadb-incident-worker' not in cmdline:
        raise ValueError('PID identity changed; no process was killed')
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not path.exists():
            return {'cancelled': True, 'graceful': True}
        time.sleep(0.2)
    raise RuntimeError('Worker still shutting down; rerun scenario repair before starting another scenario')


def execute(req):
    action = req['action']
    if action == 'cancel':
        return cancel_worker(req)
    if action == 'probe':
        return probe(req)
    with connection() as conn:
        if action == 'logging-state':
            return log_state(conn)
        if action == 'restore-logging':
            return restore_logging(conn, req['state'])
        if action not in ('cleanup', 'inspect', 'fix-slow', 'fix-fragmentation', 'reconcile', 'table-stats'):
            ensure_schema(conn, req['owner'])
        else:
            query(conn, 'SET SESSION wsrep_sync_wait=1')
            ensure_schema(conn, req['owner'], create=False)
        if action == 'prepare':
            prepare_probe(conn)
            return {'schema': SCHEMA, 'marker': SIGNATURE, 'owner': req['owner']}
        if action == 'slow':
            return slow_work(conn, req)
        if action == 'fix-slow':
            return slow_work(conn, dict(req, leave_broken=False), create=False)
        if action == 'fragmentation':
            return frag_work(conn, req)
        if action == 'fix-fragmentation':
            return frag_work(conn, dict(req, leave_broken=False), create=False)
        if action == 'lock':
            return lock_work(conn, req)
        if action == 'table-stats':
            return frag_snapshot(conn, req.get('label', 'node-local'))
        if action == 'reconcile':
            query(conn, 'SET SESSION wsrep_sync_wait=1')
            return query(conn, 'SELECT request_id,handled_by FROM fault_probe WHERE run_id=%s ORDER BY request_id', (req['run_id'],))
        if action == 'inspect':
            return {'tables': query(conn, '''SELECT table_name,table_rows,data_length,index_length,data_free
                FROM information_schema.tables WHERE table_schema=%s''', (SCHEMA,)),
                'logging': log_state(conn),
                'locks': query(conn, 'SELECT * FROM information_schema.INNODB_LOCK_WAITS')}
        if action == 'cleanup':
            if req.get('confirm_cleanup') is not True:
                raise ValueError('Cleanup requires explicit confirmation')
            query(conn, 'DROP DATABASE incident_lab')
            return {'removed': 'incident_lab', 'other_schemas': 'unchanged'}
        raise ValueError('Unknown action: ' + action)


def main():
    req = json.load(sys.stdin)
    validate_request(req)
    action = req.get('action')
    lease = None
    if action not in ('cancel', 'probe', 'logging-state', 'restore-logging'):
        lease = Path('/tmp') / ('mariadb-incident-' + req['token'] + '.json')
        fd = os.open(str(lease), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'pid': os.getpid(), 'action': action}, stream)
        def interrupted(signum, frame):
            raise KeyboardInterrupt('Scenario worker terminated; restoring session resources')
        signal.signal(signal.SIGTERM, interrupted)
    try:
        result = execute(req)
        print(json.dumps({'ok': True, 'result': result}, default=json_default))
    finally:
        if lease:
            lease.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'error_type': type(exc).__name__}))
        sys.exit(1)
