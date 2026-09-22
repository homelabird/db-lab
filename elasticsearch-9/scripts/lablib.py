#!/usr/bin/env python3
"""Python 3.9+ helpers for the Elasticsearch 9 lab; no pip dependency.

The version-neutral Elasticsearch core (ESClient, wait/guard/snapshot-repository
CLI) lives in ../../lib/es-lab/lablib_core.py and is re-exported here so the
data and query scripts keep `from lablib import ...` working.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402

sys.path.insert(0, str(ROOT.parent / 'lib' / 'es-lab'))
from lablib_core import APIError, ESClient, RETRYABLE, compact, write_json  # noqa: E402,F401
from lablib_core import cli_main  # noqa: E402,F401

INDICES = ('lab-transactions-v1', 'lab-web-logs-v1', 'lab-audit-v1',
           'lab-commerce-v1', 'lab-observability-v1')
LAYOUT = {
    INDICES[0]: (12, 1, 40),
    INDICES[1]: (18, 1, 25),
    INDICES[2]: (8, 2, 15),
    INDICES[3]: (16, 2, 12),
    INDICES[4]: (24, 1, 8),
}


def bulk_send(client, records, retries=4, backoff=0.5):
    """records = (metadata-line, source-line) bytes; retry failed items, preserve deterministic IDs."""
    import json
    import time
    import urllib.error
    pending = list(records)
    for attempt in range(retries + 1):
        try:
            payload = b''.join(a + b'\n' + b + b'\n' for a, b in pending)
            result = client.request('POST', '/_bulk', payload, 'application/x-ndjson')
        except (APIError, urllib.error.URLError, TimeoutError, OSError) as exc:
            if isinstance(exc, APIError) and exc.status not in RETRYABLE:
                raise
            if attempt == retries:
                raise RuntimeError(f'Bulk request retries exhausted: {exc}') from exc
        else:
            items = result.get('items', [])
            if len(items) != len(pending):
                raise RuntimeError('Invalid bulk response: item count does not match the request')
            retry, fatal = [], []
            for record, item in zip(pending, items):
                operation = item.get('index', {})
                status = operation.get('status', 0)
                if 200 <= status < 300 and not operation.get('error'):
                    continue
                if status in RETRYABLE:
                    retry.append(record)
                else:
                    fatal.append(operation)
            if fatal:
                raise RuntimeError('Non-retryable bulk errors: ' + json.dumps(fatal[:3], ensure_ascii=False))
            if not retry:
                if result.get('errors'):
                    raise RuntimeError('Inconsistent bulk response: errors=true with no failed items')
                return len(records)
            pending = retry
            if attempt == retries:
                raise RuntimeError(f'Bulk item retries exhausted: {len(pending)} documents remain')
        time.sleep(min(10, backoff * (2 ** attempt)))
    raise AssertionError('Unreachable')


def load_catalog():
    import json
    return json.loads((ROOT / 'queries/catalog.json').read_text(encoding='utf-8'))


def execute_example(client, entry):
    import json
    body = None
    if entry.get('file'):
        body = json.loads((ROOT / 'queries' / entry['file']).read_text(encoding='utf-8'))
    result = client.request(entry['method'], entry['path'], body)
    if isinstance(result, dict):
        if result.get('timed_out') or result.get('_shards', {}).get('failed', 0):
            raise RuntimeError('Search timed out or returned partial shard results: ' + json.dumps(result)[:2000])
        if entry['path'].startswith('/lab-') and '/_validate/' in entry['path'] and not result.get('valid'):
            raise RuntimeError('Query validation failed: ' + json.dumps(result))
    return result


if __name__ == '__main__':
    cli_main()