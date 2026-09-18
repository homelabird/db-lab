#!/usr/bin/env bash
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
bash -n "$ROOT/lab.sh"
bash -n "$ROOT/ops.sh"
sh -n "$ROOT/container/perf-start.sh"
sh -n "$ROOT/container/entrypoint.sh"
python3 -m compileall -q "$ROOT/client" "$ROOT/scripts" "$ROOT/ops"
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v
