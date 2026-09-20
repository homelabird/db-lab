"""Bounded, non-secret experimental context and strict structured evidence utilities.

Hashes are change detectors, not signatures or an independent engine attestation.
No runtime/driver fallback is provided here.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import platform
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('nonfinite_json_number')
        return value
    def invalid(_):
        raise ValueError('nonfinite_json_number')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_float=number, parse_constant=invalid)
    except RecursionError as exc:
        raise ValueError('json_nesting_exceeded') from exc


def capture_context(root: Path, compose, states: list[dict]) -> dict:
    """Read files/engine pin and already collected inspect state. Never prints .env.

    Container IDs deliberately do not participate: recreate preserves image/volume.
    All settings (including credentials) participate ONLY in one digest.
    """
    names = {'mariadb', 'kafka', 'elasticsearch', 'redis', 'api', 'worker'}
    if len(states) != 6 or {s.get('service') for s in states} != names:
        raise RuntimeError('comparison_topology_incomplete')
    files = {}
    paths = [root / name for name in ('Containerfile', '.dockerignore', 'requirements.txt', 'compose.yaml')]
    for directory in ('mvp_app', 'tools'):
        paths += [p for p in (root / directory).rglob('*') if p.suffix in {'.py', '.html'} and '__pycache__' not in p.parts]
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file() or root.resolve() not in path.resolve().parents or path.stat().st_size > 2*1024**2:
            raise RuntimeError('comparison_source_not_bounded')
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = compose.guard_engine()
    if identity.get('local_docker') is not True:
        raise RuntimeError('comparison_requires_local_docker')
    topology = [{k: state[k] for k in ('service', 'image_id', 'volumes')} for state in sorted(states, key=lambda s: s['service'])]
    return {'schema': 1, 'engine_sha256': digest(identity), 'source_sha256': digest(files),
            'config_sha256': digest(compose.config), 'topology_sha256': digest(topology),
            'client_python': platform.python_version(), 'client_platform': platform.system(),
            'source_files': files,
            'limits': 'No free-RAM/CPU/disk/cache-state attestation. Source/identity hashes are not signatures.'}


def comparable_context(value: dict) -> dict:
    keys = ('engine_sha256', 'source_sha256', 'config_sha256', 'topology_sha256', 'client_python', 'client_platform')
    return {key: value[key] for key in keys}
