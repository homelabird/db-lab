#!/usr/bin/env python3
"""Offline volume operations. Host controller must verify ALL nodes are stopped first."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

DATA = Path('/var/lib/mysql')
CONTROL = Path('/var/lib/labctl')

def parse_state(text):
    values = dict(re.findall(r'^(uuid|seqno|safe_to_bootstrap):\s*(\S+)', text, re.M))
    return {'uuid': values.get('uuid'), 'seqno': int(values.get('seqno', '-1')),
            'safe_to_bootstrap': int(values.get('safe_to_bootstrap', '0'))}

def inspect():
    path = DATA / 'grastate.dat'
    return dict(parse_state(path.read_text() if path.exists() else ''),
                initialized=(DATA / 'mysql').is_dir(),
                init_complete=(CONTROL / 'init-complete').exists())

def recovered_position(log):
    matches = re.findall(r'Recovered position:\s*([0-9a-fA-F-]{36}):(-?\d+)', log)
    if not matches:
        raise ValueError('No recovered position in recovery output. Inspect the full log.')
    uuid, seqno = matches[-1]
    if int(seqno) < 0 or uuid == '00000000-0000-0000-0000-000000000000':
        raise ValueError('Recovery did not produce a valid cluster position.')
    return {'uuid': uuid.lower(), 'seqno': int(seqno)}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('operation', choices=['inspect', 'authorize-new', 'authorize-safe',
                                        'authorize-recovered', 'recover', 'wipe'])
    p.add_argument('--uuid'); p.add_argument('--seqno', type=int)
    a = p.parse_args()
    state = inspect()
    if a.operation == 'inspect':
        print(json.dumps(state)); return
    if a.operation == 'recover':
        if not state['initialized'] or not state['init_complete']:
            raise ValueError('Uninitialized or partially initialized data volume; refusing recovery.')
        runtime = Path('/run/mysqld')
        runtime.mkdir(parents=True, exist_ok=True)
        shutil.chown(runtime, user='mysql', group='mysql')
        runtime.chmod(0o755)
        log = Path('/tmp/lab-wsrep-recover.log')
        if log.exists(): log.unlink()
        result = subprocess.run(['gosu', 'mysql', 'mariadbd', '--wsrep-recover',
                '--wsrep-on=ON', '--wsrep-provider=/opt/lab/libgalera_smm.so',
                '--wsrep-cluster-address=gcomm://',
                '--skip-networking', '--log-error=' + str(log)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
        text = result.stdout + ('\n' + log.read_text(errors='replace') if log.exists() else '')
        if result.returncode:
            print(text, file=sys.stderr)
            raise ValueError('mariadbd --wsrep-recover failed.')
        try: position = recovered_position(text)
        except ValueError:
            print(text, file=sys.stderr); raise
        print(json.dumps(dict(state, **position))); return
    if a.operation == 'wipe':
        # No user-supplied path accepted. Dedicated service volume only.
        if DATA != Path('/var/lib/mysql') or CONTROL != Path('/var/lib/labctl'):
            raise ValueError('Unexpected volume path.')
        for directory in (DATA, CONTROL):
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_dir(): path.unlink()
                else: shutil.rmtree(path)
        print(json.dumps({'wiped': True})); return
    if a.operation == 'authorize-new' and state['initialized']:
        raise ValueError('New-cluster authorization requires an empty node.')
    if a.operation == 'authorize-safe':
        if not (state['initialized'] and state['init_complete'] and state['safe_to_bootstrap'] == 1
                and state['seqno'] >= 0):
            raise ValueError('Not safe to bootstrap.')
    if a.operation == 'authorize-recovered':
        if (not state['initialized'] or not state['init_complete'] or not a.uuid
                or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', a.uuid)
                or a.uuid == '00000000-0000-0000-0000-000000000000'
                or a.seqno is None or a.seqno < 0):
            raise ValueError('A verified recovered position is required.')
        path = DATA / 'grastate.dat'
        if not path.exists(): raise ValueError('Missing grastate.dat.')
        # Use the position freshly recovered and compared by the host controller.
        text = '# GALERA saved state\nversion: 2.1\nuuid: ' + a.uuid + '\nseqno: ' + str(a.seqno) + '\nsafe_to_bootstrap: 1\n'
        path.write_text(text)
        stat = (DATA / 'mysql').stat()
        os.chown(path, stat.st_uid, stat.st_gid)
    CONTROL.mkdir(parents=True, exist_ok=True)
    (CONTROL / 'bootstrap-once').write_text(a.operation + '\n')
    print(json.dumps({'authorized': a.operation}))

if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, subprocess.TimeoutExpired) as e:
        print('ERROR: ' + str(e), file=sys.stderr); sys.exit(1)
