"""Bounded logical snapshot of the two application tables, NOT a physical/full-server backup.
Export uses one InnoDB consistent snapshot. Restore only runs in a disposable namespace.
"""
from __future__ import annotations
from .observability import safe_error
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from .adapters import SQL, Settings

FORMAT = 'db-lab-app-snapshot-v1'
MAX_ROWS = 5000
MAX_BYTES = 16 * 1024 * 1024
COLUMNS = {
    'orders': ('id', 'idempotency_key', 'item', 'quantity', 'unit_price', 'total', 'status', 'version', 'created_at'),
    'outbox': ('seq', 'event_id', 'payload', 'created_at', 'sent_at'),
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def seal(tables, server_version):
    body = {'format': FORMAT, 'tables': tables}
    raw = canonical(body)
    if len(raw) > MAX_BYTES:
        raise ValueError('Snapshot exceeds the 16MiB study limit')
    result = {**body, 'sha256': hashlib.sha256(raw).hexdigest(), 'server_version': server_version}
    if len(canonical(result)) > MAX_BYTES:
        raise ValueError('Snapshot exceeds the 16MiB input/output limit')
    return result


def validate(value):
    if not isinstance(value, dict) or set(value) != {'format', 'tables', 'sha256', 'server_version'}:
        raise ValueError('Unexpected snapshot structure')
    if value['format'] != FORMAT or not isinstance(value['tables'], dict) or set(value['tables']) != set(COLUMNS):
        raise ValueError('Unsupported application snapshot schema')
    for table, columns in COLUMNS.items():
        rows = value['tables'][table]
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError('Snapshot row limit exceeded')
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(columns):
                raise ValueError('Unexpected row columns')
            if any(type(v) not in (str, int, type(None)) for v in row.values()):
                raise ValueError('Only scalar snapshot values are accepted')
            key = row['id' if table == 'orders' else 'seq']
            if key in seen:
                raise ValueError('Duplicate snapshot primary key')
            seen.add(key)
    expected = seal(value['tables'], value['server_version'])['sha256']
    if value['sha256'] != expected:
        raise ValueError('Snapshot checksum mismatch; restore refused')
    return value


def export_snapshot(repo):
    conn = repo.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION time_zone='+00:00'")
            cur.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            cur.execute('SELECT VERSION() AS server_version')
            version = cur.fetchone()['server_version']
            cur.execute("SELECT TABLE_NAME AS name, ENGINE AS engine FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
            engines = {r['name']: r['engine'] for r in cur.fetchall()}
            if engines != {'orders': 'InnoDB', 'outbox': 'InnoDB'}:
                raise ValueError('Expected exactly orders/outbox InnoDB tables; no partial backup implied')
            # Check payload sizes INSIDE the same read view before fetching LONGTEXT rows.
            # Protect the source API container from accidentally buffering a huge poison payload.
            cur.execute('SELECT COUNT(*) AS row_count, COALESCE(SUM(OCTET_LENGTH(payload)),0) AS payload_bytes FROM outbox')
            budget = cur.fetchone()
            if budget['row_count'] > MAX_ROWS or budget['payload_bytes'] > MAX_BYTES // 4:
                raise ValueError('Snapshot outbox row/text budget exceeded')
            tables = {}
            for table, columns in COLUMNS.items():
                order = 'id' if table == 'orders' else 'seq'
                cur.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {order} LIMIT {MAX_ROWS + 1}")
                rows = cur.fetchall()
                if len(rows) > MAX_ROWS:
                    raise ValueError('Snapshot row limit exceeded')
                tables[table] = [{k: (v.strftime('%Y-%m-%d %H:%M:%S.%f') if isinstance(v, datetime) else v)
                                  for k, v in row.items()} for row in rows]
        return validate(seal(tables, version))
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def disposable(settings):
    if (not re.fullmatch(r'drill-[a-f0-9]{12}', os.environ.get('MVP_DISPOSABLE', ''))
            or settings.sql_host != '127.0.0.1' or settings.sql_database != 'mvp' or settings.sql_user != 'mvp'):
        raise ValueError('Restore is restricted to an isolated disposable loopback database')


def restore_snapshot(repo, snapshot):
    disposable(repo.s)
    validate(snapshot)  # integrity is checked BEFORE any schema/data change
    with repo.transaction() as conn, conn.cursor() as cur:
        cur.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')
        if cur.fetchall():
            raise ValueError('Restore requires an EMPTY disposable schema; no overwrite/retry on partial restore')
    repo.initialize()
    with repo.transaction() as conn, conn.cursor() as cur:
        cur.execute("SET SESSION time_zone='+00:00'")
        for table, columns in COLUMNS.items():
            marks = ','.join(['%s'] * len(columns))
            statement = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})"
            for row in snapshot['tables'][table]:
                cur.execute(statement, tuple(row[k] for k in columns))
    restored = export_snapshot(repo)
    match = restored['sha256'] == snapshot['sha256']
    canary_ok = False
    if match:
        key = os.environ['MVP_DISPOSABLE'] + '-restore-canary'
        payload = {'item': key, 'quantity': 2, 'unit_price': 13}
        order, created = repo.create(payload, key)
        repeated, duplicate_created = repo.create(payload, key)
        changed = repo.update(order['id'], {'status': 'paid', 'expected_version': order['version']})
        canary_ok = (created and not duplicate_created and repeated == order and changed['status'] == 'paid'
                     and changed['version'] == order['version'] + 1 and repo.get(order['id']) == changed)
    return {'matched': match, 'canary_passed': canary_ok,
            'canary_scope': 'after row audit, disposable-only create/idempotency/update/read; no worker/Kafka or ES restored', 'source_sha256': snapshot['sha256'], 'restored_sha256': restored['sha256'],
            'source_version': snapshot['server_version'], 'restored_version': restored['server_version'],
            'rows': {k: len(v) for k, v in restored['tables'].items()},
            'scope': 'application rows only; not users/grants/binlogs/DDL migration or physical downgrade'}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('export', 'restore'))
    a = p.parse_args()
    try:
        repo = SQL(Settings.load())
        if a.action == 'export':
            print(canonical(export_snapshot(repo)).decode('utf-8'))
            return 0
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Snapshot input too large')
        result = restore_snapshot(repo, json.loads(raw))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['matched'] and result['canary_passed'] else 1
    except Exception as exc:
        # Do not log order contents, credentials or driver connection strings.
        print(json.dumps({'stage': 'snapshot_failed', **safe_error(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
