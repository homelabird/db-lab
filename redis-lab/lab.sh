#!/usr/bin/env bash
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
command -v python3 >/dev/null 2>&1 || { echo 'python3가 필요합니다.' >&2; exit 1; }
exec python3 "$ROOT/scripts/manage.py" "$@"
