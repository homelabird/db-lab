"""Pure helpers, independently testable without Redis or third-party packages."""
from __future__ import annotations
import json
import re
from collections import Counter
from pathlib import Path


def bounded_int(value: str, minimum: int, maximum: int) -> int:
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"Expected {minimum}..{maximum}, received {number}")
    return number


def endpoint_list(raw: str) -> list[tuple[str, int]]:
    items = []
    for entry in raw.split(','):
        host, port = entry.strip().rsplit(':', 1)
        if not host or not 1 <= int(port) <= 65535:
            raise ValueError(f'Invalid endpoint: {entry}')
        items.append((host, int(port)))
    if len(set(items)) != len(items):
        raise ValueError('Duplicate endpoints')
    return items


def safe_run_id(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError('Run ID may contain letters, numbers, underscores and hyphens only.')
    return value


def payload_for(run_id: str, seq: int, size: int) -> str:
    prefix = f'{run_id}:{seq}:'
    return prefix + 'x' * max(0, size - len(prefix))


def classify_error(name: str, text: str) -> str:
    upper = text.upper()
    if 'OOM' in upper or name == 'OutOfMemoryError':
        return 'memory-limit'
    if 'MISCONF' in upper:
        return 'persistence-error'
    if 'NOAUTH' in upper or 'WRONGPASS' in upper or name == 'AuthenticationError':
        return 'authentication'
    if 'READONLY' in upper or name == 'ReadOnlyError':
        return 'wrong-role'
    if name == 'MasterNotFoundError':
        return 'master-not-found'
    if name == 'TimeoutError':
        return 'timeout'
    return 'connection-or-server-error'


def verification_bucket(row: dict, observed: str | None) -> str:
    matches = observed == row['value']
    if row.get('write_ack'):
        return 'acknowledged_present' if matches else 'acknowledged_missing_or_changed'
    if row.get('outcome') == 'not-sent':
        return 'not_sent_but_present' if observed is not None else 'not_sent_absent'
    if row.get('outcome') == 'rejected':
        return 'rejected_but_present' if observed is not None else 'rejected_absent'
    return 'uncertain_but_present' if matches else 'uncertain_absent_or_changed'


def read_run(path: Path):
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'{path.name}:{number}: incomplete or invalid JSON; finish the workload first') from exc
            if row.get('event') == 'request':
                yield row
