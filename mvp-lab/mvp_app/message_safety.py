"""Small, transport-independent rules for the opt-in message recovery laboratory.

The normal worker remains strict. This module never makes an infrastructure error
eligible for quarantine just because it has been retried many times.
"""
from __future__ import annotations
import base64
from dataclasses import dataclass
import hashlib
import json
import re
import uuid
from .core import Problem, identifier, validate_event
from .observability import safe_error

MAX_MESSAGE = 32 * 1024
RUN_RE = r"msg-[a-f0-9]{24}"
MAPPING_ERRORS = {"mapper_parsing_exception", "strict_dynamic_mapping_exception"}
SCENARIOS = {
    "poison-schema": "정상 → 미지원 schema → 정상: 정지·DLQ 격리·SQL 원본 재처리",
    "mapping-reject": "업무 검증은 통과하지만 ES strict mapping에 거절되는 이벤트",
    "projection-commit-gap": "ES 반영 뒤 offset 승인 전 의도적 예외, 같은 offset 재수신",
    "dlq-commit-gap": "DLQ ACK 뒤 원본 offset 승인 전 예외, 중복 격리와 1회 복구",
    "replay-ordering": "SQL의 실제 3개 버전을 중복·역순 전달하고 최신 내용 대조",
    "version-collision": "같은 ID·같은 버전·다른 내용: 기존 문서 덮어쓰기 차단",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Scope:
    project: str
    run_id: str
    token: str

    def __post_init__(self):
        if not re.fullmatch(r"db-lab-mvp(?:-[a-z0-9-]{1,24})?", self.project):
            raise ValueError("Invalid message lab project")
        if not re.fullmatch(RUN_RE, self.run_id) or not re.fullmatch(r"[a-f0-9]{32}", self.token):
            raise ValueError("Invalid message lab run or ownership token")

    @property
    def suffix(self):
        return digest(self.project.encode())[:10] + "-" + self.run_id[4:]

    @property
    def topic(self): return "mvp.study." + self.suffix + ".events"
    @property
    def dlq(self): return "mvp.study." + self.suffix + ".dlq"
    @property
    def group(self): return "mvp.study." + self.suffix + ".consumer"
    @property
    def index(self): return "mvp-study-" + self.suffix
    @property
    def cache_prefix(self): return "mvp:study:" + self.suffix + ":"

    def public(self):
        return {"project": self.project, "run_id": self.run_id, "topic": self.topic,
                "dlq": self.dlq, "group": self.group, "index": self.index,
                "cache_prefix": self.cache_prefix}


class InjectedBoundary(Exception):
    """Deliberate application exception, not an OS process kill or a real outage."""


class PoisonBlocked(Exception):
    def __init__(self, reason, position):
        self.reason, self.position = reason, position
        super().__init__(reason)


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MESSAGE:
        raise Problem(422, "invalid_event_size")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Problem(422, "duplicate_json_key")
            result[key] = value
        return result
    def invalid_constant(_):
        raise Problem(422, "nonfinite_json_number")
    try:
        event = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Problem(422, "invalid_event_json") from exc
    validate_event(event)
    return event


def permanent_reason(exc, stage):
    if stage == "decode" and isinstance(exc, Problem) and exc.status in (400, 422):
        return exc.code
    if stage == "project":
        if isinstance(exc, Problem) and exc.code == "event_version_payload_conflict":
            return exc.code
        info = safe_error(exc)
        if info.get("http_status") == 400 and info.get("error_type") in MAPPING_ERRORS:
            return info["error_type"]
    return None  # auth/timeout/429/5xx/unknown/commit errors must never be discarded


def quarantine_record(scope, topic_id, position, key, raw, reason):
    if (not topic_id or position.get("topic") != scope.topic or position.get("partition") != 0
            or type(position.get("offset")) is not int or position["offset"] < 0):
        raise ValueError("Foreign or invalid source position")
    if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE:
        raise ValueError("Cannot preserve oversized message; leave source offset uncommitted")
    if not isinstance(key, bytes) or len(key) > 128:
        raise ValueError("Missing or oversized key; no automatic repair")
    source = {**position, "topic_id": topic_id}
    identity = {"scope": scope.suffix, "source": source, "raw_sha256": digest(raw), "key_sha256": digest(key)}
    return {"schema": 1, "run_id": scope.run_id, "owner": scope.token,
            "dlq_id": digest(canonical(identity)), "source": source, "reason": reason,
            "raw_sha256": digest(raw), "raw_b64": base64.b64encode(raw).decode(),
            "key_b64": base64.b64encode(key).decode()}


def checked_quarantine(record, scope, topic_id):
    if (not isinstance(record, dict) or record.get("schema") != 1
            or record.get("owner") != scope.token or record.get("run_id") != scope.run_id):
        raise ValueError("Foreign quarantine envelope")
    try:
        raw = base64.b64decode(record["raw_b64"], validate=True)
        key = base64.b64decode(record["key_b64"], validate=True)
        source = record["source"]
        position = {k: source[k] for k in ("topic", "partition", "offset")}
        expected = quarantine_record(scope, topic_id, position, key, raw, record["reason"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Malformed quarantine envelope") from exc
    if canonical(expected) != canonical(record):
        raise ValueError("Quarantine identity/hash mismatch")
    return key, raw


def repair_event(record, scope, topic_id, allowed, repo):
    """Never edit/replay untrusted raw bytes. Rebuild this fixture from current SQL."""
    key, _ = checked_quarantine(record, scope, topic_id)
    order_id = identifier(key.decode("ascii"))
    expected_key = allowed.get(order_id)
    if not expected_key or not expected_key.startswith(scope.run_id + "-"):
        raise ValueError("Repair outside this run's fixture allowlist")
    original = repo.lookup_key(expected_key)  # readonly; never POST to hide missing data
    if not original or original.get("id") != order_id:
        raise ValueError("Authoritative fixture missing; repair refused")
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, record["dlq_id"] + digest(canonical(original))))
    event = {"schema_version": 1, "event_id": event_id, "order": original,
             "repair": {"dlq_id": record["dlq_id"], "basis": "current_sql", "run_id": scope.run_id}}
    validate_event(event)
    return event


def process_record(*, scope, topic_id, position, key, raw, search, cache, commit,
                   quarantine, policy="strict", checkpoint=lambda name: None):
    """Publish quarantine ACK, THEN commit source; or write ES, THEN commit source.

    There is deliberately no Kafka transaction spanning these calls. Boundary
    failures may duplicate DLQ records; their deterministic dlq_id supports repair.
    """
    if policy not in {"strict", "quarantine"}:
        raise ValueError("Unknown message error policy")
    stage = "decode"
    try:
        event = decode(raw)
        if key != event["order"]["id"].encode():
            raise Problem(422, "event_key_mismatch")
        stage = "project"
        outcome = search.index(event["order"])
    except Exception as exc:
        reason = permanent_reason(exc, stage)
        if reason is None:
            raise
        if policy == "strict":
            raise PoisonBlocked(reason, position) from exc
        record = quarantine_record(scope, topic_id, position, key, raw, reason)
        receipt = quarantine(record)  # return ONLY on durable Kafka ACK
        checkpoint("after_dlq_ack")
        commit()
        return {"outcome": "quarantined", "reason": reason, "dlq_id": record["dlq_id"], "receipt": receipt}
    checkpoint("after_projection")
    # Cache is optional and scoped by caller. No real MVP cache deletion here.
    cache_error = None
    try:
        cache.delete(event["order"]["id"])
    except Exception as exc:
        cache_error = safe_error(exc)
    commit()  # exception here is never caught as a poison record
    return {"outcome": outcome, "event_id": event["event_id"], "order_id": event["order"]["id"],
            "version": event["order"]["version"], "cache_error": cache_error}
