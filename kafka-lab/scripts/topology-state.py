#!/usr/bin/env python3
"""Keep subsequent Kafka commands on the topology selected at startup.

Only configuration and controller state are written. No container or volume
operations are performed here. A different topology needs a separate lab.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def check(path: Path, wanted: dict) -> None:
    if path.exists():
        pinned = json.loads(path.read_text(encoding='utf-8'))
        if pinned != wanted:
            raise ValueError('Existing Kafka topology is pinned. Restore matching LAB_NAME, KAFKA_MODE and NODES; '
                             'use a separate lab directory/name for a different topology. '
                             'No containers, volumes or configuration were changed.')


def run(root: Path, action: str, name: str, mode: str, nodes: int) -> None:
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,40}', name) or mode not in ('zk', 'kraft'):
        raise ValueError('Invalid Kafka lab name or mode')
    if not 1 <= nodes <= 100 or (mode == 'zk' and nodes % 2 == 0):
        raise ValueError('Invalid Kafka node count')
    state = root / '.state'
    if state.is_symlink():
        raise ValueError('Symlink state directory refused')
    state.mkdir(mode=0o700, exist_ok=True)
    path, env, lock = state / 'active-topology.json', root / '.env', state / 'topology.lock'
    if any(p.is_symlink() for p in (path, env, lock)):
        raise ValueError('Symlink configuration/state file refused')
    wanted = {'schema': 1, 'lab_name': name, 'mode': mode, 'nodes': nodes}
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        check(path, wanted)
        if action == 'save':
            lines = env.read_text(encoding='utf-8').splitlines()
            for key, value in (('NODES', nodes), ('KAFKA_MODE', mode), ('LAB_NAME', name)):
                indices = [i for i, line in enumerate(lines) if line.startswith(key + '=')]
                if len(indices) > 1:
                    raise ValueError(f'Duplicate {key} entries in .env')
                if indices:
                    lines[indices[0]] = f'{key}={value}'
                else:
                    lines.append(f'{key}={value}')
            # Keep a recoverable topology pin even on interruption between writes.
            atomic_write(path, json.dumps(wanted, sort_keys=True, indent=2) + '\n')
            atomic_write(env, '\n'.join(lines) + '\n', env.stat().st_mode & 0o777)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'save'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--lab-name', required=True)
    parser.add_argument('--mode', choices=('zk', 'kraft'), required=True)
    parser.add_argument('--nodes', type=int, required=True)
    args = parser.parse_args()
    try:
        run(args.root, args.action, args.lab_name, args.mode, args.nodes)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    main()
