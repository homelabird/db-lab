"""Real Kafka/Elasticsearch/Redis transport for bounded, isolated message drills.

No replacement in-memory runtime. Tests inject their own explicit doubles.
"""
from __future__ import annotations
from dataclasses import replace
import json
import time
from .adapters import Broker, Cache, Search
from .core import ORDER_FIELDS
from .message_safety import MAX_MESSAGE, canonical


class ScopedCache(Cache):
    def __init__(self, settings, scope, client=None):
        super().__init__(settings, client)
        self.scope = scope

    def get(self, order_id):
        raw = self.client.get(self.scope.cache_prefix + order_id)
        return json.loads(raw) if raw is not None else None

    def put(self, order):
        self.client.set(self.scope.cache_prefix + order['id'], canonical(order), ex=30)

    def delete(self, order_id):
        self.client.delete(self.scope.cache_prefix + order_id)


class MessageRuntime:
    evidence_kind = "REAL-DRIVERS-ISOLATED-RESOURCES"
    def __init__(self, settings, scope):
        from confluent_kafka.admin import AdminClient
        self.scope = scope
        self.s = replace(settings, kafka_topic=scope.topic, kafka_group=scope.group, es_index=scope.index)
        self.broker = Broker(self.s)
        self.admin = AdminClient(self.broker.client_config())
        self.search, self.cache = Search(self.s), ScopedCache(self.s, scope)
        self.producer = None
        self.sent = 0

    def topic_names(self):
        return set(self.admin.list_topics(timeout=5).topics)

    def describe(self, name):
        from confluent_kafka import TopicCollection
        if name not in (self.scope.topic, self.scope.dlq):
            raise ValueError('Topic outside message run')
        result = self.admin.describe_topics(TopicCollection([name]), request_timeout=5)[name].result(timeout=8)
        topic_id = str(result.topic_id)
        if not topic_id or topic_id in {'None', 'AAAAAAAAAAAAAAAAAAAAAA'} or result.is_internal or len(result.partitions) != 1:
            raise RuntimeError('Invalid topic identity/partition count')
        return topic_id

    def create_topic(self, name):
        if name not in (self.scope.topic, self.scope.dlq):
            raise ValueError('Foreign topic')
        if name in self.topic_names():
            raise RuntimeError('Study topic already exists; never adopt existing data')
        from confluent_kafka.admin import NewTopic
        topic = NewTopic(name, num_partitions=1, replication_factor=1,
                         config={'retention.ms': '86400000', 'retention.bytes': '1048576',
                                 'max.message.bytes': '65536', 'segment.bytes': '1048576'})
        self.admin.create_topics([topic], request_timeout=5)[name].result(timeout=8)
        return {'name': name, 'topic_id': self.describe(name)}

    def create_index(self):
        response = self.search.request('HEAD')
        if response.status_code != 404:
            response.raise_for_status()
            raise RuntimeError('Study index already exists; not adopted')
        fields = {'id': {'type': 'keyword'}, 'item': {'type': 'text'},
                  'quantity': {'type': 'integer'}, 'unit_price': {'type': 'long'},
                  'total': {'type': 'long'}, 'status': {'type': 'keyword'},
                  'version': {'type': 'long'}, 'created_at': {'type': 'date'}}
        self.search.request('PUT', json={'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
            'mappings': {'dynamic': 'strict', '_meta': {'study_owner': self.scope.token, 'study_run': self.scope.run_id},
                         'properties': fields}}).raise_for_status()
        return {'name': self.scope.index, 'index_uuid': self.index_identity()}

    def index_identity(self):
        response = self.search.request('GET', '/_settings', params={'flat_settings': 'true'})
        response.raise_for_status()
        uuid = response.json()[self.scope.index]['settings']['index.uuid']
        response = self.search.request('GET', '/_mapping')
        response.raise_for_status()
        meta = response.json()[self.scope.index]['mappings'].get('_meta', {})
        if meta != {'study_owner': self.scope.token, 'study_run': self.scope.run_id} or not isinstance(uuid, str) or not uuid:
            raise RuntimeError('Index owner/identity mismatch')
        return uuid

    def verify_resources(self, resources, allow_missing=False):
        """Check ALL identities first; no partially verified cleanup or guessed names."""
        if not isinstance(resources, dict) or set(resources) - {'topics', 'index'}:
            raise ValueError('Invalid resource ledger')
        topics = resources.get('topics', {})
        if not isinstance(topics, dict) or set(topics) - {self.scope.topic, self.scope.dlq}:
            raise ValueError('Foreign topic in resource ledger')
        index = resources.get('index')
        if index is not None and (not isinstance(index, dict) or index.get('name') != self.scope.index):
            raise ValueError('Foreign index in resource ledger')
        existing = self.topic_names()
        live = []
        for name in (self.scope.topic, self.scope.dlq):
            if name not in existing:
                if not allow_missing:
                    raise RuntimeError('Expected study topic missing')
                continue
            if not topics.get(name) or self.describe(name) != topics[name]:
                raise RuntimeError('Topic UUID missing or changed; cleanup refused')
            live.append(name)
        head = self.search.request('HEAD')
        index_exists = head.status_code != 404
        if index_exists:
            head.raise_for_status()
            if not index or self.index_identity() != index.get('index_uuid'):
                raise RuntimeError('Index UUID missing or changed; cleanup refused')
        elif not allow_missing:
            raise RuntimeError('Study index missing')
        return live, index_exists

    def cleanup(self, resources):
        live, index_exists = self.verify_resources(resources, allow_missing=True)
        # A second check reduces (does not eliminate) concurrent administrator TOCTOU.
        if index_exists:
            if self.index_identity() != resources['index']['index_uuid']:
                raise RuntimeError('Index identity changed before removal')
            self.search.request('DELETE').raise_for_status()
        for name in live:
            if self.describe(name) != resources['topics'][name]:
                raise RuntimeError('Topic identity changed before removal')
            self.admin.delete_topics([name], request_timeout=5)[name].result(timeout=8)
        deadline = time.monotonic() + 15
        while set(live) & self.topic_names():
            if time.monotonic() >= deadline:
                raise RuntimeError('Topic deletion has not become visible; retain recovery ledger')
            time.sleep(.3)
        return {'cleaned': True, 'topics': live, 'index_removed': index_exists,
                'source_orders_deleted': False, 'source_offsets_reset': False,
                'consumer_group_offsets': 'not deleted; broker expiry applies',
                'cache_keys': 'scoped 30-second TTL; no wildcard deletion'}

    def publish(self, topic, key, value):
        from confluent_kafka import Producer
        if topic not in (self.scope.topic, self.scope.dlq):
            raise ValueError('Publish outside study scope')
        if not isinstance(value, bytes) or len(value) > (MAX_MESSAGE * 2) or not isinstance(key, bytes):
            raise ValueError('Invalid/oversized study record')
        self.sent += 1
        if self.sent > 100:
            raise RuntimeError('Study publish budget exhausted')
        if self.producer is None:
            self.producer = Producer({**self.broker.client_config(), 'enable.idempotence': True,
                                      'acks': 'all', 'message.timeout.ms': 5000,
                                      'max.in.flight.requests.per.connection': 1})
        result = []
        def delivery(error, message):
            result.append((error, message))
        self.producer.produce(topic, partition=0, key=key, value=value, on_delivery=delivery)
        remaining = self.producer.flush(6)
        if remaining or not result or result[0][0] is not None:
            raise RuntimeError('Study record not acknowledged; do not commit source')
        message = result[0][1]
        return {'topic': message.topic(), 'partition': message.partition(), 'offset': message.offset()}

    def reader(self, topic=None, offset=None):
        from confluent_kafka import Consumer, TopicPartition
        topic = topic or self.scope.topic
        if topic not in (self.scope.topic, self.scope.dlq):
            raise ValueError('Read outside study scope')
        client = Consumer({**self.broker.client_config(), 'group.id': self.scope.group,
                           'enable.auto.commit': False, 'enable.auto.offset.store': False,
                           'auto.offset.reset': 'error', 'enable.partition.eof': True,
                           'session.timeout.ms': 10000})
        try:
            tp = TopicPartition(topic, 0)
            low, high = client.get_watermark_offsets(tp, timeout=5)
            if offset is None:
                committed = client.committed([tp], timeout=5)[0]
                if committed.error:
                    raise RuntimeError('Cannot read committed position')
                offset = committed.offset
                if offset < 0:
                    if low != 0:
                        raise RuntimeError('No checkpoint and retained prefix is missing; no silent reset')
                    offset = 0
            if not low <= offset <= high:
                raise RuntimeError('Checkpoint outside retained log; no silent offset reset')
            client.assign([TopicPartition(topic, 0, offset)])
            return client
        except Exception:
            client.close()
            raise

    def watermark(self, topic=None):
        from confluent_kafka import TopicPartition
        topic = topic or self.scope.topic
        client = self.reader(topic, 0)  # small fresh topics only; no retention-gap masking
        try:
            tp = TopicPartition(topic, 0)
            low, high = client.get_watermark_offsets(tp, timeout=5)
            committed = client.committed([tp], timeout=5)[0]
            if committed.error:
                raise RuntimeError('Cannot read committed position')
            return {'low': low, 'high': high, 'committed': committed.offset if committed.offset >= 0 else None}
        finally:
            client.close()

    def next_message(self, reader, seconds=10):
        from confluent_kafka import KafkaError
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            message = reader.poll(.4)
            if message is None:
                continue
            error = message.error()
            if error:
                if error.code() == KafkaError._PARTITION_EOF:
                    continue
                raise RuntimeError('Kafka study poll error')
            return message
        raise RuntimeError('Expected study message not observed before deadline')

    def read_dlq(self):
        high = self.watermark(self.scope.dlq)['high']
        if high > 32:
            raise RuntimeError('DLQ inspection budget exceeded')
        reader = self.reader(self.scope.dlq, 0)
        try:
            records = []
            for _ in range(high):
                message = self.next_message(reader)
                raw = message.value()
                if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE * 2:
                    raise RuntimeError('Invalid DLQ size')
                records.append(json.loads(raw))
            return records
        finally:
            reader.close()  # no commit of DLQ; never join/rewind the normal consumer group

    def exact(self, order_id):
        response = self.search.request('GET', '/_doc/' + order_id)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()['_source']

    def audit(self, repo, fixtures):
        rows = []
        for oid, key in fixtures.items():
            first = repo.lookup_key(key)
            cached = self.cache.get(oid)
            stored = self.exact(oid)
            # Search must become visible too; realtime GET alone is not search readiness.
            matches = [o for o in self.search.find(oid)['orders'] if o.get('id') == oid]
            original = repo.lookup_key(key)
            stable = original is not None and original == first and original['id'] == oid
            equal = lambda other: other is not None and canonical(other) == canonical(original)
            okay = stable and equal(stored) and len(matches) == 1 and equal(matches[0]) and (cached is None or equal(cached))
            rows.append({'order_id': oid, 'matched': okay, 'source_stable': stable,
                         'source': original, 'projection': stored, 'cache_present': cached is not None,
                         'search_visible': len(matches) == 1})
        return {'matched': all(r['matched'] for r in rows), 'orders': rows, 'read_only': True,
                'fields': list(ORDER_FIELDS), 'scope': 'only this run; no cache fill or forced ES refresh'}

    def close(self):
        self.search.close(); self.cache.close()
