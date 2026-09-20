"""POSIX execution support: bounded private logs, no shell, no retry/reset.

Only build_plan output is accepted by the Ansible entrypoint. Timeouts send TERM
to the child process group, allow a cleanup grace period, then KILL. Existing
fault markers are never removed; helper/engine recovery is still the operator's
responsibility after a lost connection or forced termination.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid

from ansible.module_utils.db_lab_policy import DIRS, PolicyError, regular_path, require, run_id

LOG_LIMIT = 2 * 1024 * 1024  # per stream per command; excess is drained, not retained


def private_directory(path):
    path = Path(path)
    for parent in reversed((path, *path.parents)):
        require(not parent.is_symlink(), 'symlink_report_directory_refused')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.is_dir() and path.stat().st_uid == os.geteuid(), 'report_directory_owner_mismatch')
    # Dedicated automation report subtree only; existing project dirs are not chmod'ed.
    path.chmod(0o700)
    return path


def new_file(path, mode='wb'):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, mode)


def write_json(path, value):
    with new_file(path, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def environment_fingerprint(root, target):
    targets = [k for k in DIRS if k != 'mvp'] if target == 'all' else [target]
    result = {}
    for t in targets:
        if t not in DIRS:
            continue
        p = root / DIRS[t] / '.env'
        require(not p.is_symlink(), 'symlink_env_refused')
        if p.exists():
            regular_path(p)
            require(p.stat().st_size <= 1024 * 1024, 'env_size_exceeded')
            result[t] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            result[t] = None
    return result


def kill_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def execute(argv, root, environment, directory, index, timeout, *, grace=15.0, limit=LOG_LIMIT):
    """Run once. A timeout is never success, even if TERM cleanup exits zero."""
    started = time.monotonic()
    filenames = {'stdout': directory / ('%02d.stdout.log' % index),
                 'stderr': directory / ('%02d.stderr.log' % index)}
    streams = {k: new_file(v) for k, v in filenames.items()}
    counts = {'stdout': 0, 'stderr': 0}
    process = None
    sel = selectors.DefaultSelector()
    timed_out, interrupted = False, False
    term_at = None
    previous = {}
    def on_signal(signum, frame):
        nonlocal interrupted
        interrupted = True
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, on_signal)
        env = dict(os.environ, **environment, PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.Popen(argv, cwd=root, env=env, shell=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True, umask=0o077)
        sel.register(process.stdout, selectors.EVENT_READ, 'stdout')
        sel.register(process.stderr, selectors.EVENT_READ, 'stderr')
        while sel.get_map() or process.poll() is None:
            now = time.monotonic()
            if now - started >= timeout:
                timed_out = True
            if (timed_out or interrupted) and term_at is None:
                term_at = now
                kill_group(process, signal.SIGTERM)
            if term_at is not None and now - term_at >= grace:
                kill_group(process, signal.SIGKILL)
            if term_at is not None and now - term_at >= grace + 2:
                # An escaped descendant might still hold a pipe. Do not block forever.
                break
            for key, _ in sel.select(timeout=.1):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                field = key.data
                room = max(0, limit - counts[field])
                streams[field].write(chunk[:room])
                counts[field] += len(chunk)
        try:
            native_rc = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            kill_group(process, signal.SIGKILL)
            native_rc = process.wait(timeout=2)
        return {'returncode': 130 if interrupted else (124 if timed_out else native_rc),
                'native_returncode': native_rc, 'timed_out': timed_out,
                'interrupted': interrupted, 'seconds': round(time.monotonic() - started, 3),
                'stdout_bytes': counts['stdout'], 'stderr_bytes': counts['stderr'],
                'output_truncated': any(n > limit for n in counts.values())}
    finally:
        if process is not None:
            if process.poll() is None:
                kill_group(process, signal.SIGKILL)
                process.wait(timeout=3)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        sel.close()
        for stream in streams.values():
            stream.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def share_paths(root, name):
    name = run_id(name, 'accept')
    paths = [root / 'mvp-lab/reports/share' / name / n for n in ('summary.json', 'report.md', 'junit.xml')]
    for path in paths:
        regular_path(path)
        require(path.stat().st_size <= 2 * 1024 * 1024, 'export_size_exceeded')
    return [str(p) for p in paths]


def execute_plan(plan, timeout=3600, show_output=False):
    require(type(timeout) is int and 30 <= timeout <= 43200, 'timeout_out_of_range')
    require(type(show_output) is bool, 'show_output_requires_boolean')
    root = Path(plan['project_root'])
    state = root / '.state'
    require(not state.is_symlink(), 'symlink_state_refused')
    state.mkdir(mode=0o700, exist_ok=True)
    lock_path = state / 'ansible-control.lock'
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PolicyError('another_ansible_operation_is_active') from None
        parent = private_directory(root / 'reports/ansible')
        directory = private_directory(parent / ('control-' + uuid.uuid4().hex[:16]))
        result = {'execution': 'controller_invoked', 'category': plan['category'],
                  'changed': False, 'rc': 0, 'failed': False, 'children': [],
                  'log_directory': str(directory), 'database_health_certified': False}
        before = environment_fingerprint(root, plan['target']) if plan['action'] == 'init' else None
        write_json(directory / 'plan.json', plan)
        try:
            for i, argv in enumerate(plan['commands']):
                # No automatic retries, not even for read-only commands.
                if plan['category'] in ('change', 'fault', 'destroy'):
                    result['changed'] = True  # partial failure may also have changed things
                child = execute(argv, root, plan['environment'], directory, i, timeout)
                result['children'].append(child)
                result['rc'] = child['returncode']
                if result['rc'] != 0:
                    result['failed'] = True
                    break
            if before is not None and result['rc'] == 0:
                result['changed'] = before != environment_fingerprint(root, plan['target'])
            if plan['target'] == 'mvp' and plan['action'] == 'verify' and plan['verb'] == 'export' and result['rc'] == 0:
                result['share_files'] = share_paths(root, plan['name'])
        except (OSError, ValueError, RuntimeError) as exc:
            result.update(failed=True, rc=1, error_code=str(exc) if isinstance(exc, PolicyError) else 'controller_execution_error')
        write_json(directory / 'result.json', result)
        if show_output:
            # Operator opt-in: may contain synthetic data or credentials from native labs.
            result['output'] = [{'stdout': (directory / ('%02d.stdout.log' % i)).read_text(errors='replace'),
                                 'stderr': (directory / ('%02d.stderr.log' % i)).read_text(errors='replace')}
                                for i in range(len(result['children']))]
        return result
    finally:
        os.close(lock_fd)
