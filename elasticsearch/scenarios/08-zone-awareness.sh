#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
exec python3 ./scripts/fault_legacy.py zone-awareness "$@"
