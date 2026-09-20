"""Bounded pressure client for an isolated disposable Redis, never for the source Redis."""
from .observability import safe_error
import argparse
import json
import os
import re
import time
from .adapters import Settings


def pressure(settings, mode):
    if (not re.fullmatch(r'drill-[a-f0-9]{12}', os.environ.get('MVP_DISPOSABLE', ''))
            or settings.redis_host != '127.0.0.1' or mode not in ('disk-full', 'oom')):
        raise ValueError('Disposable local Redis is required')
    import redis
    client = redis.Redis(host='127.0.0.1', port=6379, password=settings.redis_password,
                         socket_timeout=2, socket_connect_timeout=2, decode_responses=False)
    client.set('drill:baseline', 'known-before-fault')
    # Limit DATA sent as well as container memory/tmpfs in the controller.
    limit = 128 if mode == 'oom' else 32  # MiB
    payload = os.urandom(256 * 1024)
    accepted = 0
    error = None
    started = time.monotonic()
    for i in range(limit * 4):
        if time.monotonic() - started > 25:
            break
        try:
            client.set('drill:fill:' + str(i), payload)
            accepted += len(payload)
        except Exception as exc:
            error = type(exc).__name__
            break
    observation = {'mode': mode, 'accepted_bytes': accepted, 'client_error': error}
    try:
        observation['baseline_readable'] = client.get('drill:baseline') == b'known-before-fault'
        info = client.info('persistence')
        observation['aof_last_write_status'] = info.get('aof_last_write_status')
    except Exception:
        observation['baseline_readable'] = None
    client.close()
    return observation


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=('disk-full', 'oom'))
    a = p.parse_args()
    try:
        print(json.dumps(pressure(Settings.load(), a.mode)))
        return 0  # controller MUST inspect server/kernel evidence, not this exit code alone
    except Exception as exc:
        print(json.dumps({'stage': 'pressure_failed', **safe_error(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
