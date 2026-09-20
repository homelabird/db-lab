#!/usr/bin/env bash
# Host logic and template-contract tests only. Never starts containers or a cluster.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
python3 -c 'import yaml, jsonschema, requests' || { echo 'Install scripts/requirements-checks.txt in your test virtualenv.' >&2; exit 1; }
command -v go >/dev/null || { echo 'Go is required by the explicitly limited chart contract renderer.' >&2; exit 1; }
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
bash ./all.sh test
bash ./all.sh mvp test
printf '\nPASS: host/offline checks only. Real Helm, images, DB I/O and recovery are NOT certified.\n'
