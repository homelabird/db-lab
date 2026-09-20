"""Small real MariaDB / Redis / Kafka / Elasticsearch adapters. No fake runtime mode."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import re
from typing import Callable
from .core import Problem, emit, envelope, new_order, transition, ORDER_FIELDS


@dataclass(frozen=True)
class Settings:
    sql_host: str = "mariadb"
    sql_port: int = 3306
    sql_database: str = "mvp"
    sql_user: str = "mvp"
    sql_password: str = ""
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""
    cache_ttl: int = 30
    kafka_bootstrap: str = "kafka:9092"
    kafka_topic: str = "mvp.orders.v1"
    kafka_group: str = "mvp.search.v1"
    es_url: str = "http://elasticsearch:9200"
    es_index: str = "mvp-orders-v1"

    @classmethod
    def load(cls):
        raw = {name: os.environ.get(name.upper(), field.default)
               for name, field in cls.__dataclass_fields__.items()}
        for name in ("sql_port", "redis_port", "cache_ttl"):
            raw[name] = int(raw[name])
        if not 1 <= raw["cache_ttl"] <= 300:
            raise ValueError("CACHE_TTL must be 1..300 seconds")
        if not re.fullmatch(r"mvp-[a-z0-9-]{1,80}", raw["es_index"]):
            raise ValueError("ES_INDEX must be an mvp- prefixed index name")
        if not raw["es_url"].startswith(("http://", "https://")):
            raise ValueError("Invalid ES_URL")
        return cls(**raw)


PUBLIC_FIELDS = ORDER_FIELDS
COLUMNS = ", ".join(PUBLIC_FIELDS)


def public(row):
    return {key: row[key] for key in PUBLIC_FIELDS} if row else None


class SQL:
    def __init__(self, settings: Settings, connect: Callable | None = None):
        self.s, self._connect = settings, connect

    def connect(self):
        if self._connect is not None:
            return self._connect()
        import pymysql
        return pymysql.connect(host=self.s.sql_host, port=self.s.sql_port,
                               user=self.s.sql_user, password=self.s.sql_password,
                               database=self.s.sql_database, charset="utf8mb4",
                               connect_timeout=3, read_timeout=3, write_timeout=3,
                               cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                               init_command="SET SESSION innodb_lock_wait_timeout=2")

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.transaction() as conn, conn.cursor() as cur:
            # No DROP/ALTER; initialization is idempotent, not a schema migration engine.
            cur.execute('''CREATE TABLE IF NOT EXISTS orders (
                id CHAR(36) PRIMARY KEY,
                idempotency_key VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
                item VARCHAR(100) NOT NULL, quantity INT NOT NULL,
                unit_price BIGINT NOT NULL, total BIGINT NOT NULL,
                status VARCHAR(20) NOT NULL, version BIGINT NOT NULL,
                created_at VARCHAR(40) NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            cur.execute('''CREATE TABLE IF NOT EXISTS outbox (
                seq BIGINT AUTO_INCREMENT PRIMARY KEY,
                event_id CHAR(36) NOT NULL UNIQUE,
                payload LONGTEXT NOT NULL,
                created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                sent_at TIMESTAMP(3) NULL,
                INDEX pending_idx(sent_at, seq)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')

    @staticmethod
    def _enqueue(cur, order):
        event = envelope(order)
        cur.execute("INSERT INTO outbox (event_id, payload) VALUES (%s, %s)",
                    (event["event_id"], json.dumps(event, ensure_ascii=False)))

    @staticmethod
    def _existing(row, data):
        if any(row[k] != data[k] for k in ("item", "quantity", "unit_price")):
            raise Problem(409, "idempotency_key_reused_with_different_payload")
        return public(row), False

    def create(self, data, key):
        try:
            with self.transaction() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM orders WHERE idempotency_key=%s", (key,))
                existing = cur.fetchone()
                if existing:
                    return self._existing(existing, data)
                order = new_order(data, key)
                cur.execute(f"INSERT INTO orders ({COLUMNS}, idempotency_key) VALUES ({', '.join(['%s'] * 9)})",
                            tuple(order[k] for k in PUBLIC_FIELDS) + (key,))
                self._enqueue(cur, order)
            return order, True
        except Exception as exc:
            # A concurrent request with the same key may win after our initial read.
            if getattr(exc, "args", ())[:1] != (1062,):
                raise
            with self.transaction() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM orders WHERE idempotency_key=%s", (key,))
                row = cur.fetchone()
                if row:
                    return self._existing(row, data)
            raise

    def get(self, order_id):
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {COLUMNS} FROM orders WHERE id=%s", (order_id,))
            return cur.fetchone()

    def lookup_key(self, key):
        """Resolve an ambiguous client write without retrying/inserting a new order."""
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {COLUMNS} FROM orders WHERE idempotency_key=%s", (key,))
            return cur.fetchone()

    def list_orders(self):
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {COLUMNS} FROM orders ORDER BY created_at DESC, id DESC LIMIT 100")
            return cur.fetchall()

    def update(self, order_id, data):
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {COLUMNS} FROM orders WHERE id=%s FOR UPDATE", (order_id,))
            old = cur.fetchone()
            if old is None:
                raise Problem(404, "order_not_found")
            order = transition(old, data)
            cur.execute("UPDATE orders SET status=%s, version=%s WHERE id=%s",
                        (order["status"], order["version"], order_id))
            self._enqueue(cur, order)
        return order

    def pending_one(self):
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute("SELECT seq, payload FROM outbox WHERE sent_at IS NULL ORDER BY seq LIMIT 1")
            row = cur.fetchone()
            return {"seq": row["seq"], "event": json.loads(row["payload"])} if row else None

    def mark_sent(self, seq):
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute("UPDATE outbox SET sent_at=CURRENT_TIMESTAMP(3) WHERE seq=%s AND sent_at IS NULL", (seq,))

    def status(self):
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS orders FROM orders")
            data = cur.fetchone()
            cur.execute('''SELECT COUNT(*) AS outbox_pending,
                COALESCE(TIMESTAMPDIFF(SECOND, MIN(created_at), CURRENT_TIMESTAMP),0) AS oldest_pending_seconds
                FROM outbox WHERE sent_at IS NULL''')
            data.update(cur.fetchone())
            return data

    def ping(self):
        with self.transaction() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return cur.fetchone()["ok"] == 1

    def scan(self):
        # Bounded pages, not a snapshot/backup: concurrent new writes use the event path.
        last = ""
        while True:
            with self.transaction() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT {COLUMNS} FROM orders WHERE id>%s ORDER BY id LIMIT 200", (last,))
                rows = cur.fetchall()
            if not rows:
                return
            yield from rows
            last = rows[-1]["id"]


class Cache:
    def __init__(self, settings: Settings, client=None):
        self.s, self._client = settings, client

    @property
    def client(self):
        if self._client is None:
            import redis
            self._client = redis.Redis(host=self.s.redis_host, port=self.s.redis_port,
                                       password=self.s.redis_password, decode_responses=True,
                                       socket_connect_timeout=0.5, socket_timeout=0.5,
                                       retry_on_timeout=False)
        return self._client

    def get(self, order_id):
        raw = self.client.get("mvp:order:" + order_id)
        return json.loads(raw) if raw is not None else None

    def put(self, order):
        self.client.set("mvp:order:" + order["id"], json.dumps(order, ensure_ascii=False), ex=self.s.cache_ttl)

    def delete(self, order_id):
        self.client.delete("mvp:order:" + order_id)

    def close(self):
        if self._client is not None:
            self._client.close()

    def status(self):
        return {"ping": bool(self.client.ping()), "host": self.s.redis_host, "ttl_seconds": self.s.cache_ttl}


class Search:
    def __init__(self, settings: Settings, session=None):
        self.s, self._session = settings, session

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.trust_env = False
        return self._session

    def request(self, method, suffix="", **kwargs):
        # A redirect is a changed dependency target, not a successful DB operation.
        response = self.session.request(method, self.s.es_url.rstrip("/") + "/" + self.s.es_index + suffix,
                                        timeout=(2, 3), allow_redirects=False, **kwargs)
        if 300 <= response.status_code < 400:
            raise Problem(503, "elasticsearch_redirect_refused")
        return response

    def initialize(self):
        head = self.request("HEAD")
        if head.status_code == 200:
            return
        if head.status_code != 404:
            head.raise_for_status()
        fields = {"id": {"type": "keyword"}, "item": {"type": "text"},
                  "quantity": {"type": "integer"}, "unit_price": {"type": "long"},
                  "total": {"type": "long"}, "status": {"type": "keyword"},
                  "version": {"type": "long"}, "created_at": {"type": "date"}}
        result = self.request("PUT", json={"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                                            "mappings": {"dynamic": "strict", "properties": fields}})
        if result.status_code == 400 and result.json().get("error", {}).get("type") == "resource_already_exists_exception":
            return
        result.raise_for_status()

    def index(self, order):
        self.initialize()
        response = self.request("PUT", "/_doc/" + order["id"], json=order,
                                params={"version": order["version"], "version_type": "external"})
        if response.status_code == 409 and response.json().get("error", {}).get("type") == "version_conflict_engine_exception":
            # external_gte accepts *different* contents at the same version. Never
            # call that a duplicate. Read the realtime document after a fenced PUT.
            current = self.request("GET", "/_doc/" + order["id"])
            current.raise_for_status()
            document = current.json()
            stored, version = document.get("_source"), document.get("_version")
            if (not isinstance(stored, dict) or type(version) is not int
                    or stored.get("version") != version or stored.get("id") != order["id"]):
                raise Problem(409, "projection_version_metadata_mismatch")
            if version > order["version"]:
                return "older_version_ignored"
            if version == order["version"]:
                canonical = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
                if canonical(stored) == canonical(order):
                    return "duplicate_ignored"
                raise Problem(409, "event_version_payload_conflict")
            # Concurrent deletion/foreign replacement: do not silently acknowledge.
            raise Problem(409, "projection_version_changed_during_check")
        response.raise_for_status()
        return "indexed"

    def find(self, query):
        clause = {"match_all": {}} if not query else {"bool": {"should": [
            {"match": {"item": query}}, {"term": {"id": query}}], "minimum_should_match": 1}}
        response = self.request("POST", "/_search", json={"query": clause, "size": 50,
                                                       "sort": [{"created_at": "desc"}]})
        response.raise_for_status()
        data = response.json()
        shards = data.get("_shards", {})
        if (data.get("timed_out") is not False or not isinstance(shards, dict)
                or type(shards.get("failed")) is not int or shards["failed"] != 0):
            raise Problem(503, "elasticsearch_search_incomplete")
        return {"orders": [hit["_source"] for hit in data["hits"]["hits"]],
                "total": data["hits"]["total"], "source": "elasticsearch", "index": self.s.es_index}

    def status(self):
        response = self.request("GET", "/_count")
        response.raise_for_status()
        return {"index": self.s.es_index, "documents": response.json()["count"]}

    def refresh(self):
        self.request("POST", "/_refresh").raise_for_status()

    def close(self):
        if self._session is not None:
            self._session.close()


class Broker:
    def __init__(self, settings: Settings):
        self.s, self._producer = settings, None

    def client_config(self):
        return {"bootstrap.servers": self.s.kafka_bootstrap, "socket.timeout.ms": 5000,
                "log_level": 0}

    def initialize(self):
        from confluent_kafka import KafkaError, KafkaException
        from confluent_kafka.admin import AdminClient, NewTopic
        admin = AdminClient(self.client_config())
        future = admin.create_topics([NewTopic(self.s.kafka_topic, num_partitions=1, replication_factor=1,
                                              config={"retention.ms": "86400000"})], request_timeout=5)
        try:
            future[self.s.kafka_topic].result(timeout=8)
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise
        metadata = admin.list_topics(timeout=5)
        topic = metadata.topics.get(self.s.kafka_topic)
        if not topic or topic.error or len(topic.partitions) != 1:
            raise RuntimeError("This MVP requires an existing one-partition topic")

    def publish(self, event):
        from confluent_kafka import Producer
        if self._producer is None:
            self._producer = Producer({**self.client_config(), "enable.idempotence": True,
                                       "acks": "all", "message.timeout.ms": 5000,
                                       "max.in.flight.requests.per.connection": 1})
        delivery = []
        self._producer.produce(self.s.kafka_topic, key=event["order"]["id"],
                               value=json.dumps(event, ensure_ascii=False).encode(),
                               on_delivery=lambda error, message: delivery.append(error))
        left = self._producer.flush(6)
        if left or not delivery or delivery[0] is not None:
            raise RuntimeError("Kafka delivery not acknowledged; outbox row remains pending")

    def consumer(self):
        from confluent_kafka import Consumer
        return Consumer({**self.client_config(), "group.id": self.s.kafka_group,
                         "enable.auto.commit": False, "enable.auto.offset.store": False,
                         "auto.offset.reset": "earliest", "max.poll.interval.ms": 60000,
                         "session.timeout.ms": 10000})

    def status(self):
        from confluent_kafka import TopicPartition
        from confluent_kafka.admin import AdminClient
        meta = AdminClient(self.client_config()).list_topics(timeout=3)
        topic = meta.topics.get(self.s.kafka_topic)
        if not topic or topic.error:
            raise RuntimeError("Topic unavailable")
        client = self.consumer()  # No subscribe: this diagnostic client never joins the group.
        try:
            partitions = []
            for number in topic.partitions:
                tp = TopicPartition(self.s.kafka_topic, number)
                low, high = client.get_watermark_offsets(tp, timeout=3)
                positions = client.committed([tp], timeout=3)
                if len(positions) != 1 or getattr(positions[0], "error", None) is not None:
                    raise RuntimeError("Kafka committed position lookup failed; lag is unknown")
                committed = positions[0].offset
                if type(committed) is not int or not (type(low) is int and type(high) is int and 0 <= low <= high):
                    raise RuntimeError("Kafka returned invalid watermark/checkpoint metadata")
                partitions.append({"partition": number, "low": low, "high": high,
                                   "committed": committed if committed >= 0 else None,
                                   "lag": max(0, high - max(low, committed)),
                                   "offset_state": ("uninitialized" if committed < 0 else
                                       "below_retention" if committed < low else
                                       "ahead_of_log" if committed > high else "in_range"),
                                   "retention_gap_offsets": max(0, low - committed) if committed >= 0 else None})
            return {"topic": self.s.kafka_topic, "group": self.s.kafka_group,
                    "lag": sum(p["lag"] for p in partitions), "partitions": partitions,
                    "offset_warning": any(p["offset_state"] != "in_range" for p in partitions),
                    "warning": "Offsets are not an acknowledged-order loss count; reconcile with SQL."}
        finally:
            client.close()


def dependencies(settings=None):
    settings = settings or Settings.load()
    return SQL(settings), Cache(settings), Search(settings), Broker(settings)
