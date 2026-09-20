"""Lease-limited egress netem, running ONLY in a labelled target container's network namespace.
Never uses host networking, iptables, modprobe, mounts, or the Docker socket.
"""
import argparse
import json
import signal
import subprocess
import threading

HANDLE = '7a11:'


class NetemError(RuntimeError):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def tc(*args):
    value = subprocess.run(['tc', *args], capture_output=True, text=True, check=False, timeout=4)
    if value.returncode:
        text = value.stderr.lower()
        kind = ('capability_denied' if 'operation not permitted' in text else
                'qdisc_conflict' if 'file exists' in text else
                'interface_missing' if 'cannot find device' in text else
                'netem_kernel_unavailable' if 'unknown qdisc' in text or 'specified qdisc not found' in text else
                'tc_command_failed')
        raise NetemError(kind)
    return value.stdout


def state():
    return json.loads(tc('-j', '-s', 'qdisc', 'show', 'dev', 'eth0'))


def owned(rows):
    return [r for r in rows if r.get('kind') == 'netem' and r.get('handle') == HANDLE and r.get('root')]


def clear():
    rows = state()
    mine = owned(rows)
    roots = [r for r in rows if r.get('root')]
    if mine:
        tc('qdisc', 'del', 'dev', 'eth0', 'root', 'handle', HANDLE)
    elif any(r.get('kind') != 'noqueue' for r in roots):
        raise RuntimeError('Foreign root qdisc: not removed')
    if owned(state()):
        raise RuntimeError('Netem removal not confirmed')
    return mine


def run(mode, seconds, stop):
    if mode == 'cleanup':
        result = {'stage': 'netem_cleared', 'statistics': clear()}
        print(json.dumps(result), flush=True)
        return result
    if mode not in {'delay', 'loss'} or not 1 <= seconds <= 30:
        raise ValueError('Invalid bounded netem request')
    rows = state()
    if any(r.get('kind') != 'noqueue' for r in rows):
        raise RuntimeError('Existing qdisc policy: refusing replacement')
    impairment = ['delay', '180ms', '20ms'] if mode == 'delay' else ['loss', 'random', '10%']
    # The helper is also its own watchdog if the HOST CLI dies. No indefinite sleep.
    try:
        tc('qdisc', 'add', 'dev', 'eth0', 'root', 'handle', HANDLE, 'netem', 'limit', '1000', *impairment)
        if not owned(state()):
            raise RuntimeError('Netem application was not observed')
        print(json.dumps({'stage': 'netem_applied', 'mode': mode, 'lease_seconds': seconds,
                          'scope': 'target eth0 egress; includes replies; not ingress or multi-host partition'}), flush=True)
        stop.wait(seconds)
    finally:
        result = {'stage': 'netem_cleared', 'statistics': clear()}
        print(json.dumps(result), flush=True)
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=('delay', 'loss', 'cleanup'))
    p.add_argument('--seconds', type=int, default=1)
    a = p.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        run(a.mode, a.seconds, stop)
        return 0
    except Exception as exc:
        print(json.dumps({'stage': 'netem_failed', 'error': type(exc).__name__, 'reason': getattr(exc, 'kind', 'helper_failure')}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
