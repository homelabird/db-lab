"""Driver contract tests with doubles; no real Kafka/Redis/Elasticsearch service."""
import json
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch
from mvp_app.adapters import Broker, Cache, Search, Settings
from mvp_app.core import new_order, envelope
from mvp_app.worker import consume_loop, acknowledge
from fakes import MemoryCache, MemorySearch

ORDER = new_order({"item": "adapter", "quantity": 1, "unit_price": 10}, "k")

class Response:
    def __init__(self, status=200, data=None): self.status_code, self.data = status, data or {}
    def json(self): return self.data
    def raise_for_status(self):
        if self.status_code >= 400: raise OSError("test-only HTTP error")

class SearchContractTests(unittest.TestCase):
    def search(self, responses):
        session = Mock()
        session.request.side_effect = responses
        return Search(Settings(), session=session), session

    def test_creates_explicit_mapping(self):
        search, session = self.search([Response(404), Response(200)])
        search.initialize()
        body = session.request.call_args.kwargs["json"]
        self.assertEqual(body["settings"]["number_of_replicas"], 0)
        self.assertEqual(body["mappings"]["dynamic"], "strict")
        self.assertEqual(body["mappings"]["properties"]["total"]["type"], "long")

    def test_existing_index_is_not_recreated(self):
        search, session = self.search([Response(200)])
        search.initialize(); self.assertEqual(session.request.call_count, 1)

    def test_non404_head_error_is_not_treated_as_empty(self):
        search, session = self.search([Response(503)])
        with self.assertRaises(OSError): search.initialize()
        self.assertEqual(session.request.call_count, 1)

    def test_put_has_deterministic_id_and_version_fence(self):
        search, session = self.search([Response(200), Response(201)])
        self.assertEqual(search.index(ORDER), "indexed")
        args, kwargs = session.request.call_args
        self.assertTrue(args[1].endswith("/_doc/" + ORDER["id"]))
        self.assertEqual(kwargs["params"], {"version": 1, "version_type": "external"})
        self.assertEqual(kwargs["timeout"], (2, 3))

    def test_older_version_conflict_can_be_committed(self):
        search, _ = self.search([Response(200), Response(409, {"error": {"type": "version_conflict_engine_exception"}}),
                                Response(200, {"_version": 2, "_source": {**ORDER, "version": 2}})])
        self.assertEqual(search.index(ORDER), "older_version_ignored")

    def test_other_conflicts_not_swallowed(self):
        search, _ = self.search([Response(200), Response(409, {"error": {"type": "other"}})])
        with self.assertRaises(OSError): search.index(ORDER)

    def test_mapping_error_not_swallowed(self):
        search, _ = self.search([Response(200), Response(400)])
        with self.assertRaises(OSError): search.index(ORDER)

    def test_search_down_does_not_return_empty_success(self):
        search, _ = self.search([Response(503)])
        with self.assertRaises(OSError): search.find("adapter")

    def test_cache_has_ttl_and_namespace(self):
        client = Mock()
        cache = Cache(Settings(), client)
        cache.put(ORDER)
        args, kwargs = client.set.call_args
        self.assertEqual(args[0], "mvp:order:" + ORDER["id"])
        self.assertEqual(json.loads(args[1]), ORDER)
        self.assertEqual(kwargs["ex"], 30)

    def test_invalid_settings_reject_unbounded_ttl(self):
        with patch.dict("os.environ", {"CACHE_TTL": "0"}), self.assertRaises(ValueError): Settings.load()

    def test_index_requires_mvp_scope(self):
        with patch.dict("os.environ", {"ES_INDEX": "production"}), self.assertRaises(ValueError): Settings.load()


class KafkaContractTests(unittest.TestCase):
    def test_consumer_manual_commit_and_store(self):
        created = []
        def consumer(config): created.append(config); return Mock()
        module = types.SimpleNamespace(Consumer=consumer)
        with patch.dict(sys.modules, {"confluent_kafka": module}): Broker(Settings()).consumer()
        self.assertIs(created[0]["enable.auto.commit"], False)
        self.assertIs(created[0]["enable.auto.offset.store"], False)
        self.assertEqual(created[0]["auto.offset.reset"], "earliest")

    def publish(self, error=None, callback=True, left=0):
        class Producer:
            def __init__(self, config): self.config = config
            def produce(self, topic, **kw): self.callback = kw["on_delivery"]
            def flush(self, timeout):
                if callback: self.callback(error, None)
                return left
        broker = Broker(Settings())
        with patch.dict(sys.modules, {"confluent_kafka": types.SimpleNamespace(Producer=Producer)}):
            broker.publish(envelope(ORDER))
        return broker

    def test_producer_success_waits_callback(self):
        broker = self.publish()
        self.assertTrue(broker._producer.config["enable.idempotence"])
        self.assertEqual(broker._producer.config["acks"], "all")

    def test_producer_error_keeps_outbox_unsent(self):
        with self.assertRaises(RuntimeError): self.publish(error=OSError())

    def test_producer_no_callback_is_not_success(self):
        with self.assertRaises(RuntimeError): self.publish(callback=False)

    def test_producer_inflight_is_not_success(self):
        with self.assertRaises(RuntimeError): self.publish(left=1)

    def test_failed_consume_closes_before_later_poll(self):
        stop = threading.Event()
        message = Mock()
        message.error.return_value = None
        message.value.return_value = json.dumps(envelope(ORDER)).encode()
        message.partition.return_value = 0
        message.offset.return_value = 1
        consumer = Mock()
        consumer.poll.return_value = message
        consumer.close.side_effect = stop.set
        broker = Mock(); broker.consumer.return_value = consumer; broker.s.kafka_topic = "mvp.orders.v1"
        search = MemorySearch(); search.down = True
        consume_loop(stop, broker, search, MemoryCache())
        self.assertEqual(consumer.poll.call_count, 1)
        consumer.commit.assert_not_called()
        consumer.close.assert_called_once()

    def test_successful_consume_commits_specific_message_synchronously(self):
        stop = threading.Event()
        message = Mock()
        message.error.return_value = None
        message.value.return_value = json.dumps(envelope(ORDER)).encode()
        message.partition.return_value = 0; message.offset.return_value = 0
        message.topic.return_value = "mvp.orders.v1"
        consumer = Mock(); consumer.poll.return_value = message
        def commit(**kw):
            stop.set()
            return [types.SimpleNamespace(topic="mvp.orders.v1", partition=0, offset=1, error=None)]
        consumer.commit.side_effect = commit
        broker = Mock(); broker.consumer.return_value = consumer; broker.s.kafka_topic = "mvp.orders.v1"
        consume_loop(stop, broker, MemorySearch(), MemoryCache())
        consumer.commit.assert_called_once_with(message=message, asynchronous=False)

    def test_partition_commit_error_is_not_success(self):
        consumer = Mock()
        consumer.commit.return_value = [types.SimpleNamespace(error=OSError("commit failed"))]
        with self.assertRaises(RuntimeError): acknowledge(consumer, Mock())

    def test_partition_commit_result_success(self):
        consumer = Mock()
        consumer.commit.return_value = [types.SimpleNamespace(topic="mvp.orders.v1", partition=0, offset=4, error=None)]
        message = Mock(); message.topic.return_value = "mvp.orders.v1"
        message.partition.return_value = 0; message.offset.return_value = 3
        acknowledge(consumer, message)
