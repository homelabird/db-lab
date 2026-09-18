#!/usr/bin/env bash
# Backward-compatible core acceptance entrypoint. Requires explicit disruptive-test approval.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec bash tests/run-real.sh --suite core "$@"
