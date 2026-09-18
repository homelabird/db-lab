#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
# --generate-only must not wait for / contact Elasticsearch.
exec python3 ./scripts/generate_and_load.py "$@"
