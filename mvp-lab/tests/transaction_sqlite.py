"""TEST ONLY. Production SQL DML on SQLite, NOT MariaDB parser/row locks/durability.

SQLite uses a database-wide write lock (BEGIN IMMEDIATE) in these tests. Session SET,
FOR UPDATE, table index syntax and storage engine clauses are adapted. No production
module imports this file; a runtime lacking MariaDB must fail, never use SQLite.
"""
import re
import sqlite3


class EngineError(Exception):
    pass


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.cur = connection.db.cursor()
        self.fake = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cur.close()

    @property
    def rowcount(self):
        return self.cur.rowcount

    def execute(self, statement, params=()):
        self.connection.calls.append(statement)
        self.fake = None
        if statement.startswith('SET SESSION'):
            self.fake = []
            return
        if statement.startswith('SELECT DATABASE()'):
            self.fake = [{'db': 'txlab', 'version': 'SQLITE-TEST-ADAPTER-NOT-MARIADB'}]
            return
        if 'information_schema.TABLES' in statement:
            statement = "SELECT name AS TABLE_NAME FROM sqlite_master WHERE type='table'"
        if statement.startswith('START TRANSACTION'):
            statement = 'BEGIN'
        # FOR UPDATE is represented only by SQLite's coarse write-intent lock.
        if getattr(self.connection, 'defer_write_begin', False) and ' FOR UPDATE' in statement and not self.connection.db.in_transaction:
            self.connection.db.execute('BEGIN IMMEDIATE')
        statement = statement.replace('%s', '?').replace(' FOR UPDATE', '').replace(' ENGINE=InnoDB', '')
        statement = statement.replace('CHARACTER SET ascii COLLATE ascii_bin', '')
        statement = re.sub(r',\s*INDEX\(case_id\)', '', statement)
        if getattr(self.connection, 'defer_write_begin', False) and statement.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) and not self.connection.db.in_transaction:
            self.connection.db.execute('BEGIN IMMEDIATE')
        try:
            return self.cur.execute(statement, params)
        except sqlite3.IntegrityError as exc:
            raise EngineError(1062, 'unique fixture conflict') from exc

    def fetchone(self):
        if self.fake is not None:
            return self.fake.pop(0) if self.fake else None
        row = self.cur.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        if self.fake is not None:
            rows, self.fake = self.fake, []
            return rows
        return [dict(row) for row in self.cur.fetchall()]


class Connection:
    def __init__(self, path, calls=None):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=3)
        self.db.row_factory = sqlite3.Row
        self.calls = calls if calls is not None else []

    def cursor(self):
        return Cursor(self)

    def begin(self):
        self.db.execute('BEGIN IMMEDIATE')

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        self.db.close()


class DeferredWriteConnection(Connection):
    """TEST schedule adapter: SELECTs before the first DML are outside a transaction.

    This permits the runner's stale-read barrier on SQLite, which otherwise upgrades
    a read transaction with SQLITE_BUSY. Writes use SQLite's coarse database lock.
    It executes actual repository DML, but is NOT proof of MariaDB READ COMMITTED,
    unique-key waiting, FOR UPDATE, isolation, deadlocks or rollback-on-timeout semantics.
    """
    defer_write_begin = True

    def begin(self):
        pass
