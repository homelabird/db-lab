#!/usr/bin/env python3
"""Compatibility entry points for the original numbered scenario scripts."""
import argparse
import json
import os
import signal
import sys
from pathlib import Path
import fault_lab
from lablib import INDICES, ROOT


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('scenario', choices=list(fault_lab.SCENARIOS))
    p.add_argument('node', nargs='?', help='Legacy positional node for drain, e.g. es03')
    modes = p.add_mutually_exclusive_group()
    modes.add_argument('--test', action='store_true', help='Inject, check, automatically recover')
    modes.add_argument('--restore', '--recover', dest='recover', action='store_true')
    p.add_argument('--crash', action='store_true', help='For node-stop only: SIGKILL instead of graceful stop')
    p.add_argument('--yes', action='store_true')
    p.add_argument('--hold', default='0')
    p.add_argument('--timeout', default=os.getenv('FAULT_TIMEOUT', '180'))
    p.add_argument('--report-dir', type=Path, default=ROOT / 'reports/faults')
    a = p.parse_args(argv)
    scenario = 'node-crash' if a.crash else a.scenario
    fault_lab.require(not a.crash or a.scenario == 'node-stop', '--crash is only for node failure')
    common = ['--timeout', a.timeout, '--report-dir', str(a.report_dir)]
    if a.recover:
        # Recovery uses the journaled node/index rather than potentially changed environment defaults.
        active = a.report_dir / 'active.json'
        fault_lab.require(active.exists(), 'No active journal. Run 13-fault-lab.sh diagnose first.')
        current = json.loads(active.read_text())['scenario']
        allowed = {'node-stop', 'node-crash'} if a.scenario == 'node-stop' else {a.scenario}
        fault_lab.require(current in allowed, f'Active scenario is {current}; use its wrapper or 13-fault-lab.sh recover')
        return fault_lab.main(['recover', *common])
    mode = 'run' if a.test else 'apply'
    fault_lab.require(a.test or float(a.hold) == 0, '--hold needs --test on a legacy wrapper')
    args = [mode, scenario, '--node', a.node or os.getenv('NODE', 'es03'),
            '--index', os.getenv('INDEX', INDICES[0]), *common]
    if a.yes: args.append('--yes')
    if a.test: args += ['--hold', a.hold]
    print(f'[managed] {mode} {scenario}; saved-state recovery replaces hard-coded default resets.', flush=True)
    return fault_lab.main(args)


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    try: sys.exit(main())
    except (RuntimeError, ValueError, OSError, KeyboardInterrupt) as exc:
        sys.exit(f'[FAIL] {exc}')
