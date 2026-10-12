"""Bounded read-only container diagnostics. Never emits Env, health output or raw logs."""
from __future__ import annotations
import json
import re
import subprocess
import time
from .sim_engine import BASE_SERVICES

STATES = {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'}
HEALTH = {'starting', 'healthy', 'unhealthy'}
# Only the names of matches are returned. Original log lines never leave this function.
LOG_PATTERNS = {
    'permission_denied': r'permission denied|operation not permitted',
    'storage_full': r'no space left on device',
    'authentication_rejected': r'access denied|authentication failed|wrongpass|noauth',
    'connection_refused': r'connection refused',
    'configuration_rejected': r'unrecognized configuration|unknown configuration|invalid configuration|fatal config',
    'memory_allocation_failed': r'cannot allocate memory|outofmemoryerror|out of memory',
    'topic_unavailable': r'unknown_topic_or_part|unknown topic or partition',
    'connection_timeout': r'connection timed out|connect timeout|timed out',
    'name_resolution_failed': r'temporary failure in name resolution|name or service not known|could not resolve host',
    'service_unavailable': r'service unavailable|server is not ready|not yet available',
}


def log_signals(text: str) -> list[str]:
    text = text[-256 * 1024:]
    return sorted(name for name, pattern in LOG_PATTERNS.items() if re.search(pattern, text, re.I))


def sanitize_container(raw: dict, project: str, service: str) -> dict:
    labels = raw.get('Config', {}).get('Labels') or {}
    if (labels.get('com.docker.compose.project') != project or
            labels.get('com.docker.compose.service') != service):
        raise RuntimeError('container_ownership_mismatch')
    identity = raw.get('Id', '')
    image = raw.get('Image', '')
    if not re.fullmatch(r'[a-f0-9]{64}', identity) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise RuntimeError('invalid_container_identity')
    state = raw.get('State', {})
    health = state.get('Health', {}).get('Status')
    status = state.get('Status')
    code, restarts = state.get('ExitCode'), raw.get('RestartCount')
    running = state.get('Running') is True
    paused = state.get('Paused') is True
    ready = (status == 'running' and running and not paused and state.get('Restarting') is not True and
             health == 'healthy')
    return {'service': service, 'container_id': identity, 'image_id': image,
            'state': status if status in STATES else 'unknown',
            'running': running, 'paused': paused, 'oom_killed': state.get('OOMKilled') is True,
            'health': health if health in HEALTH else 'not_configured' if health is None else 'unknown',
            'exit_code': code if type(code) is int and 0 <= code <= 255 else None,
            'restarts': restarts if type(restarts) is int and restarts >= 0 else None,
            'ready': ready, 'signals': []}


def collect(compose, *, include_logs=True, log_services=(), budget=30.0, services=None) -> dict:
    """Observations, not a DB I/O/replication proof. Engine pin is mandatory."""
    if not 0 < budget <= 60:
        raise ValueError('diagnostic_budget_out_of_range')
    services = BASE_SERVICES if services is None else tuple(services)
    if not services or len(set(services)) != len(services) or set(services) - {*BASE_SERVICES, 'redis-spare'}:
        raise ValueError('invalid_diagnostic_services')
    deadline = time.monotonic() + budget
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return min(value, 5.0)
    timeout_stage = None
    stage = 'target_guard'
    try:
        compose.guard_target()
        remaining()
        stage = 'engine_guard'
        binding = compose.guard_engine()
        remaining()
    except (TimeoutError, subprocess.TimeoutExpired):
        timeout_stage = stage
    if timeout_stage is None and (compose.engine != ['docker'] or not binding.get('local_docker')):
        raise RuntimeError('diagnostics_require_pinned_local_docker')
    rows = []
    def docker(*argv):
        result = subprocess.run([*compose.engine, *argv], env=compose.inherited,
                                capture_output=True, text=True, timeout=remaining(), check=True)
        if len(result.stdout) > 4 * 1024**2:
            raise RuntimeError('diagnostic_output_exceeded')
        return result.stdout + result.stderr if argv[0] == 'logs' else result.stdout
    values, unobserved_state = [], 'missing'
    try:
        if timeout_stage is not None:
            raise TimeoutError()
        stage = 'container_discovery'
        ids = docker('ps', '-aq', '--filter',
                     'label=com.docker.compose.project=' + compose.config['MVP_PROJECT']).split()
        if any(not re.fullmatch(r'[a-f0-9]{12,64}', identity) for identity in ids):
            raise RuntimeError('invalid_container_identity')
        stage = 'container_inspection'
        inspected = json.loads(docker('inspect', *ids)) if ids else []
        if (not isinstance(inspected, list) or len(inspected) != len(ids) or any(
                not isinstance(raw, dict) or
                (raw.get('Config', {}).get('Labels') or {}).get('com.docker.compose.project')
                != compose.config['MVP_PROJECT']
                for raw in inspected)):
            raise RuntimeError('invalid_inspect_response')
        values = inspected
    except (TimeoutError, subprocess.TimeoutExpired):
        unobserved_state = 'observation_timeout'
        timeout_stage = stage
    except Exception:
        unobserved_state = 'observation_failed'
    for service in services:
        row = {'service': service, 'ready': False, 'state': unobserved_state, 'signals': []}
        try:
            matches = [raw for raw in values if
                       raw['Config']['Labels'].get('com.docker.compose.service') == service]
            if len(matches) > 1:
                row['state'] = 'ambiguous'
            elif matches:
                row = sanitize_container(matches[0], compose.config['MVP_PROJECT'], service)
                if include_logs and (not row['ready'] or service in log_services):
                    try:
                        row['signals'] = log_signals(docker('logs', '--tail', '100', row['container_id']))
                    except Exception:
                        row['logs_unavailable'] = True
        except (TimeoutError, subprocess.TimeoutExpired):
            row['state'] = 'observation_timeout'
        except Exception:
            # Do not serialize exception strings, command args or subprocess output.
            row['state'] = 'observation_failed'
        rows.append(row)
    complete = all(r['state'] in STATES | {'missing', 'ambiguous'} and r.get('health') != 'unknown' for r in rows)
    return {'schema': 1, 'ready': all(r['ready'] for r in rows), 'containers': rows, 'services': list(services),
            'observation_complete': complete, 'timeout_stage': timeout_stage,
            'scope': 'read-only container state; not database correctness or durability',
            'raw_logs_included': False, 'environment_included': False}
