"""Repeatable small DB drills: real HTTP, bounded load, exact-target faults and read-only audits.
Tests inject explicit doubles; the CLI has no fake-runtime or remote-host mode.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import signal
import threading
import time
from urllib.parse import urlsplit, quote
import uuid
from mvp_app.core import ORDER_FIELDS
from mvp_app.inspection import differences
from .sim_engine import DockerLab
from .measurement import strict_json, capture_context, comparable_context

SCENARIOS = {
    "db-network-delay": ("netem-delay", "mariadb", "DB eth0 송신 지연 180±20ms; helper 자체 만료·전체 필드 복구 대조"),
    "db-network-loss": ("netem-loss", "mariadb", "DB eth0 송신 패킷 10% 확률 손실; drop 통계와 복구 대조"),
    "baseline": (None, None, "정상 혼합 부하와 정합성 기준선"),
    "kafka-outage": ("stop", "kafka", "원본 저장·outbox 누적·검색 지연·복구"),
    "es-outage": ("stop", "elasticsearch", "검색 실패와 consumer 처리 지연"),
    "redis-outage": ("stop", "redis", "캐시 실패 시 SQL 우회와 응답 지연"),
    "db-freeze": ("pause", "mariadb", "프로세스는 존재하지만 응답하지 않는 DB"),
    "worker-freeze": ("pause", "worker", "DB 연결 정상과 이벤트 처리 정지를 구분"),
    "redis-recreate": ("recreate", "redis", "같은 이미지·볼륨으로 컨테이너만 교체"),
    "redis-switch": ("switch", "redis", "별도 캐시로 연결 변경·원래 캐시 복귀"),
    "row-lock": ("row-lock", "api", "실행 전용 주문 행의 잠금 대기와 제한시간 초과"),
    "duplicate-retry": (None, None, "동일 키 HTTP 재요청의 중복 주문 방지"),
    "version-race": (None, None, "같은 버전 주문을 동시에 변경: 하나만 승인되는지 검사"),
}
WORKLOADS = {
    "mixed": (["create", "read", "update", "retry", "race", "search"], [25, 30, 15, 10, 10, 10]),
    "read-heavy": (["create", "read", "update", "retry", "race", "search"], [10, 55, 5, 10, 5, 15]),
    "write-heavy": (["create", "read", "update", "retry", "race", "search"], [35, 10, 25, 10, 15, 5]),
    "hot-key": (["create", "read", "update", "retry", "race", "search"], [10, 45, 20, 10, 10, 5]),
}


@dataclass(frozen=True)
class Plan:
    scenario: str = "baseline"
    seed: int = 42
    seconds: int = 40
    rate: float = 2
    workers: int = 4
    fault_at: int = 8
    fault_for: int = 10
    recovery_timeout: int = 90
    workload: str = "mixed"

    def validate(self):
        if self.scenario not in SCENARIOS or self.workload not in WORKLOADS:
            raise ValueError("Unknown scenario/workload")
        for name in ("seed", "seconds", "workers", "fault_at", "fault_for", "recovery_timeout"):
            if type(getattr(self, name)) is not int:
                raise ValueError(name + " must be an integer")
        if not (0 <= self.seed <= 2**32 - 1 and 15 <= self.seconds <= 180
                and 1 <= self.workers <= 8 and 1 <= self.fault_for <= 30
                and 1 <= self.fault_at < self.seconds and 5 <= self.recovery_timeout <= 300):
            raise ValueError("Bounded study limits exceeded; inspect simulate run --help")
        if type(self.rate) not in (float, int) or not math.isfinite(self.rate) or not 0.2 <= self.rate <= 10:
            raise ValueError("rate must be finite and 0.2..10 admitted workflows/second")
        if self.scenario == "version-race" and self.workers < 2:
            raise ValueError("version-race requires at least two concurrent HTTP slots")
        if self.seconds * self.rate > 400:
            raise ValueError("At most 400 planned workflows per run; this is not a stress platform")
        if SCENARIOS[self.scenario][0] and self.fault_at + self.fault_for + 5 > self.seconds:
            raise ValueError("Leave at least five planned post-fault seconds")
        return self

    def operations(self):
        self.validate()
        rng = random.Random(self.seed)
        names, weights = WORKLOADS[self.workload]
        result = []
        for i in range(int(self.seconds * self.rate)):
            kind = rng.choices(names, weights=weights)[0]
            if self.scenario == "duplicate-retry":
                kind = "retry"
            elif self.scenario == "version-race":
                kind = "race"
            elif self.scenario == "row-lock":
                kind = "lock_update" if i % 2 == 0 else "read"
            target = 0 if self.workload == "hot-key" or self.scenario == "row-lock" else rng.randrange(6)
            result.append({"number": i, "at_seconds": round(i / self.rate, 6), "kind": kind,
                           "target": target, "quantity": rng.randint(1, 5), "unit_price": rng.randint(10, 500)})
        return result

    def document(self):
        return {"schema": 1, **asdict(self), "description": SCENARIOS[self.scenario][2],
                "rate_unit": "admitted workflows/s; one workflow can make multiple HTTP requests",
                "max_http_concurrency": self.workers,
                "overload_policy": "skip admission slots; never queue an unbounded catch-up burst",
                "operations": self.operations()}


@dataclass
class Response:
    status: int
    payload: dict
    milliseconds: float
    request_id: str | None = None
    client_queue_ms: float | None = None
    transport_ms: float | None = None
    transport_attempted: bool | None = None


class HTTP:
    def __init__(self, base, run_id, workers=4):
        url = urlsplit(base)
        if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError("Only a direct loopback HTTP study API is allowed")
        self.host, self.port, self.run_id = url.hostname, url.port or 80, run_id
        self.slots = threading.BoundedSemaphore(workers)

    def call(self, method, path, body=None, key=None, timeout=5):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only an API-relative path is allowed")
        start = time.monotonic()
        queue_seconds = None
        attempted = False
        def measured(status, payload, request_id=None):
            elapsed = time.monotonic() - start
            queued = elapsed if queue_seconds is None else queue_seconds
            return Response(status, payload, elapsed * 1000, request_id,
                            queued * 1000, max(0, elapsed - queued) * 1000 if attempted else None, attempted)
        if not self.slots.acquire(timeout=max(0.01, timeout)):
            return measured(0, {"error": "client_concurrency_timeout"})
        queue_seconds = time.monotonic() - start
        conn = None
        try:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                return measured(0, {"error": "client_concurrency_timeout"})
            conn = HTTPConnection(self.host, self.port, timeout=remaining)
            headers = {"Content-Type": "application/json", "X-Study-Run": self.run_id}
            if key:
                headers["Idempotency-Key"] = key
            attempted = True
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = conn.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Oversize response")
            data = strict_json(raw)
            if not isinstance(data, dict):
                raise ValueError("Not an object")
            rid = response.getheader("X-Request-ID", "")
            rid = rid if re.fullmatch(r"[a-zA-Z0-9-]{1,80}", rid) else None
            return measured(response.status, data, rid)
        except (OSError, ValueError, HTTPException) as exc:
            return measured(0, {"error": type(exc).__name__})
        finally:
            if conn is not None:
                conn.close()
            self.slots.release()


class Journal:
    def __init__(self, directory):
        self.path = Path(directory)
        self.path.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.zero = time.monotonic()
        self.lock = threading.RLock()
        self.files = {}
        for name in ("timeline", "requests", "audits"):
            fd = os.open(self.path / (name + ".jsonl"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.files[name] = os.fdopen(fd, "w")
        self.rows = {"timeline": [], "requests": []}

    def write(self, name, **fields):
        record = {"utc": datetime.now(timezone.utc).isoformat(),
                  "elapsed_seconds": round(time.monotonic() - self.zero, 6), **fields}
        with self.lock:
            self.files[name].write(json.dumps(record, ensure_ascii=False) + "\n")
            self.files[name].flush()
            if name in self.rows:
                self.rows[name].append(record)
        return record

    def json(self, name, value):
        fd = os.open(self.path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")

    def close(self):
        for stream in self.files.values():
            stream.close()


def percentiles(values):
    """Nearest-rank statistics, no percentile claim when sample count is zero."""
    values = sorted(values)
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    return {"count": len(values), **{f"p{p}_ms": round(values[max(0, math.ceil(p / 100 * len(values)) - 1)], 3)
                                    for p in (50, 95, 99)}}


def request_summary(rows):
    groups = {}
    for row in rows:
        key = (row["scope"], row["phase"], row["operation"], row["outcome"])
        groups.setdefault(key, []).append(row["latency_ms"])
    return [{"scope": k[0], "phase": k[1], "operation": k[2], "outcome": k[3], **percentiles(v)}
            for k, v in sorted(groups.items())]


def inspect_intent(intent, lookup, observed):
    """A timeout is ambiguous, not proof that the original write failed or data was lost."""
    ack = intent.get("acknowledged", {})
    if lookup.status != 200:
        return {"state": "unresolved", "consistent": False}
    sql = lookup.payload.get("order")
    if sql is None:
        return {"state": "acknowledged_missing" if ack else "unacknowledged_absent",
                "consistent": not bool(ack)}
    errors = []
    if any(sql.get(k) != intent["payload"][k] for k in ("item", "quantity", "unit_price")):
        errors.append("source_payload_mismatch")
    if len(intent.get("ids", [])) > 1 or (intent.get("ids") and sql.get("id") not in intent["ids"]):
        errors.append("idempotency_identity_mismatch")
    if ack:
        latest = ack[max(ack, key=lambda v: int(v))]
        if type(sql.get("version")) is not int or sql["version"] < latest["version"]:
            errors.append("acknowledged_version_missing")
        elif sql["version"] == latest["version"] and differences(latest, sql):
            errors.append("acknowledged_content_mismatch")
        elif sql["version"] > latest["version"]:
            if [sql["version"], sql["status"]] not in intent.get("attempted_updates", []):
                errors.append("unexpected_source_version")
    if observed is None or observed.status != 200:
        return {"state": "unresolved", "consistent": False, "errors": errors}
    data = observed.payload
    # Resolve-by-key and the per-source inspection must refer to the same original.
    if differences(sql, data.get("mariadb", {}).get("order")):
        errors.append("source_changed_between_observations")
    if not data.get("sql_stable_during_observation") or not data.get("authoritative_valid"):
        errors.append("unstable_or_invalid_source")
    search = data.get("elasticsearch", {}).get("comparison", "unknown")
    cache = data.get("redis", {}).get("comparison", "unknown")
    if search != "match":
        errors.append("search_" + search)
    if cache not in {"match", "missing"}:
        errors.append("cache_" + cache)
    return {"state": "acknowledged_present" if ack else "present_without_ack",
            "consistent": not errors, "order_id": sql.get("id"), "version": sql.get("version"),
            "cache": cache, "search": search, "errors": errors}


class Runner:
    def __init__(self, plan, http, lab, journal, run_id, project, evidence_kind="real-http-and-docker-actions", context_provider=None):
        self.plan, self.http, self.lab, self.journal = plan.validate(), http, lab, journal
        self.run_id, self.project, self.evidence_kind = run_id, project, evidence_kind
        self.lock = threading.RLock()
        self.workflow_context = threading.local()
        self.workload_zero = None
        self.context_provider = context_provider
        self.comparison_context = {"schema": 1, "available": False, "stable": False}
        self.intents = {}
        self.phase = "setup"
        self.contract_errors = []
        self.races = []
        self.cancel = threading.Event()
        self.observer_stop = threading.Event()
        self.fault_started = False
        self.fault_completed = False
        self.restored_at = None
        self.fault_error = None
        self.admitted = self.skipped = 0
        self.workload_requests = 0

    def call(self, method, path, body=None, key=None, operation="read", scope="workload", timeout=5):
        phase = self.phase
        started = time.monotonic()
        if scope == "workload":
            with self.lock:
                self.workload_requests += 1
                if self.workload_requests > 2400:
                    raise RuntimeError("Bounded workload HTTP request budget exhausted")
        response = self.http.call(method, path, body, key, timeout)
        finished = time.monotonic()
        payload = response.payload
        status = response.status
        outcome = ("success" if 200 <= status < 300 else "conflict" if status == 409
                   else "client_unknown" if status == 0 else "error")
        order = payload.get("order") or {}
        self.journal.write("requests", scope=scope, phase=phase, phase_finished=self.phase,
                request_started_seconds=round(started - self.journal.zero, 6),
                request_finished_seconds=round(finished - self.journal.zero, 6),
                client_queue_ms=None if response.client_queue_ms is None else round(response.client_queue_ms, 3),
                transport_ms=None if response.transport_ms is None else round(response.transport_ms, 3),
                transport_attempted=response.transport_attempted,
                workflow_number=getattr(self.workflow_context, "number", None),
                operation=operation, status=status,
                outcome=outcome, latency_ms=round(response.milliseconds, 3), request_id=response.request_id,
                error=payload.get("error"), source=payload.get("source"), cache=payload.get("cache"),
                order_id=order.get("id"), version=order.get("version"))
        return response

    def contract(self, code, **fields):
        with self.lock:
            self.contract_errors.append({"code": code, **fields})
        self.journal.write("timeline", stage="contract_violation", code=code, **fields)

    def payload(self, name, quantity=1, price=100):
        return {"item": self.run_id + "-" + name, "quantity": quantity, "unit_price": price}

    def register(self, key, payload):
        with self.lock:
            self.intents.setdefault(key, {"key": key, "payload": dict(payload), "ids": [],
                                          "acknowledged": {}, "attempted_updates": []})

    def acknowledge(self, key, response):
        if response.status not in {200, 201}:
            return
        order = response.payload.get("order")
        if not isinstance(order, dict) or not all(k in order for k in ORDER_FIELDS):
            self.contract("invalid_order_acknowledgement")
            return
        with self.lock:
            intent = self.intents[key]
            if any(order[k] != intent["payload"][k] for k in ("item", "quantity", "unit_price")):
                self.contract("acknowledged_payload_changed", key=key)
            if order["id"] not in intent["ids"]:
                intent["ids"].append(order["id"])
            if len(intent["ids"]) > 1:
                self.contract("same_key_multiple_order_ids", key=key)
            old = intent["acknowledged"].get(str(order["version"]))
            if old is not None and differences(old, order):
                self.contract("same_version_different_content", key=key)
            intent["acknowledged"][str(order["version"])] = dict(order)

    def create(self, key, payload, scope="workload"):
        self.register(key, payload)
        result = self.call("POST", "/api/orders", payload, key, "create", scope)
        self.acknowledge(key, result)
        return result

    def source(self, key, scope="workload", timeout=5):
        return self.call("GET", "/api/study/key/" + quote(key, safe=""), operation="source_lookup", scope=scope, timeout=timeout)

    def update(self, key, order, status, scope="workload"):
        with self.lock:
            self.intents[key]["attempted_updates"].append([order["version"] + 1, status])
        result = self.call("PATCH", "/api/orders/" + order["id"],
                           {"status": status, "expected_version": order["version"]}, operation="update", scope=scope)
        self.acknowledge(key, result)
        return result

    def workflow(self, op):
        self.workflow_context.number = op["number"]
        outcome = "completed"
        self.journal.write("timeline", stage="workflow_started", operation_number=op["number"], kind=op["kind"])
        try:
            if self.cancel.is_set():
                outcome = "cancelled"
                return
            return self._workflow(op)
        except Exception:
            outcome = "failed"
            raise
        finally:
            self.journal.write("timeline", stage="workflow_finished", operation_number=op["number"], outcome=outcome)
            self.workflow_context.number = None

    def _workflow(self, op):
        if self.cancel.is_set():
            return
        key = self.run_id + "-fixture-" + str(op["target"])
        with self.lock:
            intent = self.intents[key]
            payload = dict(intent["payload"])
        kind = op["kind"]
        if kind == "create":
            self.create(self.run_id + "-order-" + str(op["number"]),
                        self.payload("order-" + str(op["number"]), op["quantity"], op["unit_price"]))
        elif kind == "retry":
            for _ in range(3):
                self.create(key, payload)
        elif kind == "race":
            key = self.run_id + "-race-" + str(op["number"])
            response = self.create(key, self.payload("race-" + str(op["number"])))
            if response.status not in {200, 201}:
                return
            order = response.payload["order"]
            barrier = threading.Barrier(2)
            def change(status):
                self.workflow_context.number = op["number"]
                try:
                    barrier.wait(timeout=5)
                    return self.update(key, order, status)
                finally:
                    self.workflow_context.number = None
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(change, s) for s in ("paid", "cancelled")]
                statuses = sorted(f.result().status for f in futures)
            with self.lock:
                self.races.append(statuses)
            if statuses == [200, 200]:
                self.contract("two_winners_same_expected_version", key=key)
            elif all(s in {200, 409} for s in statuses) and statuses != [200, 409]:
                self.contract("invalid_race_result", key=key, statuses=statuses)
        else:
            if kind == "lock_update" and not self.fault_started:
                kind = "read"
            # A cache read must NOT first query SQL: that would hide the ability
            # to serve cached orders while the primary is unavailable.
            if kind in {"read", "search"}:
                with self.lock:
                    known = self.intents[key]["ids"][:]
                if known:
                    if kind == "read":
                        self.call("GET", "/api/orders/" + known[0], operation="detail")
                    else:
                        self.call("GET", "/api/search?q=" + known[0], operation="search")
                return
            response = self.source(key)
            order = response.payload.get("order") if response.status == 200 else None
            if not order:
                return
            if kind in {"update", "lock_update"}:
                status = {"created": "paid", "paid": "shipped"}.get(order["status"])
                if status:
                    self.update(key, order, status)
                else:
                    self.call("GET", "/api/orders/" + order["id"], operation="terminal_read")
            elif kind == "read":
                self.call("GET", "/api/orders/" + order["id"], operation="detail")
            elif kind == "search":
                self.call("GET", "/api/search?q=" + order["id"], operation="search")

    def audit(self, deadline):
        with self.lock:
            intents = json.loads(json.dumps(self.intents))
        rows = []
        for key, intent in intents.items():
            if time.monotonic() >= deadline:
                rows.append({"key": key, "state": "not_observed_before_deadline", "consistent": False})
                continue
            lookup = self.source(key, "audit", timeout=min(5, max(.05, deadline - time.monotonic())))
            order = lookup.payload.get("order") if lookup.status == 200 else None
            inspected = None
            if order and time.monotonic() < deadline:
                inspected = self.call("GET", "/api/study/order/" + order["id"],
                                      operation="read_only_comparison", scope="audit",
                                      timeout=min(5, max(.05, deadline - time.monotonic())))
            row = {"key": key, **inspect_intent(intent, lookup, inspected)}
            rows.append(row)
            self.journal.write("audits", **row)
        return {"rows": rows, "consistent": bool(rows) and all(r["consistent"] for r in rows),
                "acknowledged_orders": sum(bool(i["acknowledged"]) for i in intents.values())}

    def converge(self, seconds):
        deadline = time.monotonic() + seconds
        streak = 0
        latest = {"rows": [], "consistent": False}
        while time.monotonic() < deadline and not self.cancel.is_set():
            latest = self.audit(deadline)
            streak = streak + 1 if latest["consistent"] and latest["acknowledged_orders"] else 0
            self.journal.write("timeline", stage="audit_round", consistent=latest["consistent"], streak=streak)
            if streak >= 2:
                return {**latest, "stable_rounds": streak, "observed_at": time.monotonic()}
            self.cancel.wait(.75)
        return {**latest, "consistent": False, "stable_rounds": streak, "deadline_exceeded": True}

    def sample(self):
        response = self.call("GET", "/api/diagnostics", operation="diagnostics", scope="observer", timeout=12)
        data = response.payload if response.status == 200 else {"diagnostic_status": response.status}
        self.journal.write("timeline", stage="diagnostics", phase=self.phase, data=data)
        return data

    def observe(self):
        while not self.observer_stop.is_set():
            self.sample()
            self.observer_stop.wait(3)

    def fault(self):
        action, service, _ = SCENARIOS[self.plan.scenario]
        due = (self.workload_zero if self.workload_zero is not None else time.monotonic()) + self.plan.fault_at
        if not action or self.cancel.wait(max(0, due - time.monotonic())):
            return
        try:
            self.phase = "fault_transition"
            self.journal.write("timeline", stage="fault_requested", action=action, service=service)
            with self.lock:
                fixture = self.intents[self.run_id + "-fixture-0"]
                order_id = fixture["ids"][0]
            applied = self.lab.apply(action, service, self.run_id, self.plan.fault_for, order_id=order_id)
            self.fault_started = True
            self.phase = "fault"
            self.journal.write("timeline", stage="fault_applied", detail=applied)
            self.cancel.wait(self.plan.fault_for)
        except Exception as exc:
            self.fault_error = type(exc).__name__
            self.journal.write("timeline", stage="fault_error", error=self.fault_error)
        finally:
            try:
                self.journal.write("timeline", stage="fault_restore_requested")
                result = self.lab.restore()
                self.restored_at = time.monotonic()
                self.fault_completed = bool(result["restored"])
                self.phase = "recovery"
                self.journal.write("timeline", stage="fault_restore", detail=result)
            except Exception as exc:
                self.fault_error = type(exc).__name__
                self.journal.write("timeline", stage="restore_failed", error=self.fault_error,
                        next_command="bash ./all.sh mvp simulate recover --yes")

    def schedule(self, operations):
        start = self.workload_zero if self.workload_zero is not None else time.monotonic()
        end = start + self.plan.seconds
        inflight = set()
        with ThreadPoolExecutor(max_workers=self.plan.workers) as pool:
            for op in operations:
                if self.cancel.is_set() or time.monotonic() >= end:
                    self.skipped += len(operations) - op["number"]
                    break
                due = start + op["at_seconds"]
                self.cancel.wait(max(0, due - time.monotonic()))
                if self.cancel.is_set():
                    self.skipped += len(operations) - op["number"]
                    break
                done = {f for f in inflight if f.done()}
                for future in done:
                    future.result()
                inflight -= done
                # No accumulating queue and no catch-up loop if the scheduler fell behind.
                if len(inflight) >= self.plan.workers or time.monotonic() - due >= 1 / self.plan.rate:
                    self.skipped += 1
                    self.journal.write("timeline", stage="workflow_skipped", operation_number=op["number"],
                                       reason="client_capacity_or_schedule_late")
                    continue
                self.admitted += 1
                self.journal.write("timeline", stage="workflow_admitted", operation_number=op["number"],
                                   kind=op["kind"], lateness_ms=round((time.monotonic() - due) * 1000, 3))
                inflight.add(pool.submit(self.workflow, op))
            for future in inflight:
                future.result()
            # Keep the observation window even when the final operation completes early.
            self.cancel.wait(max(0, end - time.monotonic()))
        return time.monotonic() - start

    def effect_observed(self):
        scenario = self.plan.scenario
        requests = self.journal.rows["requests"]
        diagnostic = [r["data"].get("dependencies", {}) for r in self.journal.rows["timeline"]
                      if r.get("stage") == "diagnostics" and r.get("phase") == "fault"]
        if scenario in {"db-network-delay", "db-network-loss"}:
            details = [r.get("detail", {}).get("netem", {}) for r in self.journal.rows["timeline"]
                       if r.get("stage") == "fault_restore"]
            stats = [s for d in details for s in d.get("statistics", [])]
            if scenario == "db-network-loss":
                return any(s.get("drops", 0) > 0 for s in stats)
            before = [r["latency_ms"] for r in requests if r["scope"] == "workload" and r["phase"] == "baseline"]
            during = [r["latency_ms"] for r in requests if r["scope"] == "workload" and r["phase"] == "fault"]
            return bool(before and during and any(s.get("packets", 0) > 0 for s in stats)
                        and percentiles(during)["p50_ms"] >= percentiles(before)["p50_ms"] + 100)
        if scenario == "baseline":
            return True
        if scenario == "version-race":
            return [200, 409] in self.races
        if scenario == "duplicate-retry":
            return any(r["scope"] == "workload" and r["operation"] == "create" and r["status"] == 200 for r in requests)
        if scenario == "row-lock":
            return any(r.get("error") == "mariadb_lock_wait_timeout" for r in requests)
        if scenario in {"redis-recreate", "redis-switch"}:
            return self.fault_started
        if scenario == "redis-outage":
            return any(r.get("cache") in {"unavailable", "unavailable_or_invalid"} for r in requests)
        if scenario == "db-freeze":
            return any(r["phase"] in {"fault", "fault_transition"} and r["scope"] == "workload"
                       and (r["status"] == 0 or r["status"] >= 500) for r in requests)
        if scenario in {"kafka-outage", "worker-freeze"}:
            return any(d.get("mariadb", {}).get("outbox_pending", 0) > 0 for d in diagnostic)
        if scenario == "es-outage":
            return any(d.get("elasticsearch", {}).get("reachable") is False for d in diagnostic)
        return False

    def run(self):
        summary = {"schema": 1, "run_id": self.run_id, "scenario": self.plan.scenario,
                   "evidence_kind": self.evidence_kind, "status": "failed", "fault_restored": False, "measurement_schema": 2}
        fault_thread = observer = None
        final = {"consistent": False, "rows": []}
        started = time.monotonic()
        aborted = False
        try:
            snapshot = self.lab.preflight()
            self.journal.json("runtime-snapshot.json", snapshot)
            if self.context_provider is not None:
                self.comparison_context.update(available=True, before=self.context_provider(snapshot))
            info = self.call("GET", "/api/study/info", operation="identity", scope="setup")
            if (info.status != 200 or info.payload.get("application") != "db-lab-mvp"
                    or info.payload.get("study_api") != 2 or info.payload.get("project") != self.project):
                raise RuntimeError("Study API identity/version mismatch; rebuild this MVP first")
            for i in range(6):
                response = self.create(self.run_id + "-fixture-" + str(i), self.payload("fixture-" + str(i)), "setup")
                if response.status != 201:
                    raise RuntimeError("Clean baseline fixture creation failed")
                order_id = response.payload["order"]["id"]
                for _ in range(2):
                    self.call("GET", "/api/orders/" + order_id, operation="prime_cache", scope="setup")
            baseline = self.converge(min(60, self.plan.recovery_timeout))
            if not baseline["consistent"]:
                raise RuntimeError("Baseline source/search/cache comparison did not converge")
            data = self.sample()
            if not data.get("dependencies_reachable"):
                raise RuntimeError("A baseline dependency is unreachable")
            self.phase = "baseline"
            self.workload_zero = time.monotonic()
            self.journal.write("timeline", stage="workload_started",
                               clock_origin_seconds=round(self.workload_zero - self.journal.zero, 6))
            fault_thread = threading.Thread(target=self.fault, name="bounded-fault")
            observer = threading.Thread(target=self.observe, name="bounded-observer")
            fault_thread.start(); observer.start()
            summary["workload_elapsed_seconds"] = round(self.schedule(self.plan.operations()), 6)
            fault_thread.join()
            if self.fault_error:
                raise RuntimeError("Fault application/restoration failed; inspect timeline and active marker")
            self.phase = "settling"
            final = self.converge(self.plan.recovery_timeout)
            diagnostics = self.sample()
            if self.context_provider is not None:
                after = self.context_provider(self.lab.preflight())
                self.comparison_context["after"] = after
                self.comparison_context["stable"] = comparable_context(after) == comparable_context(self.comparison_context["before"])
                if not self.comparison_context["stable"]:
                    self.contract("experiment_context_changed")
            if self.admitted + self.skipped != len(self.plan.operations()):
                self.contract("planned_workflow_accounting_mismatch")
            if self.admitted == 0 or self.workload_requests == 0:
                self.contract("no_workload_observed")
            effect = self.effect_observed()
            action = SCENARIOS[self.plan.scenario][0]
            fault_ok = not action or (self.fault_started and self.fault_completed)
            baseline_errors = [r for r in self.journal.rows["requests"] if r["scope"] == "workload"
                               and r["outcome"] in {"error", "client_unknown"}]
            all_ok = (final["consistent"] and not self.contract_errors and fault_ok
                      and diagnostics.get("dependencies_reachable") is True)
            if self.plan.scenario in {"baseline", "version-race", "duplicate-retry"} and baseline_errors:
                all_ok = False
            summary["status"] = "passed" if all_ok and effect else "inconclusive" if all_ok else "failed"
            summary["fault_effect_observed"] = effect
            summary["final_dependencies_reachable"] = diagnostics.get("dependencies_reachable")
            if self.restored_at is not None and final.get("observed_at"):
                summary["observed_convergence_seconds_after_restore"] = round(final["observed_at"] - self.restored_at, 3)
        except KeyboardInterrupt:
            aborted = True
            self.cancel.set()
            summary["status"] = "aborted"
        except Exception as exc:
            summary["failure_kind"] = type(exc).__name__
            self.journal.write("timeline", stage="run_failed", phase=self.phase, error=type(exc).__name__,
                               hint=str(exc)[:240] if isinstance(exc, (RuntimeError, ValueError)) else "inspect engine or HTTP diagnostics")
        finally:
            self.cancel.set(); self.observer_stop.set()
            if fault_thread is not None:
                fault_thread.join()
            if observer is not None:
                observer.join()
            # The fault thread owns restoration. If interrupted before it started,
            # no action was issued. A failed restore must keep its durable marker.
            action = SCENARIOS[self.plan.scenario][0]
            summary.update({"fault_applied": self.fault_started,
                "fault_restored": not action or self.fault_completed,
                "admitted_workflows": self.admitted, "skipped_workflows": self.skipped,
                "workload_http_requests": self.workload_requests,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "final_consistency": {k: v for k, v in final.items() if k != "observed_at"},
                "contract_errors": self.contract_errors, "race_results": self.races,
                "request_statistics": request_summary(self.journal.rows["requests"]),
                "metric_limits": ["Client HTTP latency includes client slot wait, not isolated DB latency.",
                                  "Observed convergence is a sampled upper bound, not certified RTO/RPO.",
                                  "Timeouts are ambiguous. Missing acknowledgement is not proof of data loss.",
                                  "Seed repeats planned inputs/order, not engine timing, UUIDs or thread interleavings.",
                                  "Audit is read-only, but extra reads add load. Cache MISS is not corruption."]})
            summary["comparison_context_stable"] = self.comparison_context["stable"]
            self.journal.json("comparison-context.json", self.comparison_context)
            self.journal.json("summary.json", summary)
            self.journal.json("intents.json", self.intents)
            write_report(self.journal.path, summary)
            self.journal.close()
        return summary


def write_report(path, summary):
    rows = summary.get("final_consistency", {}).get("rows", [])
    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    lines = ["# DB 시뮬레이션 실행 보고서", "", f"실행: `{summary['run_id']}` · 시나리오: `{summary['scenario']}`",
             f"판정: **{summary['status']}** · 증거 유형: `{summary['evidence_kind']}`", "",
             "## 실행과 복구", "", f"장애 적용: {summary['fault_applied']} / 복원 확인: {summary['fault_restored']}",
             f"수용 workflow: {summary['admitted_workflows']} / 건너뛴 계획 슬롯: {summary['skipped_workflows']}",
             f"부하 HTTP 요청: {summary['workload_http_requests']} / 총 관측 시간: {summary['elapsed_seconds']}초", "",
             "## 주문별 최종 상태", "", "```json", json.dumps(counts, ensure_ascii=False, indent=2), "```", "",
             "## 응답 지연 — 성공·실패·충돌을 분리", "",
             "|구간|범위|작업|결과|표본|p50 ms|p95 ms|p99 ms|", "|---|---|---|---|---:|---:|---:|---:|"]
    for r in summary["request_statistics"]:
        lines.append("|" + "|".join(str(r[k]) for k in ("phase", "scope", "operation", "outcome", "count", "p50_ms", "p95_ms", "p99_ms")) + "|")
    lines += ["", "## 해석 제한", "", *["- " + x for x in summary["metric_limits"]], "",
              "전체 필드/주문별 대조는 summary.json, 요청별 증적은 requests.jsonl, 장애 시각은 timeline.jsonl을 확인하세요.",
              "실패·미확인 항목을 데이터 유실 또는 물리 HA 실패로 자동 판정하지 않습니다."]
    (path / "report.md").write_text("\n".join(lines) + "\n")


def add_parser(sub):
    sim = sub.add_parser("simulate", help="Bounded study scenarios; no destructive reset or remote automation")
    actions = sim.add_subparsers(dest="simulation_action", required=True)
    actions.add_parser("list")
    recover = actions.add_parser("recover", help="Restore only a persisted interrupted drill")
    recover.add_argument("--yes", action="store_true", required=True)
    for action in ("plan", "run"):
        p = actions.add_parser(action)
        p.add_argument("scenario", choices=SCENARIOS)
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--seconds", type=int, default=40)
        p.add_argument("--rate", type=float, default=2, help="Workflows/s, not DB TPS; 0.2..10")
        p.add_argument("--workers", type=int, default=4, help="Maximum HTTP concurrency 1..8")
        p.add_argument("--fault-at", type=int, default=8)
        p.add_argument("--fault-for", type=int, default=10)
        p.add_argument("--recovery-timeout", type=int, default=90)
        p.add_argument("--workload", choices=WORKLOADS, default="mixed")
        if action == "run":
            p.add_argument("--yes", action="store_true", required=True,
                           help="Allow synthetic writes and the selected bounded fault; data remains afterwards")


def cli(args, manage):
    action = args.simulation_action
    if action == "list":
        print(json.dumps({k: {"action": v[0], "service": v[1], "description": v[2]} for k, v in SCENARIOS.items()},
                         ensure_ascii=False, indent=2))
        return 0
    plan = None
    if action in {"plan", "run"}:
        plan = Plan(**{k: getattr(args, k) for k in Plan.__dataclass_fields__}).validate()
        if action == "plan":
            print(json.dumps(plan.document(), ensure_ascii=False, indent=2))
            return 0
    with manage.lock():
        config = manage.validate(manage.parse_env(manage.ROOT / ".env"))
        compose = manage.Compose(config)
        lab = DockerLab(compose, manage)
        if action == "recover":
            print(json.dumps(lab.restore(), ensure_ascii=False, indent=2))
            return 0
        run_id = "sim-" + uuid.uuid4().hex[:12]
        root = manage.ROOT / "reports/simulations" / run_id
        journal = Journal(root)
        journal.json("plan.json", plan.document())
        files = sorted(p for p in manage.ROOT.rglob("*") if p.is_file() and p.suffix in {".py", ".yaml", ".txt"}
                       and not any(x in p.parts for x in ("reports", ".state", "__pycache__")))
        journal.json("source-manifest.json", {str(p.relative_to(manage.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                              for p in files})
        journal.json("client-runtime.json", {"python": platform.python_version(), "platform": platform.system()})
        http = HTTP("http://127.0.0.1:" + config["API_PORT"], run_id, plan.workers)
        runner = Runner(plan, http, lab, journal, run_id, config["MVP_PROJECT"],
                        context_provider=lambda states: capture_context(manage.ROOT, compose, states))
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(*_):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, terminate)
        try:
            result = runner.run()
        finally:
            signal.signal(signal.SIGTERM, previous)
        print(json.dumps({"status": result["status"], "run_id": run_id, "report": str(root / "report.md"),
                          "fault_restored": result["fault_restored"]}, ensure_ascii=False, indent=2))
        return {"passed": 0, "failed": 1, "inconclusive": 2, "aborted": 130}[result["status"]]
