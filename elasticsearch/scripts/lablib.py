#!/usr/bin/env python3
"""Python 3.9+ standard-library helpers; no pip dependency."""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDICES = ('lab-transactions-v1', 'lab-web-logs-v1', 'lab-audit-v1')
LAYOUT = {INDICES[0]: (12, 1, 50), INDICES[1]: (18, 1, 32), INDICES[2]: (8, 2, 18)}
RETRYABLE = {429, 502, 503, 504}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


class APIError(RuntimeError):
    def __init__(self, status, method, path, detail):
        self.status, self.detail = status, detail
        super().__init__(f'HTTP {status} {method} {path}: {detail}')


class ESClient:
    def __init__(self, url=None, timeout=120):
        self.url = (url or os.getenv('ES_URL', 'http://127.0.0.1:9200')).rstrip('/')
        if not self.url.startswith(('http://', 'https://')):
            raise ValueError('ES_URL must begin with http:// or https://')
        self.timeout = timeout

    def request(self, method, path, body=None, content_type='application/json', timeout=None):
        data = body if isinstance(body, bytes) else (compact(body) if body is not None else None)
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={'Content-Type': content_type})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw.decode('utf-8', errors='replace')
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')[:8000]
            raise APIError(exc.code, method, path, detail) from exc

    def assert_lab(self):
        info = self.request('GET', '/')
        expected = os.getenv('LAB_CLUSTER_NAME', 'cerebro-shard-lab')
        if info.get('cluster_name') != expected:
            raise RuntimeError(f'Wrong cluster: {info.get("cluster_name")!r}; expected {expected!r}. No writes allowed.')
        if info.get('cluster_uuid') in (None, '_na_'):
            raise RuntimeError('Cluster is not bootstrapped (cluster_uuid is _na_). Inspect discovery/master logs.')
        return info

    def wait(self, min_nodes=1, seconds=300, require_yellow=False):
        deadline, last = time.monotonic() + seconds, 'No response'
        while time.monotonic() < deadline:
            try:
                info = self.request('GET', '/', timeout=5)
                health = self.request('GET', '/_cluster/health?timeout=3s', timeout=5)
                ready = (info.get('cluster_uuid') not in (None, '_na_')
                         and health.get('number_of_nodes', 0) >= min_nodes
                         and not health.get('timed_out', False))
                if ready and (not require_yellow or health.get('status') in ('yellow', 'green')):
                    return health
                last = json.dumps(health)
            except (APIError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last = str(exc)
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise RuntimeError(f'Cluster not ready after {seconds}s: {last}\n'
                           'Check: compose logs es01 es02 es03; vm.max_map_count; free memory; cluster UUIDs.')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def bulk_send(client, records, retries=4, backoff=0.5):
    """records = (metadata-line, source-line) bytes; retry failed items, preserve deterministic IDs."""
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
    return json.loads((ROOT / 'queries/catalog.json').read_text(encoding='utf-8'))


def execute_example(client, entry):
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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['wait', 'guard'])
    parser.add_argument('--nodes', type=int, default=1)
    parser.add_argument('--seconds', type=float, default=300)
    parser.add_argument('--yellow', action='store_true')
    args = parser.parse_args()
    try:
        client = ESClient()
        if args.command == 'guard':
            client.assert_lab()
        else:
            health = client.wait(args.nodes, args.seconds, args.yellow)
            print(f'[ready] nodes={health["number_of_nodes"]} status={health["status"]}')
    except (RuntimeError, ValueError, OSError, urllib.error.URLError) as exc:
        parser.exit(1, f'[error] {exc}\n')
