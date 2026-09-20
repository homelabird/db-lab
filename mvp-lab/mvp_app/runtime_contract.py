"""Read-only live prerequisite checks, plus a build-time source manifest.

No initialize, INSERT, SET cache, refresh, Kafka produce/commit, or test DB fallback.
This verifies the running image, not whether the image is current or vulnerability-free.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any
from .adapters import Settings, SQL, Cache, Search, Broker
from .observability import safe_error

SCHEMA = 1
ROOT = Path(__file__).resolve().parents[1]
BUILD_FILE = 'build-manifest.json'
DISTRIBUTIONS = {'PyMySQL': 'pymysql', 'confluent-kafka': 'confluent_kafka',
                 'redis': 'redis', 'requests': 'requests'}
TABLE_COLUMNS = {
    'orders': {'id': 'char', 'idempotency_key': 'varchar', 'item': 'varchar', 'quantity': 'int',
               'unit_price': 'bigint', 'total': 'bigint', 'status': 'varchar', 'version': 'bigint', 'created_at': 'varchar'},
    'outbox': {'seq': 'bigint', 'event_id': 'char', 'payload': 'longtext', 'created_at': 'timestamp', 'sent_at': 'timestamp'},
}
ES_FIELDS = {'id': 'keyword', 'item': 'text', 'quantity': 'integer', 'unit_price': 'long',
             'total': 'long', 'status': 'keyword', 'version': 'long', 'created_at': 'date'}

class ContractError(RuntimeError):
    """Only a constant, non-sensitive identifier may be serialized."""
    def __init__(self, code: str):
        if not re.fullmatch(r'[a-z_]{1,80}', code):
            raise ValueError('Invalid contract failure code')
        self.code = code
        super().__init__(code)


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def source_manifest(root: Path) -> dict:
    root = Path(root)
    paths = [root / 'requirements.txt', root / 'Containerfile']
    paths += sorted(p for p in (root / 'mvp_app').rglob('*') if p.suffix in {'.py', '.html'})
    if not (root / 'mvp_app/runtime_contract.py').is_file():
        raise ContractError('runtime_contract_source_missing')
    files = {}
    for p in sorted(paths):
        if p.is_symlink() or not p.is_file() or p.stat().st_size > 2 * 1024**2:
            raise ContractError('source_not_a_bounded_regular_file')
        # Do not follow a symlinked source-directory ancestor either.
        if root.resolve() not in p.resolve().parents:
            raise ContractError('source_outside_project')
        files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return {'schema': SCHEMA, 'files': files, 'sha256': hashlib.sha256(canonical(files)).hexdigest()}


def seal(root: Path) -> dict:
    value = source_manifest(root)
    # Build-only command; no DB libraries or network requests are used here.
    (root / BUILD_FILE).write_bytes(canonical(value) + b'\n')
    return value


def target_manifest(settings: Settings, project: str) -> dict:
    value = {k: getattr(settings, k) for k in Settings.__dataclass_fields__}
    for key in ('sql_password', 'redis_password'):
        value[key] = hashlib.sha256(value[key].encode()).hexdigest()
    value['study_project'] = project
    return value


def check_source(root: Path, expected: str) -> dict:
    actual = source_manifest(root)
    path = root / BUILD_FILE
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
        raise ContractError('image_build_manifest_missing_rebuild_required')
    built = json.loads(path.read_text())
    if built != actual:
        raise ContractError('image_files_differ_from_build_manifest')
    if actual['sha256'] != expected:
        raise ContractError('running_image_differs_from_host_source')
    return {'source_sha256': actual['sha256'], 'file_count': len(actual['files']), 'build_manifest_matches': True}


def check_packages(root: Path) -> dict:
    pins = {}
    for line in (root / 'requirements.txt').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9-]+)==([0-9][A-Za-z0-9.+-]*)', line)
        if not match or match[1] in pins:
            raise ContractError('unsupported_dependency_pin')
        pins[match[1]] = match[2]
    if set(pins) != set(DISTRIBUTIONS):
        raise ContractError('dependency_contract_changed')
    observed = {}
    for dist, module in DISTRIBUTIONS.items():
        # Distribution version, not PyMySQL's compatibility __version__ string.
        observed[dist] = metadata.version(dist)
        importlib.import_module(module)
        if observed[dist] != pins[dist]:
            raise ContractError('installed_dependency_differs_from_pin')
    kafka = importlib.import_module('confluent_kafka')
    admin = importlib.import_module('confluent_kafka.admin')
    if not hasattr(kafka, 'TopicCollection') or not hasattr(admin.AdminClient, 'describe_topics'):
        raise ContractError('kafka_topic_identity_api_missing')
    if sys.version_info[:2] != (3, 12):
        raise ContractError('unexpected_image_python_version')
    return {'python': platform.python_version(), 'distributions': observed,
            'librdkafka': kafka.libversion()[0], 'scope': 'direct pins and imports; not a CVE scan or complete lockfile'}


def check_sql(repo: SQL) -> dict:
    conn = repo.connect()
    try:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION READ ONLY')
            cur.execute('SELECT DATABASE() AS db, VERSION() AS version, @@datadir AS datadir, '
                        '@@session.tx_isolation AS isolation_level, @@session.innodb_lock_wait_timeout AS lock_wait')
            info = cur.fetchone()
            if info['db'] != 'mvp' or 'MariaDB' not in info['version']:
                raise ContractError('unexpected_database_or_engine')
            if info['datadir'].rstrip('/') != '/var/lib/mysql':
                raise ContractError('unexpected_mariadb_data_directory')
            if info['lock_wait'] != 2:
                raise ContractError('unexpected_sql_lock_wait')
            cur.execute('SELECT TABLE_NAME AS name, ENGINE AS engine FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')
            if {r['name']: r['engine'] for r in cur.fetchall()} != {'orders': 'InnoDB', 'outbox': 'InnoDB'}:
                raise ContractError('unexpected_application_tables_or_engine')
            cur.execute('SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, DATA_TYPE AS data_type '
                        'FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()')
            tables = {name: {} for name in TABLE_COLUMNS}
            for row in cur.fetchall():
                tables.setdefault(row['table_name'], {})[row['column_name']] = row['data_type'].lower()
            if tables != TABLE_COLUMNS:
                raise ContractError('application_column_contract_mismatch')
            cur.execute('SELECT TABLE_NAME AS table_name, INDEX_NAME AS index_name, NON_UNIQUE AS non_unique, '
                        'SEQ_IN_INDEX AS seq_in_index, COLUMN_NAME AS column_name '
                        'FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() ORDER BY SEQ_IN_INDEX')
            unique = {}
            for row in cur.fetchall():
                if row['non_unique'] == 0:
                    unique.setdefault((row['table_name'], row['index_name']), []).append(row['column_name'])
            required = {'orders': {('id',), ('idempotency_key',)}, 'outbox': {('seq',), ('event_id',)}}
            for table, keys in required.items():
                actual = {tuple(columns) for (t, _), columns in unique.items() if t == table}
                if not keys.issubset(actual):
                    raise ContractError('application_unique_key_contract_mismatch')
        return {**info, 'table_contract': 'orders-outbox-inno-db-v1', 'unique_keys_checked': True,
                'scope': 'read-only metadata, not DDL/concurrency/persistence verification'}
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def check_redis(cache: Cache) -> dict:
    try:
        if not cache.client.ping():
            raise ContractError('redis_ping_failed')
        server = cache.client.info('server')
        replication = cache.client.info('replication')
        persistence = cache.client.info('persistence')
        if replication.get('role') != 'master' or persistence.get('aof_enabled') != 1:
            raise ContractError('redis_role_or_persistence_mismatch')
        return {'version': server['redis_version'], 'role': replication['role'], 'aof_enabled': 1,
                'scope': 'PING/INFO only; no SET/DEL or durability claim'}
    finally:
        cache.close()


def check_search(search: Search) -> dict:
    try:
        root = search.session.get(search.s.es_url.rstrip('/'), timeout=(2, 3))
        root.raise_for_status()
        mapping = search.request('GET', '/_mapping')
        mapping.raise_for_status()
        value = mapping.json()[search.s.es_index]['mappings']
        fields = {k: v.get('type') for k, v in value.get('properties', {}).items()}
        if value.get('dynamic') != 'strict' or fields != ES_FIELDS:
            raise ContractError('search_mapping_contract_mismatch')
        settings = search.request('GET', '/_settings')
        settings.raise_for_status()
        index = settings.json()[search.s.es_index]['settings']['index']
        if str(index.get('number_of_shards')) != '1' or str(index.get('number_of_replicas')) != '0':
            raise ContractError('search_topology_contract_mismatch')
        return {'version': root.json()['version']['number'], 'index_uuid': index['uuid'],
                'mapping_checked': True, 'scope': 'GET only; no indexing/refresh/repair'}
    finally:
        search.close()


def check_kafka(broker: Broker) -> dict:
    from confluent_kafka import TopicCollection
    from confluent_kafka.admin import AdminClient
    admin = AdminClient(broker.client_config())
    topic = admin.describe_topics(TopicCollection([broker.s.kafka_topic]), request_timeout=5)[broker.s.kafka_topic].result(timeout=8)
    topic_id = str(topic.topic_id)
    if topic.is_internal or len(topic.partitions) != 1 or topic_id in {'', 'None', 'AAAAAAAAAAAAAAAAAAAAAA'}:
        raise ContractError('kafka_topic_identity_or_partition_mismatch')
    value = broker.status()
    for partition in value['partitions']:
        if partition['offset_state'] in {'below_retention', 'ahead_of_log'}:
            raise ContractError('kafka_checkpoint_outside_retention')
        if partition['offset_state'] == 'uninitialized' and partition['low'] != 0:
            raise ContractError('kafka_checkpoint_missing_with_retained_prefix_gap')
    return {'topic_id': topic_id, 'partitions': value['partitions'], 'lag': value['lag'],
            'scope': 'metadata and checkpoints only; no subscribe/produce/commit'}


def run_probe(request: dict, root: Path = ROOT) -> dict:
    if (not isinstance(request, dict) or set(request) != {'schema', 'service', 'source_sha256', 'target'}
            or request['schema'] != SCHEMA or request['service'] not in {'api', 'worker'}
            or not re.fullmatch(r'[a-f0-9]{64}', str(request['source_sha256']))
            or not isinstance(request['target'], dict)):
        raise ContractError('invalid_probe_request')
    checks = []
    def check(name, operation):
        try:
            details = operation()
            checks.append({'name': name, 'status': 'passed', 'detail': details})
        except Exception as exc:
            checks.append({'name': name, 'status': 'failed', **safe_error(exc)})
    check('image_source', lambda: check_source(root, request['source_sha256']))
    check('driver_imports_and_pins', lambda: check_packages(root))
    settings = Settings.load()
    def target():
        if target_manifest(settings, os.environ.get('STUDY_PROJECT', '')) != request['target']:
            raise ContractError('effective_container_configuration_mismatch')
        return {'matches_expected': True, 'credentials': 'hash comparison only, hashes not repeated in report'}
    check('effective_target', target)
    if all(c['status'] == 'passed' for c in checks):
        for name, operation in [('mariadb', lambda: check_sql(SQL(settings))), ('redis', lambda: check_redis(Cache(settings))),
                                ('elasticsearch', lambda: check_search(Search(settings))), ('kafka', lambda: check_kafka(Broker(settings)))]:
            check(name, operation)
    else:
        checks += [{'name': name, 'status': 'not_run', 'reason': 'image_or_configuration_prerequisite_failed'}
                   for name in ('mariadb', 'redis', 'elasticsearch', 'kafka')]
    return {'schema': SCHEMA, 'service': request['service'], 'checks': checks,
            'status': 'passed' if all(c['status'] == 'passed' for c in checks) else 'failed',
            'evidence_kind': 'LIVE-DRIVER-READ-ONLY-PROBE', 'data_writes': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('seal', 'probe'))
    args = parser.parse_args(argv)
    try:
        if args.action == 'seal':
            print(json.dumps({'source_sha256': seal(ROOT)['sha256']}))
            return 0
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ContractError('probe_request_too_large')
        result = run_probe(json.loads(raw))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'passed' else 1
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'checks': [], **safe_error(exc)}))
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
