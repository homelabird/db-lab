#!/usr/bin/env python3
"""Shared Python core for the Elasticsearch labs. Standard library only.

Used by the 7.x legacy lab (elasticsearch/) and the 9.x lab
(elasticsearch-9/). Each lab keeps a thin scripts/lablib.py whose CLI
dispatches here for the version-neutral commands: wait, guard, snapshot-repo.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

RETRYABLE = {429, 502, 503, 504}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


class APIError(RuntimeError):
    def __init__(self, status, method, path, detail):
        self.status, self.detail = status, detail
        super().__init__(f'HTTP {status} {method} {path}: {detail}')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


class ESClient:
    def __init__(self, url=None, timeout=120):
        self.url = (url or os.getenv('ES_URL', 'http://127.0.0.1:9200')).rstrip('/')
        if not self.url.startswith(('http://', 'https://')):
            raise ValueError('ES_URL must begin with http:// or https://')
        self.timeout = timeout

    def request(self, method, path, body=None, content_type='application/json', timeout=None):
        data = body if isinstance(body, bytes) else (compact(body) if body is not None else None)
        headers = {'Content-Type': content_type}
        if os.getenv('XPACK_SECURITY_ENABLED', 'false').lower() in ('true', '1', 'yes'):
            import base64
            user = os.getenv('ELASTIC_USERNAME', 'elastic')
            password = os.getenv('ELASTIC_PASSWORD', '')
            token = base64.b64encode(f'{user}:{password}'.encode('ascii')).decode('ascii')
            headers['Authorization'] = 'Basic ' + token
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
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

    def wait(self, min_nodes=1, seconds=300, require_yellow=False, require_green=False):
        deadline, last = time.monotonic() + seconds, 'No response'
        while time.monotonic() < deadline:
            try:
                info = self.request('GET', '/', timeout=5)
                health = self.request('GET', '/_cluster/health?timeout=3s', timeout=5)
                ready = (info.get('cluster_uuid') not in (None, '_na_')
                         and health.get('number_of_nodes', 0) >= min_nodes
                         and not health.get('timed_out', False)
                         and (not require_yellow or health.get('status') in ('yellow', 'green'))
                         and (not require_green or health.get('status') == 'green'))
                if ready:
                    return health
                last = json.dumps(health)
            except (APIError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last = str(exc)
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise RuntimeError(f'Cluster not ready after {seconds}s: {last}\n'
                           'Run ./lab.sh doctor, or check: compose logs es01..es05; '
                           'vm.max_map_count; free memory; cluster UUIDs; container-to-container networking.')

    def ensure_snapshot_repo(self, name, location, compress=True, min_nodes=5):
        """Create/update a shared filesystem snapshot repository and verify it."""
        self.assert_lab()
        try:
            current = self.request('GET', f'/_snapshot/{name}')
        except APIError as exc:
            if exc.status != 404:
                raise
            current = None
        body = {'type': 'fs', 'settings': {'location': location, 'compress': bool(compress)}}
        self.request('PUT', f'/_snapshot/{name}', body)
        verified = self.request('POST', f'/_snapshot/{name}/_verify')
        nodes = sorted((verified.get('nodes') or {}).keys())
        if len(nodes) < min_nodes:
            raise RuntimeError(f'Snapshot repository {name!r} verified on {len(nodes)} nodes; expected at least {min_nodes}.')
        return {'repository': name, 'location': location, 'verified_nodes': nodes,
                'previous': current}


def cli_main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Elasticsearch lab shared core CLI')
    parser.add_argument('command', choices=['wait', 'guard', 'snapshot-repo'])
    parser.add_argument('--nodes', type=int, default=1)
    parser.add_argument('--seconds', type=float, default=300)
    health = parser.add_mutually_exclusive_group()
    health.add_argument('--yellow', action='store_true')
    health.add_argument('--green', action='store_true')
    args = parser.parse_args(argv)
    try:
        client = ESClient()
        if args.command == 'guard':
            client.assert_lab()
        elif args.command == 'snapshot-repo':
            result = client.ensure_snapshot_repo(
                os.getenv('SNAPSHOT_REPO_NAME', 'lab-snapshots'),
                os.getenv('SNAPSHOT_PATH', '/usr/share/elasticsearch/snapshots'),
                min_nodes=5)
            print(f'[ready] snapshot repository {result["repository"]} verified on '
                  f'{len(result["verified_nodes"])} nodes: {" ".join(result["verified_nodes"])}')
        else:
            result = client.wait(args.nodes, args.seconds, args.yellow, args.green)
            print(f'[ready] nodes={result["number_of_nodes"]} status={result["status"]}')
    except (RuntimeError, ValueError, OSError, urllib.error.URLError) as exc:
        parser.exit(1, f'[error] {exc}\n')


if __name__ == '__main__':
    cli_main()
