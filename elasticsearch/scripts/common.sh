#!/usr/bin/env bash
set -euo pipefail
# Legacy 7.x lab loader: exports this lab's location/defaults, then sources the
# shared Elasticsearch core at ../../lib/es-lab/common.sh (engine, network,
# container, diagnostics, snapshot and HTTP helpers).
LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export LAB_ROOT
LAB_SCRIPTS="$LAB_ROOT/scripts"
export LAB_SCRIPTS
LAB_CONTAINER_PREFIX="${LAB_CONTAINER_PREFIX:-cerebro-seed-}"
export LAB_CONTAINER_PREFIX
source "$(cd "$LAB_ROOT/../lib/es-lab" && pwd)/common.sh"