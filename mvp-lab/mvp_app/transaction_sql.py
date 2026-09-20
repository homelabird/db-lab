"""Real MariaDB transaction lab. Never called by the normal API/worker.

All six tables live in a fresh disposable txlab database. The intentionally unsafe
policies are negative controls, not selectable production behaviors. Connections are
owned by one thread; all SQL values are bound parameters and all failures close them.
"""
from __future__ import annotations
import json
import os
import re
import threading
import time
import uuid
from typing import Callable
from .adapters import SQL, Settings
from .transaction_model import (Purchase, Rejected, InjectedFailure, CommitUnknown,
                               TABLES, MAX_ROWS, canonical)

POLICIES = {'protected', 'stale-stock', 'no-claim', 'split-commit', 'unchecked-replay'}


def guard(settings: Settings) -> str:
    run = os.environ.get('MVP_DISPOSABLE', '')
    if (not re.fullmatch(r'drill-[a-f0-9]{12}', run)
            or settings.sql_host != '127.0.0.1' or settings.sql_port != 3306
            or settings.sql_database != 'txlab' or settings.sql_user != 'txlab'):
        raise ValueError('Transaction lessons require a disposable loopback txlab database/user')
    return run


class Trace:
    def __init__(self):
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.started = time.monotonic()

    def add(self, stage: str, **fields) -> None:
        with self.lock:
            if len(self.events) >= 4096:
                raise RuntimeError('Transaction trace budget exceeded')
            self.events.append({'seq': len(self.events), 'stage': stage,
                                'elapsed_ms': round((time.monotonic() - self.started) * 1000, 3), **fields})


class _ExistingKey(Exception):
    pass


class Repository:
    def __init__(self, settings: Settings, *, connect: Callable | None = None, trace: Trace | None = None):
        self.run_id = guard(settings)  # Validate BEFORE even connecting.
        self.sql = SQL(settings, connect=connect)
        self.trace = trace or Trace()
        self.server_version = None

    def connect(self):
        guard(self.sql.s)
        conn = self.sql.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
                cur.execute("SET SESSION time_zone='+00:00'")
            return conn
        except BaseException:
            conn.close()
            raise

    def initialize(self) -> None:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT DATABASE() AS db, VERSION() AS version')
                info = cur.fetchone()
                if info['db'] != 'txlab':
                    raise ValueError('Unexpected selected schema')
                self.server_version = info['version']
                cur.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')
                if cur.fetchall():
                    raise ValueError('Requires an EMPTY disposable schema; no migration, DROP or overwrite')
                # DDL is deliberately outside the checkout transactions: DDL may implicitly commit.
                definitions = (
                    '''CREATE TABLE inventory (case_id VARCHAR(40) PRIMARY KEY, initial_qty BIGINT NOT NULL,
                       available BIGINT NOT NULL, unit_price BIGINT NOT NULL) ENGINE=InnoDB''',
                    '''CREATE TABLE wallets (case_id VARCHAR(40) NOT NULL, owner VARCHAR(20) NOT NULL,
                       initial_balance BIGINT NOT NULL, balance BIGINT NOT NULL,
                       PRIMARY KEY(case_id, owner)) ENGINE=InnoDB''',
                    '''CREATE TABLE requests (case_id VARCHAR(40) NOT NULL,
                       request_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                       fingerprint CHAR(64) NOT NULL, order_id CHAR(32) NOT NULL,
                       PRIMARY KEY(case_id, request_key)) ENGINE=InnoDB''',
                    '''CREATE TABLE orders (id CHAR(32) PRIMARY KEY, case_id VARCHAR(40) NOT NULL,
                       request_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                       quantity BIGINT NOT NULL, amount BIGINT NOT NULL, state VARCHAR(20) NOT NULL,
                       INDEX(case_id)) ENGINE=InnoDB''',
                    '''CREATE TABLE ledger (id CHAR(32) PRIMARY KEY, case_id VARCHAR(40) NOT NULL,
                       order_id CHAR(32) NOT NULL, owner VARCHAR(20) NOT NULL, phase VARCHAR(20) NOT NULL,
                       delta BIGINT NOT NULL, INDEX(case_id)) ENGINE=InnoDB''',
                    '''CREATE TABLE events (id CHAR(32) PRIMARY KEY, case_id VARCHAR(40) NOT NULL,
                       order_id CHAR(32) NOT NULL, kind VARCHAR(20) NOT NULL, payload LONGTEXT NOT NULL,
                       INDEX(case_id)) ENGINE=InnoDB''',
                )
                for statement in definitions:
                    cur.execute(statement)
            conn.commit()
        finally:
            conn.close()

    def seed(self, case_id: str, *, stock: int, balance: int, price: int) -> None:
        Purchase(case_id, 'seed')
        if any(type(v) is not int or not 0 <= v <= 1_000_000 for v in (stock, balance, price)) or price < 1:
            raise ValueError('Invalid fixture quantities')
        conn = self.connect()
        try:
            conn.begin()
            with conn.cursor() as cur:
                cur.execute('INSERT INTO inventory (case_id,initial_qty,available,unit_price) VALUES (%s,%s,%s,%s)',
                            (case_id, stock, stock, price))
                for owner, amount in (('buyer', balance), ('merchant', 0)):
                    cur.execute('INSERT INTO wallets (case_id,owner,initial_balance,balance) VALUES (%s,%s,%s,%s)',
                                (case_id, owner, amount, amount))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _order(cur, oid: str):
        cur.execute('SELECT * FROM orders WHERE id=%s', (oid,))
        return cur.fetchone()

    def lookup(self, request: Purchase, *, verify_payload: bool = True) -> dict | None:
        # Fresh connection/read view after a failed INSERT; no retry can create an order here.
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM requests WHERE case_id=%s AND request_key=%s', (request.case_id, request.key))
                claim = cur.fetchone()
                if not claim:
                    return None
                if verify_payload and claim['fingerprint'] != request.fingerprint:
                    raise Rejected('idempotency_conflict')
                order = self._order(cur, claim['order_id'])
                if (not order or order['case_id'] != request.case_id or order['request_key'] != request.key
                        or (verify_payload and order['quantity'] != request.quantity)):
                    raise RuntimeError('Committed request claim has no matching order')
                return order
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def purchase(self, request: Purchase, *, policy: str = 'protected', after_read=None,
                 fail_at: str | None = None, lose_response: bool = False) -> dict:
        if policy not in POLICIES:
            raise ValueError('Unknown checkout policy')
        from .transaction_model import FAILPOINTS
        if fail_at is not None and fail_at not in FAILPOINTS:
            raise ValueError('Unknown transaction failure point')
        if type(lose_response) is not bool:
            raise ValueError('Invalid commit observation mode')
        # Retry only lock/deadlock failures after _purchase has rolled back AND closed.
        # A transport failure at commit is NEVER treated as a confirmed rollback.
        for attempt in range(3):
            try:
                return self._purchase(request, policy, after_read, fail_at, lose_response)
            except _ExistingKey:
                order = self.lookup(request, verify_payload=policy != 'unchecked-replay')
                if order is None:
                    raise RuntimeError('Unique request conflict without a committed request')
                self.trace.add('idempotent_replay', case=request.case_id, key=request.key, order_id=order['id'])
                return {'outcome': 'replayed', 'order': order}
            except (InjectedFailure, CommitUnknown, Rejected):
                raise
            except Exception as exc:
                code = exc.args[0] if exc.args and type(exc.args[0]) is int else None
                if code not in (1205, 1213) or attempt == 2 or policy not in {'protected', 'unchecked-replay'}:
                    raise
                self.trace.add('retry_after_rollback', case=request.case_id, sql_code=code, attempt=attempt + 1)
                time.sleep(0.02 * (attempt + 1))
        raise AssertionError('unreachable')

    def _purchase(self, req, policy, after_read, fail_at, lose_response):
        conn = self.connect()
        oid = uuid.uuid4().hex
        protected = policy in {'protected', 'unchecked-replay'}
        case, key = req.case_id, req.key
        self.trace.add('checkout_begin', case=case, key=key, policy=policy)

        def checkpoint(stage):
            if policy == 'split-commit':
                conn.commit()
                self.trace.add('unsafe_partial_commit', case=case, key=key, boundary=stage)
            self.trace.add('statement_boundary', case=case, key=key, boundary=stage)
            if fail_at == stage:
                raise InjectedFailure(stage)

        try:
            conn.begin()
            with conn.cursor() as cur:
                if protected:
                    try:
                        cur.execute('INSERT INTO requests (case_id,request_key,fingerprint,order_id) VALUES (%s,%s,%s,%s)',
                                    (case, key, req.fingerprint, oid))
                    except Exception as exc:
                        # Restrict duplicate interpretation to THIS INSERT, not arbitrary later SQL.
                        if exc.args and exc.args[0] == 1062:
                            raise _ExistingKey() from None
                        raise
                cur.execute('SELECT * FROM inventory WHERE case_id=%s', (case,))
                item = cur.fetchone()
                if not item:
                    raise Rejected('unknown_case')
                amount = req.quantity * item['unit_price']
                if policy == 'stale-stock':
                    self.trace.add('unlocked_stock_read', case=case, key=key, available=item['available'])
                    if after_read:
                        after_read()
                    if item['available'] < req.quantity:
                        raise Rejected('sold_out')
                    # INTENTIONAL LOST UPDATE: every racer writes its previously read value.
                    cur.execute('UPDATE inventory SET available=%s WHERE case_id=%s', (item['available']-req.quantity, case))
                else:
                    cur.execute('UPDATE inventory SET available=available-%s WHERE case_id=%s AND available>=%s',
                                (req.quantity, case, req.quantity))
                    if cur.rowcount != 1:
                        raise Rejected('sold_out')
                checkpoint('inventory')
                cur.execute("UPDATE wallets SET balance=balance-%s WHERE case_id=%s AND owner='buyer' AND balance>=%s", (amount, case, amount))
                if cur.rowcount != 1:
                    raise Rejected('insufficient_funds')
                checkpoint('debit')
                cur.execute("UPDATE wallets SET balance=balance+%s WHERE case_id=%s AND owner='merchant'", (amount, case))
                if cur.rowcount != 1:
                    raise RuntimeError('Merchant fixture missing')
                checkpoint('credit')
                order = {'id': oid, 'case_id': case, 'request_key': key, 'quantity': req.quantity, 'amount': amount, 'state': 'paid'}
                cur.execute('INSERT INTO orders (id,case_id,request_key,quantity,amount,state) VALUES (%s,%s,%s,%s,%s,%s)',
                            tuple(order[k] for k in ('id', 'case_id', 'request_key', 'quantity', 'amount', 'state')))
                checkpoint('order')
                self._ledger(cur, order, 'paid')
                checkpoint('ledger')
                self._event(cur, order, 'paid')
                checkpoint('outbox')
            try:
                conn.commit()
            except Exception as exc:
                self.trace.add('commit_response_unknown', case=case, key=key, simulated=False)
                raise CommitUnknown('commit result cannot be established; query same key') from exc
            self.trace.add('commit_confirmed', case=case, key=key, order_id=oid)
            if lose_response:
                self.trace.add('commit_response_unknown', case=case, key=key, simulated=True)
                raise CommitUnknown('deliberate response loss AFTER confirmed DB commit')
            return {'outcome': 'created', 'order': order}
        except CommitUnknown:
            # Closing a connection is not proof that its COMMIT did or did not take effect.
            raise
        except BaseException:
            conn.rollback()
            self.trace.add('rollback_completed', case=case, key=key)
            raise
        finally:
            conn.close()

    @staticmethod
    def _ledger(cur, order: dict, phase: str) -> None:
        sign = -1 if phase == 'paid' else 1
        for owner, delta in (('buyer', sign * order['amount']), ('merchant', -sign * order['amount'])):
            cur.execute('INSERT INTO ledger (id,case_id,order_id,owner,phase,delta) VALUES (%s,%s,%s,%s,%s,%s)',
                        (uuid.uuid4().hex, order['case_id'], order['id'], owner, phase, delta))

    @staticmethod
    def _event(cur, order: dict, phase: str) -> None:
        payload = canonical({'order_id': order['id'], 'quantity': order['quantity'], 'amount': order['amount'], 'state': phase}).decode()
        cur.execute('INSERT INTO events (id,case_id,order_id,kind,payload) VALUES (%s,%s,%s,%s,%s)',
                    (uuid.uuid4().hex, order['case_id'], order['id'], phase, payload))

    def refund(self, case_id: str, oid: str, *, protected: bool = True, after_read=None) -> dict:
        Purchase(case_id, 'refund')
        if not isinstance(oid, str) or not re.fullmatch(r'[a-f0-9]{32}', oid):
            raise ValueError('Invalid synthetic order ID')
        conn = self.connect()
        try:
            conn.begin()
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM orders WHERE id=%s AND case_id=%s' + (' FOR UPDATE' if protected else ''), (oid, case_id))
                order = cur.fetchone()
                if not order:
                    raise Rejected('order_missing')
                if order['state'] == 'refunded':
                    conn.rollback()
                    self.trace.add('refund_replay', case=case_id, order_id=oid)
                    return {'outcome': 'replayed', 'order_id': oid}
                if order['state'] != 'paid':
                    raise Rejected('invalid_refund_state')
                if after_read:
                    after_read()
                self.trace.add('refund_state_read', case=case_id, order_id=oid, protected=protected)
                cur.execute('UPDATE inventory SET available=available+%s WHERE case_id=%s', (order['quantity'], case_id))
                cur.execute("UPDATE wallets SET balance=balance+%s WHERE case_id=%s AND owner='buyer'", (order['amount'], case_id))
                cur.execute("UPDATE wallets SET balance=balance-%s WHERE case_id=%s AND owner='merchant'", (order['amount'], case_id))
                cur.execute("UPDATE orders SET state='refunded' WHERE id=%s", (oid,))
                self._ledger(cur, order, 'refunded')
                self._event(cur, order, 'refunded')
            try:
                conn.commit()
            except Exception as exc:
                raise CommitUnknown('refund commit result unknown; inspect order before retry') from exc
            self.trace.add('refund_committed', case=case_id, order_id=oid)
            return {'outcome': 'refunded', 'order_id': oid}
        except CommitUnknown:
            raise
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def snapshot(self, case_id: str) -> dict:
        Purchase(case_id, 'snapshot')
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                result = {}
                for table in TABLES:
                    # Identifiers come only from the module constant, not from input.
                    sort = 'case_id' if table == 'inventory' else ('owner' if table == 'wallets' else ('request_key' if table == 'requests' else 'id'))
                    cur.execute(f'SELECT * FROM `{table}` WHERE case_id=%s ORDER BY `{sort}` LIMIT {MAX_ROWS + 1}', (case_id,))
                    rows = cur.fetchall()
                    if len(rows) > MAX_ROWS:
                        raise RuntimeError('Transaction snapshot row budget exceeded')
                    result[table] = list(rows)
                return result
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()
