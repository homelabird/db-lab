"""Bind host-side HTTP commands to the pinned local engine and labelled API.

Project identity is an accidental-target guard, not HTTP authentication. External
container changes can still race an observation; this is not a distributed lock.
"""
from __future__ import annotations
import json
import re
import subprocess
from urllib.request import build_opener, ProxyHandler
from .probe import NoRedirect, read_object, verify_identity


def guard(compose):
    compose.guard_target()
    binding = compose.guard_engine()

    def read(*args):
        result = subprocess.run([*compose.engine, *args], env=compose.inherited,
                                capture_output=True, text=True, check=True, timeout=15)
        if len(result.stdout) > 4 * 1024**2:
            raise RuntimeError('HTTP target inspect response exceeded limit')
        return json.loads(result.stdout)

    if not isinstance(binding, dict):
        raise RuntimeError('Invalid pinned engine identity')
    if compose.engine == ['docker']:
        local = binding.get('local_docker') is True
    elif compose.engine == ['podman']:
        info = read('info', '--format', 'json')
        host = info.get('host') if isinstance(info, dict) else None
        local = isinstance(host, dict) and host.get('serviceIsRemote') is False
    else:
        local = False
    if not local:
        raise RuntimeError('Host HTTP commands require a verified local engine. Run on the lab host; remote engine loopback is not this host.')
    result = compose.run('ps', '-a', '-q', 'api', capture=True, timeout=15)
    ids = result.stdout.split()
    if len(ids) != 1 or not re.fullmatch(r'[a-f0-9]{12,64}', ids[0]):
        raise RuntimeError('HTTP command requires exactly one existing API container')
    rows = read('inspect', ids[0])
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError('Invalid API container inspect response')
    row = rows[0]
    config = row.get('Config')
    labels = config.get('Labels') if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        raise RuntimeError('Invalid API container labels')
    if (labels.get('com.docker.compose.project') != compose.config['MVP_PROJECT']
            or labels.get('com.docker.compose.service') != 'api'):
        raise RuntimeError('HTTP API container ownership mismatch')
    state = row.get('State') or {}
    if (not isinstance(state, dict) or state.get('Running') is not True
            or state.get('Paused') is True or state.get('Restarting') is True):
        raise RuntimeError('HTTP API container is not running')
    network = row.get('NetworkSettings')
    ports = network.get('Ports') if isinstance(network, dict) else None
    if (not isinstance(ports, dict) or any(entries is not None and not isinstance(entries, list)
                                          for entries in ports.values())):
        raise RuntimeError('Invalid API container port bindings')
    actual = [(port, entry) for port, entries in ports.items() for entry in (entries or [])]
    expected = [('8080/tcp', {'HostIp': '127.0.0.1', 'HostPort': compose.config['API_PORT']})]
    if actual != expected:
        raise RuntimeError('HTTP API port differs from the configured exclusive loopback binding')


def diagnostics(config):
    """Bounded, no-proxy, no-redirect GETs; never initialize or write a DB."""
    base = 'http://127.0.0.1:' + config['API_PORT']
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(base + '/api/study/info', timeout=8) as response:
        verify_identity(read_object(response), config['MVP_PROJECT'])
    with opener.open(base + '/api/diagnostics', timeout=25) as response:
        value = read_object(response)
    checks = value.get('dependencies')
    if (type(value.get('dependencies_reachable')) is not bool or not isinstance(checks, dict)
            or set(checks) != {'mariadb', 'redis', 'elasticsearch', 'kafka'}
            or any(not isinstance(row, dict) or type(row.get('reachable')) is not bool for row in checks.values())
            or value['dependencies_reachable'] != all(row['reachable'] for row in checks.values())):
        raise RuntimeError('Malformed or inconsistent diagnostics response')
    return value
