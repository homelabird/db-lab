#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v python3 >/dev/null 2>&1 || { echo 'ERROR: Python 3.8+ is required on the host.' >&2; exit 127; }
exec python3 scripts/lab.py "$@"
