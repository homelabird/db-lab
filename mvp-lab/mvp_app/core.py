"""Business rules, deliberately independent of database drivers for unit tests."""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from contextvars import ContextVar

REQUEST_CONTEXT = ContextVar("request_context", default={})
ORDER_FIELDS = ("id", "item", "quantity", "unit_price", "total", "status", "version", "created_at")

STATES = {"created": {"paid", "cancelled"}, "paid": {"shipped", "cancelled"},
          "shipped": set(), "cancelled": set()}

class Problem(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


def emit(stage: str, **fields: Any) -> None:
    # Only pass identifiers/codes, never connection strings or raw exceptions.
    print(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                      "stage": stage, **REQUEST_CONTEXT.get(), **fields}, ensure_ascii=False), flush=True)


def identifier(value: Any) -> str:
    if not isinstance(value, str):
        raise Problem(400, "invalid_order_id")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise Problem(400, "invalid_order_id") from exc
    return value


def create_input(data: Any) -> dict:
    if not isinstance(data, dict):
        raise Problem(400, "json_object_required")
    item, quantity, unit_price = data.get("item"), data.get("quantity"), data.get("unit_price")
    if not isinstance(item, str) or not 1 <= len(item.strip()) <= 100:
        raise Problem(400, "item_must_be_1_to_100_characters")
    try:
        item.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise Problem(400, "item_must_be_valid_unicode") from exc
    if type(quantity) is not int or not 1 <= quantity <= 1000:
        raise Problem(400, "quantity_must_be_integer_1_to_1000")
    if type(unit_price) is not int or not 0 <= unit_price <= 100_000_000:
        raise Problem(400, "unit_price_must_be_integer_0_to_100000000")
    return {"item": item.strip(), "quantity": quantity, "unit_price": unit_price}


def request_key(value: str | None) -> str:
    value = value or str(uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
        raise Problem(400, "invalid_idempotency_key")
    return value


def new_order(data: dict, key: str) -> dict:
    return {**data, "id": str(uuid.uuid4()), "status": "created", "version": 1,
            "total": data["quantity"] * data["unit_price"],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}


def transition(order: dict, data: Any) -> dict:
    if not isinstance(data, dict) or type(data.get("expected_version")) is not int:
        raise Problem(400, "expected_version_required")
    if data["expected_version"] != order["version"]:
        raise Problem(409, "order_version_conflict")
    status = data.get("status")
    if not isinstance(status, str) or status not in STATES[order["status"]]:
        raise Problem(409, "invalid_status_transition")
    return {**order, "status": status, "version": order["version"] + 1}


def envelope(order: dict) -> dict:
    return {"schema_version": 1, "event_id": str(uuid.uuid4()), "order": dict(order)}


def validate_event(event: Any) -> dict:
    if not isinstance(event, dict) or type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        raise Problem(422, "unsupported_event_schema")
    identifier(event.get("event_id"))
    order = event.get("order")
    if not isinstance(order, dict):
        raise Problem(422, "invalid_event_order")
    identifier(order.get("id")); create_input(order)
    if type(order.get("version")) is not int or order["version"] < 1:
        raise Problem(422, "invalid_event_version")
    if not isinstance(order.get("status"), str) or order["status"] not in STATES or type(order.get("total")) is not int:
        raise Problem(422, "invalid_event_fields")
    if order["total"] != order["quantity"] * order["unit_price"]:
        raise Problem(422, "invalid_event_total")
    if not isinstance(order.get("created_at"), str):
        raise Problem(422, "invalid_event_timestamp")
    try:
        datetime.fromisoformat(order["created_at"])
    except ValueError as exc:
        raise Problem(422, "invalid_event_timestamp") from exc
    return order


class Storefront:
    def __init__(self, repo, cache, search):
        self.repo, self.cache, self.search = repo, cache, search

    def invalidate(self, order_id: str) -> None:
        try:
            self.cache.delete(order_id)
        except Exception as exc:
            emit("cache_invalidate_failed", order_id=order_id, error=type(exc).__name__)

    def create(self, data: Any, key: str | None = None) -> tuple[dict, bool]:
        order, created = self.repo.create(create_input(data), request_key(key))
        emit("order_saved", order_id=order["id"], version=order["version"], created=created)
        return order, created

    def update(self, order_id: str, data: Any) -> dict:
        order = self.repo.update(identifier(order_id), data)
        self.invalidate(order_id)
        emit("order_updated", order_id=order_id, version=order["version"])
        return order

    def detail(self, order_id: str, fresh: bool = False) -> dict:
        identifier(order_id)
        cache_state = "bypassed" if fresh else "miss"
        if not fresh:
            try:
                cached = self.cache.get(order_id)
                if cached is not None:
                    # Cache corruption must not break the authoritative read path.
                    validate_event({"schema_version": 1, "event_id": order_id, "order": cached})
                    if cached["id"] != order_id:
                        raise ValueError("wrong cache key")
                    return {"order": cached, "source": "redis", "cache": "hit"}
            except Exception as exc:
                cache_state = "unavailable_or_invalid"
                emit("cache_read_failed", order_id=order_id, error=type(exc).__name__)
        order = self.repo.get(order_id)
        if order is None:
            raise Problem(404, "order_not_found")
        try:
            self.cache.put(order)
        except Exception as exc:
            cache_state = "unavailable"
            emit("cache_fill_failed", order_id=order_id, error=type(exc).__name__)
        return {"order": order, "source": "mariadb", "cache": cache_state}


def relay_once(repo, broker) -> bool:
    """Exactly one relay process. Crash after send/before mark can duplicate an event."""
    row = repo.pending_one()
    if row is None:
        return False
    event = row["event"]
    broker.publish(event)  # Must wait for broker delivery ACK, not just local enqueue.
    repo.mark_sent(row["seq"])
    emit("outbox_published", order_id=event["order"]["id"], event_id=event["event_id"])
    return True


def project_one(event, search, cache, commit) -> None:
    order = validate_event(event)
    outcome = search.index(order)  # Deterministic ID + external version fence.
    try:
        cache.delete(order["id"])
    except Exception as exc:
        # Cache is optional: TTL provides a bounded best-effort repair window.
        emit("projection_cache_unavailable", order_id=order["id"], error=type(exc).__name__)
    commit()  # NEVER advance past a failed ES write.
    emit("search_projected", order_id=order["id"], event_id=event["event_id"],
         version=order["version"], outcome=outcome)
