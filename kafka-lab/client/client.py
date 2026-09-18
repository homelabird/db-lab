#!/usr/bin/env python3
"""Kafka/ZooKeeper lab client. Live calls are bounded; failed deliveries are fatal."""
from __future__ import annotations
import argparse
from collections import Counter, deque
import json
import math
import os
import random
import re
import socket
import sys
import time
import uuid
from typing import Any

from dataset import KINDS, PROFILES, TOPICS, make_event, utc_now


class LabError(RuntimeError):
    pass


def kafka_modules():
    # Lazy import permits dataset/unit tests without Kafka or pip on the host.
    import confluent_kafka as ck
    from confluent_kafka import admin
    return ck, admin


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def checked_topic(topic: str) -> str:
    if not re.fullmatch(r"lab\.[a-zA-Z0-9._-]{1,200}", topic):
        raise LabError("쓰기/관리 대상 토픽은 lab. 접두사와 영문/숫자/._-만 허용합니다.")
    return topic


def positive(text: str) -> int:
    n = int(text)
    if n <= 0:
        raise argparse.ArgumentTypeError("positive integer required")
    return n


def admin_client(bootstrap: str):
    _, ka = kafka_modules()
    return ka.AdminClient({"bootstrap.servers": bootstrap, "socket.timeout.ms": 10000,
                           "socket.connection.setup.timeout.ms": 5000})


def consumer(bootstrap: str, group: str):
    ck, _ = kafka_modules()
    return ck.Consumer({"bootstrap.servers": bootstrap, "group.id": group,
                        "auto.offset.reset": "earliest", "enable.auto.commit": False,
                        "enable.auto.offset.store": False, "enable.partition.eof": True,
                        "allow.auto.create.topics": False, "session.timeout.ms": 10000,
                        "max.poll.interval.ms": 300000, "socket.timeout.ms": 10000})


def metadata(bootstrap: str, topic: str | None = None) -> dict:
    a = admin_client(bootstrap)
    md = a.list_topics(topic=topic, timeout=8)
    topics = {}
    for name, t in sorted(md.topics.items()):
        topics[name] = {"error": str(t.error) if t.error else None, "partitions": [
            {"partition": p.id, "leader": p.leader, "replicas": list(p.replicas),
             "isr": list(p.isrs), "error": str(p.error) if p.error else None}
            for _, p in sorted(t.partitions.items())]}
    return {"brokers": sorted(md.brokers), "controller": md.controller_id, "topics": topics}


def get_partitions(snapshot: dict, topic: str) -> list[dict]:
    t = snapshot["topics"].get(topic)
    if t is None or t["error"] or not t["partitions"]:
        raise LabError(f"토픽 메타데이터 없음/오류: {topic}: {t}")
    return t["partitions"]


def check_state(s: dict, brokers: int | None = None, isr: int | None = None,
                topic: str | None = None, leader_not: int | None = None,
                controller_not: int | None = None) -> list[str]:
    problems = []
    if brokers is not None and len(s["brokers"]) != brokers:
        problems.append(f"brokers={s['brokers']}, expected count={brokers}")
    if s["controller"] not in s["brokers"]:
        problems.append(f"invalid controller={s['controller']}")
    if controller_not is not None and s["controller"] == controller_not:
        problems.append(f"controller is still {controller_not}")
    names = [topic] if topic else list(s["topics"])
    for name in names:
        try:
            ps = get_partitions(s, name)
        except LabError as e:
            problems.append(str(e))
            continue
        for p in ps:
            if p["error"] or p["leader"] < 0 or p["leader"] not in s["brokers"]:
                problems.append(f"{name}/{p['partition']}: no valid leader")
            if leader_not is not None and p["leader"] == leader_not:
                problems.append(f"{name}/{p['partition']}: leader is still {leader_not}")
            want = isr if isr is not None else len(p["replicas"])
            if len(p["isr"]) != want or not set(p["isr"]).issubset(p["replicas"]):
                problems.append(f"{name}/{p['partition']}: ISR={p['isr']}, expected count={want}")
    return problems


def wait_state(args) -> None:
    deadline = time.monotonic() + args.timeout
    last = ""
    while True:
        try:
            s = metadata(args.bootstrap, args.topic)
            problems = check_state(s, args.brokers, args.isr, args.topic,
                                   args.leader_not, args.controller_not)
            if not problems:
                emit({"status": "PASS", "brokers": s["brokers"], "controller": s["controller"],
                      "topic": args.topic, "isr": args.isr if args.isr is not None else "full"})
                return
            last = "; ".join(problems[:5])
        except Exception as e:
            last = str(e)
        if time.monotonic() >= deadline:
            raise LabError(f"상태 수렴 시간 초과: {last}")
        print(f"waiting: {last}", file=sys.stderr, flush=True)
        time.sleep(2)


def zk_role(address: str) -> dict:
    host, port = address.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=2) as sock:
            sock.settimeout(2)
            sock.sendall(b"srvr")
            chunks = []
            while True:
                try:
                    buf = sock.recv(4096)
                except socket.timeout:
                    break
                if not buf:
                    break
                chunks.append(buf)
                if sum(map(len, chunks)) > 65536:
                    break
        text = b"".join(chunks).decode(errors="replace")
        match = re.search(r"Mode:\s*(\w+)", text)
        role = match.group(1).lower() if match else "not-serving"
        return {"server": address, "role": role,
                "serving": role in ("leader", "follower"), "detail": text.strip()[:1000]}
    except (OSError, ValueError) as e:
        return {"server": address, "role": "unreachable", "serving": False, "detail": str(e)}


def zk_status(args) -> None:
    deadline = time.monotonic() + args.timeout
    while True:
        states = [zk_role(a) for a in args.servers.split(",")]
        count = sum(s["serving"] for s in states)
        leaders = sum(s["role"] == "leader" for s in states)
        if args.serving is None or (count == args.serving and (count == 0 or leaders == 1)):
            emit({"serving": count, "leaders": leaders, "servers": states})
            return
        if time.monotonic() >= deadline:
            emit({"serving": count, "leaders": leaders, "servers": states})
            raise LabError(f"ZooKeeper serving expected={args.serving}, actual={count}")
        time.sleep(2)


def create_topic(bootstrap: str, topic: str, partitions: int = 3,
                 configs: dict | None = None, assignment: list[int] | None = None,
                 exist_ok: bool = False, timeout: int = 15) -> None:
    checked_topic(topic)
    ck, ka = kafka_modules()
    rf = int(os.getenv("REPLICATION_FACTOR", "3"))
    min_isr = min(2, rf)
    cfg = {"min.insync.replicas": str(min_isr), "unclean.leader.election.enable": "false"}
    cfg.update(configs or {})
    if assignment is not None:
        if partitions != 1 or set(assignment) != set(range(1, rf + 1)) or len(assignment) != rf:
            raise LabError(f"고정 배치는 partitions=1, assignment=1..{rf}의 순열만 허용합니다.")
        nt = ka.NewTopic(topic, num_partitions=1, replica_assignment=[assignment], config=cfg)
    else:
        nt = ka.NewTopic(topic, num_partitions=partitions, replication_factor=rf, config=cfg)
    a = admin_client(bootstrap)
    try:
        a.create_topics([nt], request_timeout=timeout, operation_timeout=timeout)[topic].result(timeout + 3)
    except ck.KafkaException as e:
        if not (exist_ok and e.args[0].code() == ck.KafkaError.TOPIC_ALREADY_EXISTS):
            raise
        ps = get_partitions(metadata(bootstrap, topic), topic)
        if len(ps) != partitions or any(len(p["replicas"]) != rf for p in ps):
            raise LabError(f"기존 토픽의 partition/RF가 기대값과 다릅니다: {topic}")
    emit({"created_or_exists": topic, "partitions": partitions, "replication_factor": rf})


def parse_configs(items: list[str]) -> dict:
    cfg = {}
    for item in items:
        if "=" not in item:
            raise LabError("--config requires key=value")
        k, v = item.split("=", 1)
        if not k or not v:
            raise LabError("empty config key/value")
        cfg[k] = v
    return cfg


def topic_create(args) -> None:
    assignment = list(map(int, args.assignment.split(","))) if args.assignment else None
    create_topic(args.bootstrap, args.topic, args.partitions, parse_configs(args.config),
                 assignment, args.exist_ok, args.timeout)


def init_topics(args) -> None:
    for kind, count in [("payments", 12), ("access", 6), ("metrics", 6)]:
        create_topic(args.bootstrap, TOPICS[kind], count, exist_ok=True)
    create_topic(args.bootstrap, "lab.manual", 3, exist_ok=True)


def make_producer(bootstrap: str, timeout_ms: int = 15000, probe: bool = False):
    ck, _ = kafka_modules()
    return ck.Producer({"bootstrap.servers": bootstrap, "client.id": "kzk-lab-producer",
                        "acks": "all", "enable.idempotence": not probe,
                        "message.send.max.retries": 0 if probe else 100,
                        "message.timeout.ms": timeout_ms, "request.timeout.ms": 5000,
                        "socket.timeout.ms": 10000, "compression.type": "none",
                        "queue.buffering.max.kbytes": 32768, "batch.size": 16384, "linger.ms": 5})


def publish(args) -> dict:
    topic = checked_topic(args.topic or TOPICS[args.kind])
    get_partitions(metadata(args.bootstrap, topic), topic)  # fail fast; no silent auto-creation
    if not 0 <= args.payload_bytes <= 2 * 1024 * 1024:
        raise LabError("payload-bytes range: 0..2097152")
    if args.count is not None and args.count > 2_000_000:
        raise LabError("한 번의 --count 상한은 2,000,000입니다.")
    if args.mib is not None and not 0 < args.mib <= 512:
        raise LabError("한 번의 --mib 범위는 0 < MiB <= 512입니다.")
    if args.duration is not None and not 0 < args.duration <= 600:
        raise LabError("--duration range: 1..600 seconds")
    if not math.isfinite(args.rate) or args.rate < 0:
        raise LabError("--rate must be finite and >= 0 (0 = unlimited)")
    count = args.count if args.count is not None else (1000 if args.mib is None and args.duration is None else None)
    target = int(args.mib * 1024 * 1024) if args.mib is not None else None
    rid = args.run_id or uuid.uuid4().hex[:12]
    rng, base, p = random.Random(args.seed), utc_now(), make_producer(args.bootstrap)
    totals = {"queued": 0, "delivered": 0, "failed": 0, "logical_bytes": 0}
    errors = Counter()
    latencies = deque(maxlen=10000)
    def delivered(err, msg):
        if err is not None:
            totals["failed"] += 1
            errors[err.name()] += 1
            if totals["failed"] <= 5:
                print(f"DELIVERY FAILED: {err}", file=sys.stderr, flush=True)
        else:
            totals["delivered"] += 1
            if msg.latency() is not None:
                latencies.append(round(msg.latency() * 1000, 3))
    start = time.monotonic()
    while True:
        n = totals["queued"]
        if count is not None and n >= count:
            break
        if target is not None and totals["logical_bytes"] >= target:
            break
        if args.duration is not None and time.monotonic() - start >= args.duration:
            break
        if totals["failed"]:
            break
        key, value = make_event(args.kind, n, rng, rid, base, args.payload_bytes,
                                args.hot_key, getattr(args, "profile", "baseline"))
        queue_deadline = time.monotonic() + 20
        while True:
            try:
                p.produce(topic, key=key, value=value, on_delivery=delivered)
                break
            except BufferError:
                p.poll(0.1)
                if totals["failed"] or time.monotonic() > queue_deadline:
                    raise LabError("producer queue full / delivery error; 데이터 적재 실패")
        totals["queued"] += 1
        totals["logical_bytes"] += len(key) + len(value)
        p.poll(0)
        if args.rate:
            next_send = start + totals["queued"] / args.rate
            if args.duration is not None:
                next_send = min(next_send, start + args.duration)
            # Poll callbacks during rate limiting; never sleep past a duration limit.
            while not totals["failed"]:
                delay = next_send - time.monotonic()
                if delay <= 0:
                    break
                p.poll(min(delay, .25))
    pending = p.flush(20)
    elapsed = round(time.monotonic() - start, 3)
    ordered = sorted(latencies)
    result = {"topic": topic, "run_id": rid, **totals, "pending": pending,
              "errors": dict(errors), "elapsed_seconds": elapsed,
              "delivered_per_second": round(totals["delivered"] / max(elapsed, .001), 1),
              "logical_mib": round(totals["logical_bytes"] / 1048576, 3),
              "ack_latency_p95_ms_last_10000": ordered[min(len(ordered)-1, int(len(ordered)*.95))] if ordered else None}
    emit(result)
    if pending or totals["failed"] or totals["delivered"] != totals["queued"]:
        raise LabError("브로커 delivery 확인 실패: queued를 성공 건수로 세지 않습니다.")
    return result


def simulate(args) -> None:
    if not math.isfinite(args.interval) or args.interval < 0 or args.interval > 3600:
        raise LabError("--interval range: 0..3600 seconds")
    if args.batch_count <= 0 or args.batch_count > 100000:
        raise LabError("--batch-count range: 1..100000")
    if args.batches is not None and (args.batches <= 0 or args.batches > 100000):
        raise LabError("--batches range: 1..100000")
    if args.duration is not None and (args.duration <= 0 or args.duration > 86400):
        raise LabError("--duration range: 1..86400 seconds")
    if args.batches is None and args.duration is None:
        raise LabError("시뮬레이션은 --batches 또는 --duration 중 하나가 필요합니다.")
    phases = parse_phases(args.phases)
    mix = parse_mix(args.mix)
    if args.kind == "all" and args.topic:
        raise LabError("--topic cannot be used with --kind all")
    if args.kind != "all" and args.mix:
        raise LabError("--mix requires --kind all")
    if not math.isfinite(args.jitter) or args.jitter < 0 or args.jitter > 100:
        raise LabError("--jitter range: 0..100 percent")
    if args.burst_multiplier < 1 or args.burst_multiplier > 20:
        raise LabError("--burst-multiplier range: 1..20")
    if args.burst_every < 0:
        raise LabError("--burst-every must be >= 0")
    started = time.monotonic()
    batch = 0
    totals = {"batches": 0, "queued": 0, "delivered": 0, "failed": 0}
    schedule_rng = random.Random(args.seed)
    try:
        while (args.batches is None or batch < args.batches) and (
            args.duration is None or time.monotonic() - started < args.duration):
            remaining = None if args.duration is None else max(1, int(args.duration - (time.monotonic() - started)))
            ns = argparse.Namespace(**vars(args))
            ns.kind = choose_kind(args.kind, mix, schedule_rng, batch)
            ns.profile = choose_profile(phases, batch, args.profile)
            ns.count = args.batch_count * (args.burst_multiplier if args.burst_every and
                                            (batch + 1) % args.burst_every == 0 else 1)
            ns.mib = None
            ns.duration = None
            ns.run_id = f"{args.run_id or uuid.uuid4().hex[:8]}-{batch:05d}"
            if ns.kind == "all":
                raise LabError("internal error: unresolved simulation kind")
            result = publish(ns)
            batch += 1
            totals["batches"] = batch
            for key in ("queued", "delivered", "failed"):
                totals[key] += result[key]
            emit({"status": "batch", "batch": batch, "kind": ns.kind,
                  "profile": ns.profile, "count": ns.count, **result})
            if remaining is not None and remaining <= 0:
                break
            if args.batches is not None and batch >= args.batches:
                break
            deadline = time.monotonic() + args.interval
            if args.jitter:
                spread = args.interval * args.jitter / 100
                deadline = time.monotonic() + max(0, args.interval + schedule_rng.uniform(-spread, spread))
            while time.monotonic() < deadline:
                time.sleep(min(.25, deadline - time.monotonic()))
    except KeyboardInterrupt:
        emit({"status": "interrupted", **totals})
        return
    emit({"status": "completed", **totals, "elapsed_seconds": round(time.monotonic() - started, 3)})


def parse_mix(text: str | None) -> list[tuple[str, int]]:
    if not text:
        return []
    rows = []
    for item in text.split(","):
        try:
            kind, weight = item.split("=", 1)
            weight = int(weight)
        except ValueError as e:
            raise LabError("--mix 형식은 payments=60,access=30입니다.") from e
        if kind not in KINDS or weight <= 0:
            raise LabError("--mix에는 유효한 kind와 양의 weight가 필요합니다.")
        rows.append((kind, weight))
    if len({kind for kind, _ in rows}) != len(rows):
        raise LabError("--mix kind은 중복될 수 없습니다.")
    return rows


def parse_phases(text: str | None) -> list[tuple[int, str]]:
    if not text:
        return []
    rows = []
    for item in text.split(","):
        try:
            profile, batches = item.split("=", 1)
            batches = int(batches)
        except ValueError as e:
            raise LabError("--phases 형식은 baseline=10,fraud=5입니다.") from e
        if profile not in PROFILES or batches <= 0:
            raise LabError("--phases에는 유효한 profile과 양의 batch 수가 필요합니다.")
        rows.append((batches, profile))
    return rows


def choose_kind(kind: str, mix: list[tuple[str, int]], rng: random.Random, batch: int) -> str:
    if kind != "all":
        return kind
    if not mix:
        mix = [(name, 1) for name in KINDS]
    names, weights = zip(*mix)
    return rng.choices(names, weights=weights, k=1)[0]


def choose_profile(phases: list[tuple[int, str]], batch: int, default: str = "baseline") -> str:
    for count, profile in phases:
        if batch < count:
            return profile
        batch -= count
    return phases[-1][1] if phases else default


def topic_watermarks(bootstrap: str, topic: str, group: str | None = None) -> list[dict]:
    ck, _ = kafka_modules()
    ps = get_partitions(metadata(bootstrap, topic), topic)
    c = consumer(bootstrap, group or f"lab-inspect-{uuid.uuid4().hex[:8]}")
    try:
        tps = [ck.TopicPartition(topic, p["partition"]) for p in ps]
        commits = {p.partition: p.offset for p in c.committed(tps, timeout=10)} if group else {}
        rows = []
        for tp in tps:
            low, high = c.get_watermark_offsets(tp, timeout=8, cached=False)
            row = {"partition": tp.partition, "low": low, "high": high, "offset_span": high-low}
            if group:
                committed = commits[tp.partition]
                effective = committed if committed >= 0 else low
                row.update(committed=committed if committed >= 0 else None,
                           lag=max(0, high-effective), estimated=committed < 0)
            rows.append(row)
        return rows
    finally:
        c.close()


def offsets(args) -> None:
    rows = topic_watermarks(args.bootstrap, args.topic, args.group)
    result = {"topic": args.topic, "partitions": rows,
              "total_offset_span": sum(r["offset_span"] for r in rows)}
    if args.group:
        result.update(group=args.group, total_lag=sum(r["lag"] for r in rows),
                      contains_estimates=any(r["estimated"] for r in rows))
    emit(result)
    if args.expect_active is not None:
        active = sum(r["offset_span"] > 0 for r in rows)
        if active != args.expect_active:
            raise LabError(f"nonempty partitions expected={args.expect_active}, actual={active}")
    if args.expect_lag is not None:
        if not args.group:
            raise LabError("--expect-lag requires --group")
        total = sum(r["lag"] for r in rows)
        if total != args.expect_lag or any(r["estimated"] for r in rows):
            raise LabError(f"실제 committed offset의 lag expected={args.expect_lag}, actual={total}, rows={rows}")


def scan_records(bootstrap: str, topic: str, limit: int | None, timeout: int):
    """Read a finite low/high-watermark snapshot without joining or committing a group."""
    ck, _ = kafka_modules()
    rows = topic_watermarks(bootstrap, topic)
    c = consumer(bootstrap, f"lab-read-{uuid.uuid4().hex[:8]}")
    targets = {r["partition"]: r["high"] for r in rows if r["high"] > r["low"]}
    n, deadline = 0, time.monotonic() + timeout
    try:
        if not targets:
            return
        c.assign([ck.TopicPartition(topic, r["partition"], r["low"]) for r in rows if r["partition"] in targets])
        while targets:
            if time.monotonic() >= deadline:
                raise LabError(f"읽기 시간 초과: remaining partitions={targets}, records={n}")
            msg = c.poll(1)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == ck.KafkaError._PARTITION_EOF:
                    if msg.partition() in targets and msg.offset() >= targets[msg.partition()]:
                        targets.pop(msg.partition(), None)
                    continue
                raise ck.KafkaException(msg.error())
            end = targets.get(msg.partition())
            if end is None or msg.offset() >= end:
                continue
            yield msg
            n += 1
            if limit is not None and n >= limit:
                return
            if msg.offset() + 1 >= end:
                targets.pop(msg.partition(), None)
    finally:
        c.close()


def read_records(args) -> None:
    n = 0
    for msg in scan_records(args.bootstrap, args.topic, args.max, args.timeout):
        raw = msg.value()
        try:
            value = json.loads(raw) if raw is not None else None
        except (ValueError, TypeError):
            value = raw.decode(errors="replace")
        emit({"topic": msg.topic(), "partition": msg.partition(), "offset": msg.offset(),
              "key": msg.key().decode(errors="replace") if msg.key() is not None else None, "value": value})
        n += 1
    print(f"read={n}; inspection only; no group offset committed", file=sys.stderr)


def consume(args) -> None:
    if not 0 <= args.sleep_ms <= 1000 or not 1 <= args.duration <= 600:
        raise LabError("sleep-ms range=0..1000; duration range=1..600")
    ck, _ = kafka_modules()
    c, n, dirty = consumer(args.bootstrap, args.group), 0, False
    start = time.monotonic()
    last_data = start
    def assigned(_c, parts):
        print(f"ASSIGNED group={args.group} partitions={[p.partition for p in parts]}", file=sys.stderr, flush=True)
    def revoked(_c, parts):
        nonlocal dirty
        if dirty:
            _c.commit(asynchronous=False)
            dirty = False
        print(f"REVOKED group={args.group} partitions={[p.partition for p in parts]}", file=sys.stderr, flush=True)
    try:
        c.subscribe([args.topic], on_assign=assigned, on_revoke=revoked)
        while time.monotonic() - start < args.duration:
            if args.count and n >= args.count:
                break
            msg = c.poll(.5)
            if msg is None or (msg.error() and msg.error().code() == ck.KafkaError._PARTITION_EOF):
                if args.idle_timeout and time.monotonic() - last_data >= args.idle_timeout and n > 0:
                    break
                continue
            if msg.error():
                raise ck.KafkaException(msg.error())
            # This sleep stands in for application processing, not broker slowness.
            if args.sleep_ms:
                time.sleep(args.sleep_ms / 1000)
            last_data = time.monotonic()
            c.store_offsets(message=msg)  # only processed messages are eligible for commit
            dirty = True
            n += 1
            if args.print_records:
                emit({"partition": msg.partition(), "offset": msg.offset(), "value": msg.value().decode(errors="replace")})
            if n % args.commit_every == 0:
                c.commit(asynchronous=False)
                dirty = False
                emit({"group": args.group, "processed": n, "elapsed_seconds": round(time.monotonic()-start, 2)})
    finally:
        try:
            if dirty:
                c.commit(asynchronous=False)
        finally:
            c.close()
    emit({"group": args.group, "processed": n, "elapsed_seconds": round(time.monotonic()-start, 3)})
    if args.count and n < args.count:
        raise LabError(f"consumer processed {n}, expected {args.count}; duration 상한 확인")


def probe_failure(args) -> None:
    checked_topic(args.topic)
    ck, ka = kafka_modules()
    ps = get_partitions(metadata(args.bootstrap, args.topic), args.topic)
    if len(ps) != 1 or ps[0]["leader"] < 0:
        raise LabError("probe requires one partition with a live leader")
    resource = ka.ConfigResource(ka.ResourceType.TOPIC, args.topic)
    cfg = admin_client(args.bootstrap).describe_configs([resource], request_timeout=8)[resource].result(10)
    if args.kind == "isr":
        if len(ps[0]["isr"]) != 1 or cfg["min.insync.replicas"].value != "2":
            raise LabError("isr probe precondition: ISR=1 and min.insync.replicas=2")
        payload, expected = b"test-min-isr", {"NOT_ENOUGH_REPLICAS", "NOT_ENOUGH_REPLICAS_AFTER_APPEND"}
    else:
        if int(cfg["max.message.bytes"].value) != 1024:
            raise LabError("oversize probe precondition: max.message.bytes=1024")
        payload, expected = b"x" * 8192, {"MSG_SIZE_TOO_LARGE"}
    p, result = make_producer(args.bootstrap, timeout_ms=10000, probe=True), []
    p.produce(args.topic, partition=0, value=payload, on_delivery=lambda err, msg: result.append(err))
    pending = p.flush(15)
    if pending or len(result) != 1:
        raise LabError("probe delivery callback missing; not a confirmed expected failure")
    err = result[0]
    name = err.name() if err is not None else "SUCCESS"
    emit({"probe": args.kind, "result": name, "detail": str(err), "expected": sorted(expected)})
    if name not in expected:
        raise LabError("다른 오류/성공을 예상한 장애로 오인하지 않습니다: " + name)


def retention_wait(args) -> None:
    deadline = time.monotonic() + args.timeout
    while True:
        rows = topic_watermarks(args.bootstrap, args.topic)
        if sum(r["low"] for r in rows) > 0:
            emit({"retention_observed": True, "partitions": rows})
            return
        if time.monotonic() >= deadline:
            raise LabError("retention 관찰 시간 초과: log-start-offset이 아직 전진하지 않음")
        time.sleep(3)


def smoke(args) -> None:
    topic = f"lab.smoke.{uuid.uuid4().hex[:10]}"
    rid = uuid.uuid4().hex[:12]
    create_topic(args.bootstrap, topic, 3)
    seed = argparse.Namespace(bootstrap=args.bootstrap, topic=topic, kind="payments", count=120,
        mib=None, duration=None, rate=0, payload_bytes=32, hot_key=False, seed=7, run_id=rid)
    result = publish(seed)
    seen = []
    for msg in scan_records(args.bootstrap, topic, None, 45):
        event = json.loads(msg.value())
        if event["run_id"] != rid:
            raise LabError("unexpected data in fresh smoke topic")
        seen.append(event["sequence"])
    if sorted(seen) != list(range(120)) or result["delivered"] != 120:
        raise LabError(f"smoke mismatch: read={len(seen)}, unique={len(set(seen))}")
    emit({"status": "PASS", "test": "live-produce-consume", "topic": topic,
          "delivered": 120, "read": len(seen), "unique_sequences": len(set(seen))})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", default=os.getenv("BOOTSTRAP_SERVERS", "kafka1:9092,kafka2:9092,kafka3:9092"))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init"); p.set_defaults(func=init_topics)
    p = sub.add_parser("create")
    p.add_argument("--topic", required=True); p.add_argument("--partitions", type=positive, default=3)
    p.add_argument("--config", action="append", default=[]); p.add_argument("--assignment")
    p.add_argument("--exist-ok", action="store_true"); p.add_argument("--timeout", type=positive, default=15)
    p.set_defaults(func=topic_create)
    p = sub.add_parser("wait")
    p.add_argument("--topic"); p.add_argument("--brokers", type=int, default=3)
    p.add_argument("--isr", type=int); p.add_argument("--leader-not", type=int)
    p.add_argument("--controller-not", type=int); p.add_argument("--timeout", type=positive, default=120)
    p.set_defaults(func=wait_state)
    p = sub.add_parser("zk-status")
    p.add_argument("--servers", default=os.getenv("ZK_SERVERS", "zk1:2181,zk2:2181,zk3:2181"))
    p.add_argument("--serving", type=int); p.add_argument("--timeout", type=positive, default=60)
    p.set_defaults(func=zk_status)
    p = sub.add_parser("status"); p.add_argument("--topic")
    p.set_defaults(func=lambda a: emit(metadata(a.bootstrap, a.topic)))
    p = sub.add_parser("leader"); p.add_argument("--topic", required=True)
    p.add_argument("--partition", type=int, default=0)
    def leader(a):
        ps = get_partitions(metadata(a.bootstrap, a.topic), a.topic)
        print(next(p["leader"] for p in ps if p["partition"] == a.partition))
    p.set_defaults(func=leader)
    p = sub.add_parser("controller")
    p.set_defaults(func=lambda a: print(metadata(a.bootstrap)["controller"]))
    p = sub.add_parser("seed")
    p.add_argument("--kind", choices=KINDS, default="payments"); p.add_argument("--topic")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--count", type=positive); group.add_argument("--mib", type=float)
    group.add_argument("--duration", type=positive)
    p.add_argument("--rate", type=float, default=1000)
    p.add_argument("--payload-bytes", type=int, default=256); p.add_argument("--seed", type=int, default=42)
    p.add_argument("--hot-key", action="store_true"); p.add_argument("--run-id")
    p.add_argument("--profile", choices=PROFILES, default="baseline")
    p.set_defaults(func=publish)
    p = sub.add_parser("simulate")
    p.add_argument("--kind", choices=(*KINDS, "all"), default="payments"); p.add_argument("--topic")
    p.add_argument("--batch-count", type=positive, default=100)
    p.add_argument("--batches", type=positive); p.add_argument("--duration", type=positive)
    p.add_argument("--interval", type=float, default=5); p.add_argument("--rate", type=float, default=1000)
    p.add_argument("--payload-bytes", type=int, default=256); p.add_argument("--seed", type=int, default=42)
    p.add_argument("--hot-key", action="store_true"); p.add_argument("--run-id")
    p.add_argument("--profile", choices=PROFILES, default="baseline")
    p.add_argument("--mix", help="all 모드의 weighted kind 목록, 예: payments=60,access=30,metrics=10")
    p.add_argument("--phases", help="profile별 batch 수, 예: baseline=10,fraud=5,outage=5")
    p.add_argument("--jitter", type=float, default=0, help="interval ± percentage")
    p.add_argument("--burst-every", type=positive, default=0)
    p.add_argument("--burst-multiplier", type=positive, default=1)
    p.set_defaults(func=simulate)
    p = sub.add_parser("read")
    p.add_argument("--topic", required=True); p.add_argument("--max", type=positive, default=10)
    p.add_argument("--timeout", type=positive, default=30); p.set_defaults(func=read_records)
    p = sub.add_parser("consume")
    p.add_argument("--topic", required=True); p.add_argument("--group", required=True)
    p.add_argument("--duration", type=positive, default=60); p.add_argument("--count", type=positive)
    p.add_argument("--sleep-ms", type=int, default=0); p.add_argument("--commit-every", type=positive, default=100)
    p.add_argument("--idle-timeout", type=positive); p.add_argument("--print-records", action="store_true")
    p.set_defaults(func=consume)
    p = sub.add_parser("offsets")
    p.add_argument("--topic", required=True); p.add_argument("--group")
    p.add_argument("--expect-active", type=int); p.add_argument("--expect-lag", type=int)
    p.set_defaults(func=offsets)
    p = sub.add_parser("probe-failure")
    p.add_argument("--topic", required=True); p.add_argument("--kind", choices=["isr", "oversize"], required=True)
    p.set_defaults(func=probe_failure)
    p = sub.add_parser("retention-wait")
    p.add_argument("--topic", required=True); p.add_argument("--timeout", type=positive, default=150)
    p.set_defaults(func=retention_wait)
    p = sub.add_parser("smoke"); p.set_defaults(func=smoke)
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
