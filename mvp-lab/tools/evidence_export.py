"""Export a small allowlisted acceptance report, never recursively copy lab reports.

No .env, state/marker, order/payload, raw stdout/stderr, DSN, or logs are exported.
This is a result projection, NOT an assertion that arbitrary files are safe to publish.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from .acceptance import SUITES, steps_for, read_json, RUN_RE

STATUSES = {'passed', 'failed', 'blocked', 'inconclusive', 'aborted', 'not_run', 'running'}
CHECKS = {'image_source', 'driver_imports_and_pins', 'effective_target', 'mariadb', 'redis', 'elasticsearch', 'kafka'}
ERROR_CODES = {
    'runtime_contract_source_missing', 'source_not_a_bounded_regular_file', 'source_outside_project',
    'image_build_manifest_missing_rebuild_required', 'image_files_differ_from_build_manifest',
    'running_image_differs_from_host_source', 'unsupported_dependency_pin', 'dependency_contract_changed',
    'installed_dependency_differs_from_pin', 'kafka_topic_identity_api_missing', 'unexpected_image_python_version',
    'unexpected_database_or_engine', 'unexpected_mariadb_data_directory', 'unexpected_sql_lock_wait',
    'unexpected_application_tables_or_engine', 'application_column_contract_mismatch',
    'application_unique_key_contract_mismatch', 'redis_ping_failed', 'redis_role_or_persistence_mismatch',
    'search_mapping_contract_mismatch', 'search_topology_contract_mismatch',
    'kafka_topic_identity_or_partition_mismatch', 'kafka_checkpoint_outside_retention',
    'kafka_checkpoint_missing_with_retained_prefix_gap', 'effective_container_configuration_mismatch',
    'invalid_probe_request', 'probe_request_too_large', 'kafka_checkpoint_unconfirmed',
}
HINTS = {
    'image_source': '호스트 소스와 실행 이미지가 다릅니다. 보존 중인 실습 자원을 먼저 정리하고 앱을 다시 빌드하세요.',
    'driver_imports_and_pins': '컨테이너 Python과 직접 의존성 설치 버전을 확인하세요. 호스트 설치만으로 해결되지 않습니다.',
    'effective_target': '실험 모드와 실행 컨테이너의 연결 설정을 확인하세요. 비밀번호를 새로 생성하거나 pin을 삭제하지 마세요.',
    'mariadb': 'MariaDB 연결·스키마·영속 경로를 조사하세요. 기존 테이블이나 볼륨을 삭제하지 마세요.',
    'redis': 'Redis 연결·역할·AOF 설정을 확인하세요. 연결 성공은 캐시 내용 일치를 보장하지 않습니다.',
    'elasticsearch': 'ES 연결·매핑·샤드 설정을 확인하세요. 재색인 전에 원본과 현재 인덱스의 증거를 보존하세요.',
    'kafka': '토픽과 checkpoint/보존 범위를 확인하세요. offset reset만으로 데이터 복구를 완료 처리하지 마세요.',
}


def status(value):
    return value if isinstance(value, str) and value in STATUSES else 'failed'


def number(value, *, maximum=10000000):
    return value if type(value) is int and -255 <= value <= maximum else None


def project_checks(value):
    rows = []
    if not isinstance(value, dict) or not isinstance(value.get('checks'), list):
        return rows
    for item in value['checks'][:20]:
        if not isinstance(item, dict) or item.get('name') not in CHECKS:
            continue
        row = {'name': item['name'], 'status': status(item.get('status'))}
        code = item.get('code')
        row['code'] = code if isinstance(code, str) and code in ERROR_CODES else number(code, maximum=9999)
        row['http_status'] = number(item.get('http_status'), maximum=599)
        if row['status'] not in {'passed', 'not_run'}:
            row['investigate'] = HINTS[row['name']]
        rows.append(row)
    return rows


def safe_read(path: Path, root: Path):
    root = root.resolve()
    if not path.is_file() or any(p.is_symlink() for p in [path, *path.parents] if p != root):
        raise RuntimeError('Symlinked/missing evidence is not exportable')
    if not path.resolve().is_relative_to(root):
        raise RuntimeError('Evidence lies outside selected report')
    return read_json(path)


def project_acceptance(root: Path, run_id: str) -> dict:
    if not isinstance(run_id, str) or not re.fullmatch(RUN_RE, run_id):
        raise ValueError('Invalid acceptance run ID')
    directory = root / 'reports/acceptance' / run_id
    value = safe_read(directory / 'summary.json', root)
    suite = value.get('suite')
    if suite not in SUITES or suite == 'candidate':
        # Candidate references are operator inputs; keep candidate reports local for now.
        raise ValueError('Unsupported public export suite')
    if value.get('run_id') != run_id:
        raise ValueError('Evidence run identity mismatch')
    expected = ['container-targets', 'api-runtime-contract', 'worker-runtime-contract'] + [s.name for s in steps_for(suite)]
    source_rows = value.get('steps')
    if not isinstance(source_rows, list) or [s.get('name') for s in source_rows] != expected:
        raise ValueError('Evidence step plan is incomplete or changed')
    rows = []
    for item in source_rows:
        row = {'name': item['name'], 'status': status(item.get('status')),
               'returncode': number(item.get('returncode'), maximum=255)}
        if item['name'] in {'api-runtime-contract', 'worker-runtime-contract'}:
            path = directory / (item['name'] + '.json')
            if path.exists():
                raw = safe_read(path, root)
                row['checks'] = project_checks(raw.get('result'))
        rows.append(row)
    final = status(value.get('status'))
    if final == 'passed' and any(r['status'] != 'passed' for r in rows):
        final = 'failed'
    scenarios = {s.name for s in steps_for(suite) if s.scenario}
    return {'schema': 1, 'run_id': run_id, 'suite': suite, 'status': final,
            'evidence_kind': 'ALLOWLISTED-ACCEPTANCE-RESULT-NOT-INDEPENDENT-DB-PROOF',
            'source_summary_sha256': hashlib.sha256((directory/'summary.json').read_bytes()).hexdigest(),
            'planned_scenarios': len(scenarios),
            'passed_scenarios': sum(r['status'] == 'passed' and r['name'] in scenarios for r in rows),
            'steps': rows, 'omitted': ['raw logs', 'free text errors', 'orders/payloads', 'environment', 'credentials',
                                     'recovery markers', 'connection targets', 'candidate image references']}


def write_projection(value: dict, destination: Path, atomic):
    # Refuse overwrite: do not mix an old PASS bundle with a new failed run.
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    atomic(destination/'summary.json', value)
    lines = ['# 공유용 실행 결과', '', f"판정: **{value['status']}**", '',
             '실제 DB 검증의 대체가 아닌, 선택된 실행 기록의 허용 필드만 추출한 결과입니다.', '',
             '| 단계 | 상태 | 종료 코드 |', '|---|---|---:|']
    notes=[]
    for row in value['steps']:
        lines.append(f"| {row['name']} | {row['status']} | {row.get('returncode')} |")
        for check in row.get('checks', []):
            if check.get('investigate'):
                notes += ['', f"**{check['name']}**: `{check['code']}` — {check['investigate']}"]
    lines += notes
    (destination/'report.md').write_text('\n'.join(lines)+'\n')
    xml = ET.Element('testsuite', name='db-lab-public-' + value.get('suite', 'pipeline'), tests=str(len(value['steps'])))
    for row in value['steps']:
        case = ET.SubElement(xml, 'testcase', name=row['name'])
        if row['status'] == 'not_run': ET.SubElement(case, 'skipped', message='not executed')
        elif row['status'] in {'blocked', 'running', 'aborted'}: ET.SubElement(case, 'error', message=row['status'])
        elif row['status'] != 'passed': ET.SubElement(case, 'failure', message=row['status'])
    xml.set('failures', str(len(xml.findall('testcase/failure')))); xml.set('errors', str(len(xml.findall('testcase/error'))))
    xml.set('skipped', str(len(xml.findall('testcase/skipped'))))
    ET.ElementTree(xml).write(destination/'junit.xml', encoding='utf-8', xml_declaration=True)
    for path in destination.iterdir(): path.chmod(0o600)
    return destination


def export(root: Path, run_id: str, atomic):
    result = project_acceptance(root, run_id)
    return write_projection(result, root/'reports/share'/run_id, atomic)
