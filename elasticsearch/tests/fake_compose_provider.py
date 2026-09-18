#!/usr/bin/env python3
"""TEST FIXTURE ONLY: forwards exec/curl and node actions to a loopback fake API.
This does not start containers. Never install this as your actual compose provider.
"""
import json
import os
import subprocess
import sys
import urllib.request


def main():
    base = os.environ['FAULT_TEST_API']
    if not base.startswith('http://127.0.0.1:'):
        raise RuntimeError('Fixture accepts only a loopback test server')
    args = sys.argv[1:]
    if args[:1] == ['-p']: args = args[2:]
    command = args[0]
    if command in ('stop', 'kill', 'start'):
        data = json.dumps({'action': command, 'node': args[-1]}).encode()
        req = urllib.request.Request(base + '/__fixture__/action', data=data,
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req) as resp: resp.read()
        return 0
    if command == 'exec':
        if args[1] != '-T': raise RuntimeError('exec -T required')
        node, program = args[2], args[3:]
        if program[0] != 'curl': raise RuntimeError('Only curl allowed')
        program = [part.replace('http://127.0.0.1:9200', base) for part in program]
        program[1:1] = ['-H', 'X-Fault-Test-Node: ' + node]
        payload = sys.stdin.buffer.read() if '@-' in program else b''
        return subprocess.run(program, input=payload, check=False).returncode
    raise RuntimeError('Unsupported fixture command: ' + command)


if __name__ == '__main__': sys.exit(main())
