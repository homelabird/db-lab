"""Actual repository DML executed through a SQLite test adapter.
NOT MariaDB parser, isolation, Galera, authentication or durability verification.
"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from mvp_app.adapters import SQL, Settings
from mvp_app.core import Problem, order_page_input

DATA = {"item": "adapter study", "quantity": 3, "unit_price": 10}

class Cursor:
    def __init__(self, con, fail):
        self.cur, self.fail = con.cursor(), fail

    def __enter__(self): return self
    def __exit__(self, *args): self.cur.close()
    def execute(self, sql, args=()):
        if self.fail and "INSERT INTO outbox" in sql:
            raise OSError("injected outbox failure")
        sql = sql.replace("%s", "?").replace(" FOR UPDATE", "").replace("CURRENT_TIMESTAMP(3)", "CURRENT_TIMESTAMP")
        return self.cur.execute(sql, args)
    def fetchone(self):
        row = self.cur.fetchone()
        return dict(row) if row else None
    def fetchall(self): return [dict(row) for row in self.cur.fetchall()]

class Connection:
    def __init__(self, path, fail=False):
        self.con = sqlite3.connect(path); self.con.row_factory = sqlite3.Row; self.fail = fail
    def cursor(self): return Cursor(self.con, self.fail)
    def commit(self): self.con.commit()
    def rollback(self): self.con.rollback()
    def close(self): self.con.close()

class SQLContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "sql.sqlite3")
        self.fail = False
        with closing(sqlite3.connect(self.path)) as con:
            con.executescript('''CREATE TABLE orders (
                id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, item TEXT, quantity INTEGER,
                unit_price INTEGER, total INTEGER, status TEXT, version INTEGER, created_at TEXT);
                CREATE TABLE outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE,
                payload TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, sent_at TEXT);''')
        self.repo = SQL(Settings(), connect=lambda: Connection(self.path, self.fail))

    def test_create_commits_order_and_event_together(self):
        order, created = self.repo.create(DATA, "key1")
        self.assertTrue(created)
        self.assertEqual(self.repo.get(order["id"]), order)
        self.assertEqual(self.repo.pending_one()["event"]["order"], order)

    def test_outbox_insert_failure_rolls_back_order(self):
        self.fail = True
        with self.assertRaises(OSError):
            self.repo.create(DATA, "key1")
        self.fail = False
        self.assertEqual(self.repo.list_orders(), [])
        self.assertIsNone(self.repo.pending_one())

    def test_duplicate_key_returns_same_order_without_second_event(self):
        first, _ = self.repo.create(DATA, "k")
        second, created = self.repo.create(DATA, "k")
        self.assertEqual(first, second); self.assertFalse(created)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_key_conflict_rejects_different_payload(self):
        self.repo.create(DATA, "k")
        with self.assertRaises(Problem):
            self.repo.create(dict(DATA, quantity=4), "k")

    def test_update_and_event_commit_together(self):
        order, _ = self.repo.create(DATA, "k")
        updated = self.repo.update(order["id"], {"status": "paid", "expected_version": 1})
        self.assertEqual(self.repo.get(order["id"]), updated)
        with closing(sqlite3.connect(self.path)) as con:
            event = json.loads(con.execute("SELECT payload FROM outbox ORDER BY seq DESC LIMIT 1").fetchone()[0])
        self.assertEqual(event["order"]["version"], 2)

    def test_update_outbox_failure_rolls_back_status(self):
        order, _ = self.repo.create(DATA, "k")
        self.fail = True
        with self.assertRaises(OSError):
            self.repo.update(order["id"], {"status": "paid", "expected_version": 1})
        self.fail = False
        self.assertEqual(self.repo.get(order["id"])["status"], "created")

    def test_mark_sent_only_hides_acknowledged_row(self):
        self.repo.create(DATA, "k")
        row = self.repo.pending_one()
        self.repo.mark_sent(row["seq"])
        self.assertIsNone(self.repo.pending_one())
        self.assertEqual(len(self.repo.list_orders()), 1)

    def test_stale_update_rejected(self):
        order, _ = self.repo.create(DATA, "k")
        with self.assertRaises(Problem):
            self.repo.update(order["id"], {"status": "paid", "expected_version": 9})

    def test_parameter_binding_does_not_execute_user_sql(self):
        data = dict(DATA, item="x'); DROP TABLE orders; --")
        order, _ = self.repo.create(data, "sql-test")
        self.assertEqual(self.repo.get(order["id"])["item"], data["item"])
        self.assertEqual(len(self.repo.list_orders()), 1)

    def test_keyset_scan(self):
        for n in range(205):
            self.repo.create(DATA, f"k{n}")
        items = list(self.repo.scan())
        self.assertEqual(len(items), 205)
        self.assertEqual([i["id"] for i in items], sorted(i["id"] for i in items))

    def test_order_pages_cover_more_than_100_rows_with_timestamp_ties(self):
        for n in range(103):
            self.repo.create(dict(DATA, item=f"page {n}"), f"page-{n}")
        with closing(sqlite3.connect(self.path)) as con:
            con.execute("UPDATE orders SET created_at='2026-01-01T00:00:00+00:00'"); con.commit()
        found, cursor = [], None
        while True:
            query = {"limit": ["17"]}
            if cursor: query["cursor"] = [cursor]
            page = self.repo.list_page(**order_page_input(query))
            found.extend(o["id"] for o in page["orders"])
            if not page["has_more"]: break
            cursor = page["next_cursor"]
        self.assertEqual(len(found), 103)
        self.assertEqual(found, sorted(set(found), reverse=True))

    def test_page_filters_treat_wildcards_and_sql_as_literal_text(self):
        a, _ = self.repo.create(dict(DATA, item="Special %_! Keyboard"), "literal")
        b, _ = self.repo.create(dict(DATA, item="Keyboard normal"), "normal")
        self.repo.update(b["id"], {"status":"paid", "expected_version":1})
        self.assertEqual([o["id"] for o in self.repo.list_page(q="%_!")["orders"]], [a["id"]])
        self.assertEqual(len(self.repo.list_page(q="keyboard")["orders"]), 2)
        self.assertEqual([o["id"] for o in self.repo.list_page(status="paid")["orders"]], [b["id"]])
        self.assertEqual(self.repo.list_page(q="' OR 1=1 --")["orders"], [])
        self.assertEqual(len(self.repo.list_orders()), 2)

    def test_newer_insert_between_pages_does_not_shift_the_cursor(self):
        for n in range(4): self.repo.create(DATA, f"before-{n}")
        first = self.repo.list_page(limit=2)
        new, _ = self.repo.create(DATA, "newer")
        with closing(sqlite3.connect(self.path)) as con:
            con.execute("UPDATE orders SET created_at='2099-01-01T00:00:00+00:00' WHERE id=?", (new["id"],)); con.commit()
        second = self.repo.list_page(**order_page_input({"limit":["2"], "cursor":[first["next_cursor"]]}))
        self.assertFalse(set(o["id"] for o in first["orders"]) & set(o["id"] for o in second["orders"]))
        self.assertEqual(len(second["orders"]), 2)
        self.assertNotIn(new["id"], [o["id"] for o in second["orders"]])
