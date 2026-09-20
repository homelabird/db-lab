"""Hold ONLY a run-owned synthetic order row. No update/delete/global setting changes."""
import argparse
import re
import signal
import threading
from .adapters import Settings, SQL
from .core import identifier, emit
from .observability import safe_error


def hold(repo, order_id, run_id, seconds, stop=None):
    identifier(order_id)
    if not re.fullmatch(r"sim-[a-f0-9]{12}", run_id) or not 0 <= seconds <= 30:
        raise ValueError("Invalid bounded synthetic-row lock request")
    stop = stop or threading.Event()
    conn = repo.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION innodb_lock_wait_timeout=2")
            cur.execute("SELECT item FROM orders WHERE id=%s", (order_id,))
            row = cur.fetchone()
            if not row or not row["item"].startswith(run_id + "-"):
                raise ValueError("Refusing an unrelated order before requesting its row lock")
            cur.execute("SELECT item FROM orders WHERE id=%s FOR UPDATE", (order_id,))
            row = cur.fetchone()
            if not row or not row["item"].startswith(run_id + "-"):
                raise ValueError("Refusing to lock an order not owned by this simulation run")
            emit("row_lock_acquired", order_id=order_id, study_run=run_id, hold_seconds=seconds)
            stop.wait(seconds)
    finally:
        try:
            conn.rollback()  # no changes, release transaction locks
        finally:
            conn.close()
    emit("row_lock_released", order_id=order_id, study_run=run_id)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--order-id", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--seconds", type=int, default=0)
    a = p.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        hold(SQL(Settings.load()), a.order_id, a.run_id, a.seconds, stop)
        return 0
    except Exception as exc:
        emit("row_lock_failed", **safe_error(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
