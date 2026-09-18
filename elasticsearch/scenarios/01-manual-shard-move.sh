#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
wait_es >/dev/null
exec python3 ./scripts/move_shard.py
