#!/usr/bin/env python3
"""ES9-native feature scenarios, strictly es9-only.

Modern Elasticsearch 9 features exercised against the seeded lab data:
  1. data stream + index template + ILM policy (lab-api-logs)
  2. ES|QL queries over the lab indices and the data stream
  3. kNN vector search (dense_vector field)
  4. async search (submit/poll/delete)

These additions are additive: they do not modify the shared realistic generator
(lib/es-lab/datagen) used by both labs, nor the legacy 7.x catalog/drills.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lablib import APIError, ESClient, ROOT, write_json  # noqa: E402

DATA_STREAM = 'lab-api-logs'
TEMPLATE = 'lab-api-logs-template'
ILM = 'lab-ilm-30d'
KNN_INDEX = 'lab-knowledge'

ESQL_QUERIES = [
    ('01-txn-high-risk-by-country',
     'FROM lab-transactions-v1 | WHERE risk_score >= 800 | '
     'STATS cnt = COUNT(*) , avg_amt = AVG(amount) BY country | SORT cnt DESC | LIMIT 5'),
    ('02-web-5xx-by-service',
     'FROM lab-web-logs-v1 | WHERE status >= 500 | '
     'STATS cnt = COUNT(*) BY service | SORT cnt DESC | LIMIT 8'),
    ('03-api-datastream-5xx',
     'FROM lab-api-logs | WHERE status >= 500 | '
     'STATS cnt = COUNT(*) BY service | SORT cnt DESC | LIMIT 5'),
    ('04-observability-avg-by-service',
     'FROM lab-observability-v1 | STATS avg_metric = AVG(metric_value) BY service.name | '
     'SORT avg_metric DESC | LIMIT 5'),
    ('05-audit-actions',
     'FROM lab-audit-v1 | STATS cnt = COUNT(*) BY action | SORT cnt DESC | LIMIT 8'),
]

API_ROWS = [
    ('gateway', '/api/gateway', 'GET', 200, 42),
    ('gateway', '/api/gateway', 'GET', 200, 51),
    ('gateway', '/api/gateway', 'GET', 429, 180),
    ('search', '/api/search?q=es', 'GET', 200, 88),
    ('search', '/api/search?q=es', 'GET', 200, 120),
    ('search', '/api/search?q=vector', 'GET', 200, 95),
    ('search', '/api/search?q=esql', 'GET', 502, 2100),
    ('checkout', '/api/orders', 'POST', 201, 140),
    ('checkout', '/api/orders', 'POST', 400, 33),
    ('checkout', '/api/orders', 'POST', 503, 3200),
    ('gateway', '/api/auth', 'POST', 200, 35),
    ('gateway', '/api/auth', 'POST', 200, 39),
    ('search', '/api/facets', 'GET', 200, 210),
    ('search', '/api/facets', 'GET', 500, 60),
    ('checkout', '/api/payments', 'POST', 200, 300),
    ('checkout', '/api/payments', 'POST', 502, 2500),
    ('gateway', '/api/rate-limit', 'GET', 429, 15),
    ('gateway', '/api/health', 'GET', 200, 9),
    ('search', '/api/suggest', 'GET', 200, 340),
    ('checkout', '/api/cart', 'POST', 201, 75),
]
API_START = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
KNN_DOCS = [
    ('knn-1', 'data stream', 'ingest', 'index template, rollover and ILM manage streaming logs',
     [0.91, 0.42, 0.25, 0.18, 0.82, 0.36, 0.24, 0.77]),
    ('knn-2', 'ilm', 'ingest', 'hot warm delete lifecycle phases and rollover actions',
     [0.88, 0.39, 0.21, 0.17, 0.80, 0.33, 0.22, 0.72]),
    ('knn-3', 'esql', 'query', 'pipelines for HTTP script queries with stats and sort',
     [0.25, 0.82, 0.95, 0.63, 0.26, 0.84, 0.31, 0.18]),
    ('knn-4', 'query dsl', 'query', 'boolean bool must filter terms range and aggregations',
     [0.30, 0.76, 0.62, 0.81, 0.24, 0.71, 0.92, 0.20]),
    ('knn-5', 'knn vector', 'query', 'dense vector similarity search against indexed vectors',
     [0.22, 0.88, 0.71, 0.20, 0.21, 0.90, 0.34, 0.55]),
    ('knn-6', 'async search', 'query', 'submit long running search and poll the async response',
     [0.60, 0.31, 0.22, 0.72, 0.58, 0.28, 0.79, 0.24]),
]


def api_docs(offset_days, step_seconds):
    start = API_START + timedelta(days=offset_days)
    out = []
    for i, (service, endpoint, method, status, latency) in enumerate(API_ROWS):
        doc = {
            '@timestamp': (start + timedelta(seconds=i * step_seconds)).isoformat(),
            'service': service,
            'endpoint': endpoint,
            'method': method,
            'status': status,
            'latency_ms': latency,
            'src_ip': f'10.10.{i % 10}.{i % 250 + 1}',
            'message': f'{method} {endpoint}: status={status} latency={latency}ms',
        }
        out.append((f'api-{offset_days}-{i:02d}', doc))
    return out


def ensure_data_stream(client, seed):
    policy = GET_template = None
    body = {
        'index_patterns': [DATA_STREAM + '*'],
        'priority': 200,
        'data_stream': {},
        'template': {
            'settings': {
                'number_of_shards': 1,
                'number_of_replicas': 1,
                'index.lifecycle.name': ILM,
            },
            'mappings': {
                'properties': {
                    '@timestamp': {'type': 'date'},
                    'service': {'type': 'keyword'},
                    'endpoint': {'type': 'keyword'},
                    'method': {'type': 'keyword'},
                    'status': {'type': 'integer'},
                    'latency_ms': {'type': 'integer'},
                    'src_ip': {'type': 'ip'},
                    'message': {'type': 'text', 'fields': {'keyword': {'type': 'keyword', 'ignore_above': 256}}},
                }
            },
        },
    }
    client.request('PUT', f'/_ilm/policy/{ILM}', {
        'policy': {
            'phases': {
                'hot': {'actions': {'rollover': {'max_size': '2gb', 'max_age': '30d'}}},
                'delete': {'min_age': '30d', 'actions': {'delete': {}}},
            }
        },
    })
    client.request('PUT', f'/_index_template/{TEMPLATE}', body)
    try:
        existing = client.request('GET', f'/_data_stream/{DATA_STREAM}')
    except APIError as exc:
        if exc.status != 404:
            raise
        existing = None
    if existing and existing.get('data_streams'):
        count = client.request('POST', f'/{DATA_STREAM}/_count', {'query': {'match_all': {}}})
        return {'status': 'reused', 'docs': count.get('count', 0)}
    payload = b''.join(b'{"create":{}}\n' + json.dumps(doc, ensure_ascii=False).encode() + b'\n'
                       for _, doc in api_docs(1, 60))
    result = client.request('POST', f'/{DATA_STREAM}/_bulk', payload, 'application/x-ndjson')
    imported = 0
    for item in result.get('items', []):
        op = item.get('create', {})
        if 200 <= op.get('status', 0) < 300:
            imported += 1
        elif not op.get('status', 0):
            raise RuntimeError('Bulk create item returned no status: ' + json.dumps(item)[:500])
    client.request('POST', f'/{DATA_STREAM}/_refresh')
    return {'status': 'created', 'docs': imported}


def run_esql(client, query):
    return client.request('POST', '/_query', {'query': query})


def run_knn(client):
    payload = b''.join(
        b'{"index":{"_id":"' + doc_id.encode() + b'"}}\n' + json.dumps(
            {'title': title, 'category': category, 'text': text, 'vector': vector},
            ensure_ascii=False).encode() + b'\n'
        for (doc_id, title, category, text, vector) in KNN_DOCS)
    result = client.request('POST', f'/{KNN_INDEX}/_bulk', payload, 'application/x-ndjson')
    errors = sum(1 for item in result.get('items', []) if item.get('index', {}).get('status', 200) >= 300)
    client.request('POST', f'/{KNN_INDEX}/_refresh')
    query_vector = KNN_DOCS[2][4]
    knn = client.request('POST', f'/{KNN_INDEX}/_search', {
        'knn': {'field': 'vector', 'query_vector': query_vector, 'k': 3},
        '_source': ['title', 'category'], 'size': 3,
    })
    hits = [{'id': hit['_id'], 'title': hit['_source'].get('title'),
             'score': hit.get('_score')} for hit in knn.get('hits', {}).get('hits', [])]
    return {'bulk_errors': errors, 'top': hits}


def run_async(client):
    body = {
        'size': 0,
        'aggs': {
            'by_country': {
                'terms': {'field': 'country', 'size': 20},
                'aggs': {'avg_amt': {'avg': {'field': 'amount'}}},
            }
        },
    }
    submitted = client.request(
        'POST', '/lab-transactions-v1/_async_search?wait_for_completion_timeout=50ms&keep_on_completion=true',
        body)
    search_id = submitted.get('id')
    if not search_id:
        raise RuntimeError('Async search returned no id: ' + json.dumps(submitted)[:1000])
    result, running = submitted, submitted.get('is_running', False)
    deadline = time.monotonic() + 30
    while running and time.monotonic() < deadline:
        time.sleep(0.5)
        result = client.request(
            'GET', '/_async_search/' + urllib.parse.quote(search_id, safe='') + '?wait_for_completion_timeout=1s')
        running = result.get('is_running', False)
    client.request('DELETE', '/_async_search/' + urllib.parse.quote(search_id, safe=''))
    agg = result.get('response', result).get('aggregations', {})
    countries = [{'country': b['key'], 'count': b['doc_count']}
                 for b in agg.get('by_country', {}).get('buckets', [])[:5]]
    return {'id_prefix': search_id[:12], 'is_running_final': running, 'took_ms': result.get('took'),
            'top_countries': countries}


def ensure_knn_index(client):
    try:
        client.request('DELETE', f'/{KNN_INDEX}')
    except APIError as exc:
        if exc.status != 404:
            raise
    client.request('PUT', f'/{KNN_INDEX}', {
        'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
        'mappings': {
            'dynamic': False,
            'properties': {
                'title': {'type': 'keyword'},
                'category': {'type': 'keyword'},
                'text': {'type': 'text'},
                'vector': {'type': 'dense_vector', 'dims': 8},
            },
        },
    })


def clean(client):
    for path in (f'/_data_stream/{DATA_STREAM}', f'/_index_template/{TEMPLATE}',
                 f'/_ilm/policy/{ILM}', f'/{KNN_INDEX}'):
        try:
            client.request('DELETE', path)
        except APIError as exc:
            if exc.status != 404:
                raise
    print('[clean] removed data stream, template, ILM policy and kNN index')


def main():
    parser = argparse.ArgumentParser(description='ES9 modern feature scenarios (es9-only)')
    parser.add_argument('--seed', type=int, default=42, help='Deterministic seed (unused; data is fixed)')
    parser.add_argument('--clean', action='store_true', help='Remove all artifacts created by this script')
    args = parser.parse_args()

    client = ESClient()
    client.assert_lab()

    if args.clean:
        clean(client)
        return 0

    try:
        report = {'features': {}, 'esql': []}
        report['features']['data_stream'] = ensure_data_stream(client, args.seed)
        ensure_knn_index(client)
        for name, query in ESQL_QUERIES:
            response = run_esql(client, query)
            columns = [c.get('name') for c in response.get('columns', [])]
            rows = [[(repr(v) if not isinstance(v, (int, float, bool)) else v)
                     for v in row] for row in response.get('values', [])]
            report['esql'].append({'name': name, 'columns': columns, 'rows': rows})
            print(f'[esql] {name}: {len(rows)} rows')
        report['features']['knn'] = run_knn(client)
        report['features']['async_search'] = run_async(client)
        for key in ('data_stream', 'knn'):
            print(f'[{key}] {json.dumps(report["features"][key], ensure_ascii=False)[:300]}')
        print(f'[async_search] {json.dumps(report["features"]["async_search"], ensure_ascii=False)[:300]}')
        report_path = ROOT / 'reports/features.json'
        report_path.parent.mkdir(exist_ok=True)
        write_json(report_path, report)
        print(f'[report] {report_path}')
        return 0
    except (APIError, RuntimeError, OSError) as exc:
        print(f'[error] {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())