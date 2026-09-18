#!/usr/bin/env bash
# Host-only validation. This never starts a database or alters container volumes.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
command -v python3 >/dev/null 2>&1 || { echo 'ERROR: Python 3.8+ required.' >&2; exit 127; }
exec python3 tests/check_host.py "$@"
