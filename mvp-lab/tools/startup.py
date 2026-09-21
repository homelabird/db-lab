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


def collect(compose, *, include_logs=True, budget=30.0) -> dict:
    """Observations, not a DB I/O/replication proof. Engine pin is mandatory."""
    if not 0 < budget <= 60:
        raise ValueError('diagnostic_budget_out_of_range')
    deadline = time.monotonic() + budget
    compose.guard_target()
    binding = compose.guard_engine()
    if compose.engine != ['docker'] or not binding.get('local_docker'):
        raise RuntimeError('diagnostics_require_pinned_local_docker')
    rows = []
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return min(value, 5.0)
    def docker(*argv):
        result = subprocess.run([*compose.engine, *argv], env=compose.inherited,
                                capture_output=True, text=True, timeout=remaining(), check=True)
        if len(result.stdout) > 4 * 1024**2:
            raise RuntimeError('diagnostic_output_exceeded')
        return result.stdout + result.stderr if argv[0] == 'logs' else result.stdout
    for service in BASE_SERVICES:
        row = {'service': service, 'ready': False, 'state': 'unknown', 'signals': []}
        try:
            result = compose.run('ps', '-a', '-q', service, capture=True, timeout=remaining())
            ids = result.stdout.split()
            if len(ids) != 1:
                row['state'] = 'missing' if not ids else 'ambiguous'
            else:
                if not re.fullmatch(r'[a-f0-9]{12,64}', ids[0]):
                    raise RuntimeError('invalid_container_identity')
                values = json.loads(docker('inspect', ids[0]))
                if not isinstance(values, list) or len(values) != 1:
                    raise RuntimeError('invalid_inspect_response')
                row = sanitize_container(values[0], compose.config['MVP_PROJECT'], service)
                if include_logs and not row['ready']:
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
    return {'schema': 1, 'ready': all(r['ready'] for r in rows), 'containers': rows,
            'scope': 'read-only container state; not database correctness or durability',
            'raw_logs_included': False, 'environment_included': False}
