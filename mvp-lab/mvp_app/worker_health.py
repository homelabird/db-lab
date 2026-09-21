"""Read-only worker healthcheck: both loops must make recent successful progress.

The receipt is private, credential-free and tied to the Linux process start time.
This is relay/subscribed-consumer progress, NOT end-to-end delivery or durability.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
import uuid

PATH = Path('/tmp/db-lab-worker-health.json')
MAX_AGE = 30.0
LOOPS = ('relay', 'consumer')


def process_start(pid: int) -> str:
    # /proc field 2 may include spaces or parentheses. Field 22 is starttime.
    value = Path(f'/proc/{pid}/stat').read_text()
    fields = value[value.rfind(')') + 2:].split()
    if fields[0] == 'Z':
        raise ValueError('worker_process_is_zombie')
    return fields[19]


class WorkerHealth:
    def __init__(self, path: Path = PATH, *, clock=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()
        self.last_write = float('-inf')
        self.data = {'schema': 1, 'pid': os.getpid(), 'process_start': process_start(os.getpid()),
                     'loops': {name: {'ok': False, 'last_success': None} for name in LOOPS}}
        self._save()

    def _save(self):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('worker_receipt_symlink_refused')
        tmp = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.data, stream, allow_nan=False)
            os.replace(tmp, self.path)
            self.last_write = self.clock()
        finally:
            tmp.unlink(missing_ok=True)

    def mark(self, name: str, ok: bool):
        if name not in LOOPS or type(ok) is not bool:
            raise ValueError('invalid_worker_loop_status')
        with self.lock:
            row = self.data['loops'][name]
            changed = row['ok'] != ok
            row['ok'] = ok
            now = self.clock()
            if ok:
                row['last_success'] = now
            # Status transitions are immediate; busy loops write at most once/sec.
            if changed or now - self.last_write >= 1:
                self._save()

    def close(self):
        with self.lock:
            for row in self.data['loops'].values():
                row['ok'] = False
            self._save()


def is_ready(path: Path = PATH, *, now: float | None = None) -> bool:
    """Fail closed on missing, stale, malformed or foreign-process receipts."""
    try:
        path = Path(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_uid != os.getuid():
                return False
            data = json.loads(stream.read(4097))
        if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1:
            return False
        pid = data.get('pid')
        if type(pid) is not int or pid < 1 or data.get('process_start') != process_start(pid):
            return False
        loops = data.get('loops')
        if not isinstance(loops, dict) or set(loops) != set(LOOPS):
            return False
        now = time.monotonic() if now is None else now
        for row in loops.values():
            if not isinstance(row, dict) or row.get('ok') is not True:
                return False
            last = row.get('last_success')
            if type(last) not in (int, float) or not math.isfinite(last) or not 0 <= now - last <= MAX_AGE:
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return False


if __name__ == '__main__':
    raise SystemExit(0 if is_ready() else 1)
