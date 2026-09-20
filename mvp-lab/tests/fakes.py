"""TEST ONLY. These doubles do not certify any real database, durability or HA."""
import copy
from mvp_app.core import Problem, new_order, envelope, transition


class MemoryRepo:
    def __init__(self):
        self.orders, self.keys, self.outbox = {}, {}, []
        self.down = False
        self.fail_mark = False

    def check(self):
        if self.down:
            raise OSError("test-only SQL unavailable")

    def create(self, data, key):
        self.check()
        if key in self.keys:
            order = self.orders[self.keys[key]]
            if any(order[k] != data[k] for k in data):
                raise Problem(409, "idempotency_key_reused_with_different_payload")
            return copy.deepcopy(order), False
        order = new_order(data, key)
        self.orders[order["id"]] = copy.deepcopy(order)
        self.keys[key] = order["id"]
        self.enqueue(order)
        return order, True

    def enqueue(self, order):
        self.outbox.append({"seq": len(self.outbox) + 1, "event": envelope(order), "sent": False})

    def get(self, order_id):
        self.check()
        return copy.deepcopy(self.orders.get(order_id))

    def update(self, order_id, data):
        self.check()
        if order_id not in self.orders:
            raise Problem(404, "order_not_found")
        order = transition(self.orders[order_id], data)
        self.orders[order_id] = copy.deepcopy(order)
        self.enqueue(order)
        return order

    def lookup_key(self, key):
        self.check()
        return self.get(self.keys[key]) if key in self.keys else None

    def list_orders(self):
        self.check()
        return list(copy.deepcopy(self.orders).values())

    def pending_one(self):
        self.check()
        return next((row for row in self.outbox if not row["sent"]), None)

    def mark_sent(self, seq):
        self.check()
        if self.fail_mark:
            raise OSError("test-only crash after ACK")
        self.outbox[seq - 1]["sent"] = True

    def ping(self):
        self.check()
        return True

    def status(self):
        self.check()
        return {"orders": len(self.orders), "outbox_pending": sum(not o["sent"] for o in self.outbox)}

    def scan(self):
        self.check()
        yield from self.list_orders()


class MemoryCache:
    def __init__(self):
        self.data, self.down, self.clock = {}, False, 0

    def check(self):
        if self.down:
            raise OSError("test-only Redis unavailable")

    def get(self, key):
        self.check()
        value, expires = self.data.get(key, (None, -1))
        return copy.deepcopy(value) if expires > self.clock else None

    def put(self, order):
        self.check()
        self.data[order["id"]] = (copy.deepcopy(order), self.clock + 30)

    def delete(self, key):
        self.check()
        self.data.pop(key, None)

    def status(self):
        self.check()
        return {"ping": True}


class MemorySearch:
    def __init__(self):
        self.docs, self.down = {}, False

    def check(self):
        if self.down:
            raise OSError("test-only ES unavailable")

    def initialize(self):
        self.check()

    def index(self, order):
        self.check()
        existing = self.docs.get(order["id"])
        if existing and existing["version"] > order["version"]:
            return "older_version_ignored"
        self.docs[order["id"]] = copy.deepcopy(order)
        return "indexed"

    def find(self, query):
        self.check()
        return {"orders": [copy.deepcopy(o) for o in self.docs.values() if not query or query in o["item"] or query == o["id"]],
                "source": "elasticsearch"}

    def status(self):
        self.check()
        return {"documents": len(self.docs)}

    def refresh(self):
        self.check()


class MemoryBroker:
    def __init__(self):
        self.events, self.down = [], False

    def publish(self, event):
        if self.down:
            raise OSError("test-only Kafka unavailable")
        self.events.append(copy.deepcopy(event))

    def status(self):
        if self.down:
            raise OSError("test-only Kafka unavailable")
        return {"events": len(self.events)}
