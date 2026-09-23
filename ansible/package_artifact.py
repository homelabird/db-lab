#!/usr/bin/env python3
"""Create a deterministic source-only DB Lab release archive."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

EXCLUDED_DIRS = {'.git', '.state', '.venv', 'node_modules', '__pycache__', '.pytest_cache',
                 '.mypy_cache', '.ruff_cache', '.quality', 'reports', 'data', 'volumes',
                 'artifacts', 'certs', 'htmlcov', '.venv-ansible', '.venv-quality',
                 'snapshots', 'backups', 'dumps', 'secrets', '.ssh'}


def excluded(path: Path) -> bool:
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return True
    name = path.name
    if name == '.env' or (name.startswith('.env.') and name != '.env.example'):
        return True
    if name.endswith(('.pem', '.key')) or name.startswith(('id_rsa', 'id_ed25519')):
        return True
    if name.endswith(('.rdb', '.ibd', '.sqlite', '.sqlite3')) or name.endswith('.log'):
        return True
    if name.startswith('.coverage'):
        return True
    return False


def create(source: Path, output_dir: Path) -> dict:
    source = source.expanduser().absolute()
    if source.is_symlink() or not source.is_dir() or source.resolve() != source:
        raise ValueError('source must be an existing canonical directory')
    for required in ('all.sh', 'scripts/control.py'):
        if not (source / required).is_file() or (source / required).is_symlink():
            raise ValueError('source is missing required project entrypoints')

    try:
        top = subprocess.run(['git', '-C', str(source), 'rev-parse', '--show-toplevel'],
                             check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        listed = subprocess.run(['git', '-C', str(source), 'ls-files', '--cached', '--others',
                                 '--exclude-standard', '-z'],
                                check=True, capture_output=True, timeout=30).stdout.split(b'\0')
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('source must be a readable Git worktree') from exc
    if Path(top).resolve() != source:
        raise ValueError('source must be the Git worktree root')

    members = []
    for raw in listed:
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or '..' in relative.parts or excluded(relative):
            continue
        path = source / relative
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent != source.parent):
            raise ValueError('source contains a symlink path')
        if not path.is_file():
            raise ValueError('source contains a non-regular file')
        members.append(path)
    members.sort(key=lambda path: path.relative_to(source).as_posix())
    if not members:
        raise ValueError('source archive is empty')

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError('unsafe output directory')
    fd, temporary = tempfile.mkstemp(prefix='.db-lab-release-', suffix='.tar.gz', dir=output_dir)
    os.close(fd)
    try:
        with open(temporary, 'wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0, filename='') as compressed:
                with tarfile.open(fileobj=compressed, mode='w|') as archive:
                    for path in members:
                        relative = path.relative_to(source).as_posix()
                        info = archive.gettarinfo(str(path), arcname=relative)
                        info.uid = info.gid = 0
                        info.uname = info.gname = ''
                        info.mtime = 0
                        info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                        with path.open('rb') as stream:
                            archive.addfile(info, stream)
            raw.flush()
            os.fsync(raw.fileno())
        digest = hashlib.sha256(Path(temporary).read_bytes()).hexdigest()
        target = output_dir / (digest + '.tar.gz')
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('existing artifact hash mismatch')
            os.unlink(temporary)
        else:
            os.replace(temporary, target)
        return {'schema_version': 1, 'release_id': digest, 'sha256': digest,
                'artifact': str(target), 'files': len(members)}
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output_dir', type=Path)
    args = parser.parse_args()
    print(json.dumps(create(args.source, args.output_dir), sort_keys=True))


if __name__ == '__main__':
    main()
