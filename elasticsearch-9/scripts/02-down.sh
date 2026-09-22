#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
if [[ "${1:-}" == '--purge' ]]; then
  [[ "${2:-}" == '--yes' ]] || { echo 'Deletes all project ES volumes. Use: ./scripts/02-down.sh --purge --yes' >&2; exit 2; }
  compose down -v --remove-orphans
elif [[ $# -eq 0 ]]; then
  compose down --remove-orphans
else
  echo 'Usage: 02-down.sh [--purge --yes]' >&2; exit 2
fi