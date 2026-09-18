"""Pure, streaming-friendly synthetic data. No Kafka or third-party dependency."""
from __future__ import annotations
import json
import random
import string
from datetime import datetime, timedelta, timezone

KINDS = ("payments", "access", "metrics")
TOPICS = {kind: f"lab.{kind}" for kind in KINDS}


def make_event(kind: str, seq: int, rng: random.Random, run_id: str,
               base_time: datetime, payload_bytes: int = 256,
               hot_key: bool = False) -> tuple[bytes, bytes]:
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    if seq < 0 or payload_bytes < 0:
        raise ValueError("seq and payload_bytes must be nonnegative")
    event = {
        "schema_version": 1, "event_id": f"{run_id}:{seq}", "run_id": run_id,
        "sequence": seq, "kind": kind,
        "timestamp": (base_time + timedelta(milliseconds=seq)).isoformat(),
        "synthetic": True,
    }
    if kind == "payments":
        account = f"test-account-{rng.randrange(1, 1001):04d}"
        key = account
        event.update(account_id=account, merchant_id=f"test-shop-{rng.randrange(1, 101):03d}",
                     amount_krw=rng.randrange(1, 5001) * 100,
                     channel=rng.choice(["app", "web", "atm"]),
                     result=rng.choices(["approved", "declined", "review"], [90, 7, 3])[0],
                     risk_score=rng.randrange(101))
    elif kind == "access":
        key = f"test-user-{rng.randrange(1, 2001):04d}"
        event.update(user_id=key, client_ip=f"192.0.2.{rng.randrange(1, 255)}",
                     path=rng.choice(["/login", "/balance", "/payment", "/logout"]),
                     method=rng.choice(["GET", "POST"]),
                     status=rng.choices([200, 401, 404, 500], [90, 4, 4, 2])[0],
                     latency_ms=rng.randrange(1, 2001))
    else:
        key = f"test-device-{rng.randrange(1, 201):03d}"
        event.update(device_id=key, region=rng.choice(["lab-a", "lab-b", "lab-c"]),
                     temperature_c=round(rng.uniform(10, 50), 2),
                     cpu_percent=round(rng.uniform(0, 100), 2),
                     memory_percent=round(rng.uniform(5, 95), 2))
    # Random ASCII padding avoids the misleading sizes of repeated "x" compression.
    event["payload"] = "".join(rng.choices(string.ascii_letters + string.digits, k=payload_bytes))
    if hot_key:
        key = "hot-key-only"
    return key.encode(), json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
