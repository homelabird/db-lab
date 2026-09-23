#!/usr/bin/env python3
"""Run one bounded child command while sampling host pressure."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('DB_LAB_LIB', str(ROOT.parent/'lib')))
from db_lab_benchmark import HostPressureSampler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command is required after --')
    sampler = HostPressureSampler().start()
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    finally:
        observation = sampler.stop()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.observation-', dir=args.output.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(observation, stream, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, args.output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
