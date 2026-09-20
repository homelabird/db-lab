#!/usr/bin/env python3
"""Persist a canonical Kafka UUID; never silently repair or replace an existing ID."""
import base64
import os
from pathlib import Path
import re
import sys
import uuid


def validate(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{22}', value):
        raise ValueError('KRaft ID must be a canonical 22-character URL-safe Base64 UUID; recover the original ID from meta.properties, do not delete data')
    raw = base64.b64decode(value + '==', altchars=b'-_', validate=True)
    if len(raw) != 16 or base64.urlsafe_b64encode(raw).decode().rstrip('=') != value:
        raise ValueError('KRaft ID is not a canonical 128-bit value')
    return value


def ensure(path, supplied=''):
    if supplied:
        validate(supplied)
    if path.exists():
        saved = validate(path.read_text().strip())
        if supplied and supplied != saved:
            raise ValueError('Supplied cluster ID conflicts with the saved ID; refusing to overwrite')
        return saved
    value = supplied
    if not value:
        while not value or value.startswith('-'):
            value = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('=')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return ensure(path, supplied)
    with os.fdopen(fd, 'w') as stream:
        stream.write(validate(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return value


if __name__ == '__main__':
    try:
        print(ensure(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else ''))
    except (ValueError, OSError) as exc:
        sys.exit('ERROR: ' + str(exc))
