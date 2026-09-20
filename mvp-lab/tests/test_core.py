import copy
import unittest
from mvp_app.core import (Problem, Storefront, create_input, envelope, identifier, new_order,
                          project_one, relay_once, request_key, transition, validate_event)
from mvp_app.admin import rebuild
from fakes import MemoryRepo, MemoryCache, MemorySearch, MemoryBroker

DATA = {"item": "공부용 키보드", "quantity": 2, "unit_price": 500}

class InputTests(unittest.TestCase):
    def test_valid_input(self):
        self.assertEqual(create_input(DATA), DATA)

    def test_string_is_trimmed(self):
        self.assertEqual(create_input(dict(DATA, item=" a "))["item"], "a")

    def test_rejects_bad_inputs(self):
        for value in [None, [], {}, dict(DATA, quantity=True), dict(DATA, quantity=0),
                      dict(DATA, quantity=1001), dict(DATA, unit_price=-1),
                      dict(DATA, unit_price=0.5), dict(DATA, unit_price=100000001),
                      dict(DATA, item=""), dict(DATA, item="x" * 101)]:
            with self.subTest(value=value), self.assertRaises(Problem):
                create_input(value)

    def test_identifier_rejects_paths(self):
        for value in [None, "../../orders", "a", 123]:
            with self.subTest(value=value), self.assertRaises(Problem):
                identifier(value)

    def test_keys(self):
        self.assertEqual(request_key("study:1"), "study:1")
        self.assertTrue(request_key(None))
        for key in ["a b", "$(touch tmp)", "x" * 81]:
            with self.assertRaises(Problem):
                request_key(key)

    def test_transition(self):
        old = new_order(DATA, "k")
        updated = transition(old, {"status": "paid", "expected_version": 1})
        self.assertEqual(updated["version"], 2)
        self.assertEqual(old["version"], 1)
        self.assertEqual(updated["status"], "paid")

    def test_invalid_transition(self):
        for body in [{"status": "shipped", "expected_version": 1},
                     {"status": "paid", "expected_version": 2}, {"status": "paid"}]:
            with self.assertRaises(Problem):
                transition(new_order(DATA, "k"), body)

    def test_terminal_status(self):
        order = dict(new_order(DATA, "k"), status="shipped")
        with self.assertRaises(Problem):
            transition(order, {"status": "created", "expected_version": 1})

    def test_valid_envelope(self):
        order = new_order(DATA, "k")
        self.assertEqual(validate_event(envelope(order)), order)

    def test_bad_schema_and_total(self):
        event = envelope(new_order(DATA, "k"))
        with self.assertRaises(Problem):
            validate_event(dict(event, schema_version=2))
        event["order"]["total"] = -1
        with self.assertRaises(Problem):
            validate_event(event)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.cache, self.search, self.broker = MemoryRepo(), MemoryCache(), MemorySearch(), MemoryBroker()
        self.shop = Storefront(self.repo, self.cache, self.search)
        self.order, _ = self.shop.create(DATA, "request-1")

    def test_idempotency(self):
        order, created = self.shop.create(DATA, "request-1")
        self.assertFalse(created)
        self.assertEqual(order["id"], self.order["id"])
        self.assertEqual(len(self.repo.outbox), 1)

    def test_idempotency_payload_conflict(self):
        with self.assertRaises(Problem):
            self.shop.create(dict(DATA, quantity=3), "request-1")

    def test_cache_miss_then_hit(self):
        self.assertEqual(self.shop.detail(self.order["id"])["source"], "mariadb")
        self.assertEqual(self.shop.detail(self.order["id"])["source"], "redis")

    def test_cache_down_falls_back(self):
        self.cache.down = True
        result = self.shop.detail(self.order["id"])
        self.assertEqual(result["source"], "mariadb")
        self.assertEqual(result["cache"], "unavailable")

    def test_corrupt_cache_falls_back(self):
        self.cache.data[self.order["id"]] = ({"wrong": 1}, 30)
        self.assertEqual(self.shop.detail(self.order["id"])["source"], "mariadb")

    def test_cached_read_survives_sql_down(self):
        self.shop.detail(self.order["id"])
        self.repo.down = True
        self.assertEqual(self.shop.detail(self.order["id"])["source"], "redis")
        with self.assertRaises(OSError):
            self.shop.detail(self.order["id"], fresh=True)

    def test_update_invalidates_cache(self):
        self.shop.detail(self.order["id"])
        updated = self.shop.update(self.order["id"], {"status": "paid", "expected_version": 1})
        self.assertEqual(updated["version"], 2)
        self.assertEqual(self.shop.detail(self.order["id"])["source"], "mariadb")
        self.assertEqual(len(self.repo.outbox), 2)

    def test_failed_invalidation_recovers_after_ttl(self):
        self.shop.detail(self.order["id"])
        self.cache.down = True
        self.shop.update(self.order["id"], {"status": "paid", "expected_version": 1})
        self.cache.down = False
        self.assertEqual(self.shop.detail(self.order["id"])["order"]["status"], "created")
        self.cache.clock = 31
        self.assertEqual(self.shop.detail(self.order["id"])["order"]["status"], "paid")

    def test_broker_down_keeps_outbox(self):
        self.broker.down = True
        with self.assertRaises(OSError):
            relay_once(self.repo, self.broker)
        self.assertIsNotNone(self.repo.pending_one())
        self.assertEqual(len(self.repo.orders), 1)

    def test_relay_ack_then_mark(self):
        self.assertTrue(relay_once(self.repo, self.broker))
        self.assertIsNone(self.repo.pending_one())
        self.assertFalse(relay_once(self.repo, self.broker))
        self.assertEqual(len(self.broker.events), 1)

    def test_ack_before_crash_causes_same_event_duplicate(self):
        self.repo.fail_mark = True
        with self.assertRaises(OSError):
            relay_once(self.repo, self.broker)
        self.repo.fail_mark = False
        relay_once(self.repo, self.broker)
        self.assertEqual(self.broker.events[0], self.broker.events[1])
        commits = []
        for event in self.broker.events:
            project_one(event, self.search, self.cache, lambda: commits.append(True))
        self.assertEqual(len(self.search.docs), 1)
        self.assertEqual(len(commits), 2)

    def test_es_failure_never_commits(self):
        self.search.down = True
        commits = []
        with self.assertRaises(OSError):
            project_one(envelope(self.order), self.search, self.cache, lambda: commits.append(True))
        self.assertEqual(commits, [])

    def test_cache_failure_does_not_hold_kafka_offset(self):
        self.cache.down = True
        commits = []
        project_one(envelope(self.order), self.search, self.cache, lambda: commits.append(True))
        self.assertEqual(commits, [True])

    def test_old_event_cannot_overwrite_new(self):
        newer = dict(self.order, version=2, status="paid")
        for order in (newer, self.order, newer):
            project_one(envelope(order), self.search, self.cache, lambda: None)
        self.assertEqual(self.search.docs[self.order["id"]]["version"], 2)

    def test_commit_failure_can_reapply_safely(self):
        def fail():
            raise OSError("commit uncertain")
        with self.assertRaises(OSError):
            project_one(envelope(self.order), self.search, self.cache, fail)
        project_one(envelope(self.order), self.search, self.cache, lambda: None)
        self.assertEqual(len(self.search.docs), 1)

    def test_invalid_event_is_not_committed(self):
        event = envelope(self.order); event["schema_version"] = 2
        commits = []
        with self.assertRaises(Problem):
            project_one(event, self.search, self.cache, lambda: commits.append(True))
        self.assertEqual(commits, [])

    def test_fresh_cache_is_miss_without_data_loss(self):
        self.shop.detail(self.order["id"])
        self.shop.cache = MemoryCache()
        self.assertEqual(self.shop.detail(self.order["id"])["source"], "mariadb")
        self.assertEqual(len(self.repo.orders), 1)

    def test_search_rebuild_needs_sql_and_not_kafka(self):
        self.broker.down = True
        self.assertEqual(rebuild(self.repo, self.search), {"attempted": 1, "indexed": 1, "duplicates": 0, "skipped_older_version": 0, "errors": 0})
        self.assertEqual(self.search.docs[self.order["id"]], self.order)
        self.assertEqual(len(self.broker.events), 0)

    def test_rebuild_version_fence_preserves_newer_projection(self):
        self.search.index(dict(self.order, version=3, status="shipped"))
        rebuild(self.repo, self.search)
        self.assertEqual(self.search.docs[self.order["id"]]["version"], 3)
