#!/usr/bin/env bash
set -euo pipefail
# Elasticsearch 9 lab loader: exports this lab's location/defaults and sources
# the shared Elasticsearch core at ../../lib/es-lab/common.sh (engine, network,
# container, diagnostics, snapshot and HTTP helpers).
LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export LAB_ROOT
LAB_ES_VERSION=9.5.
LAB_HAS_CEREBRO=false
export LAB_ES_VERSION LAB_HAS_CEREBRO
LAB_SCRIPTS="$LAB_ROOT/scripts"
export LAB_SCRIPTS
source "$(cd "$LAB_ROOT/../lib/es-lab" && pwd)/common.sh"
LAB_CONTAINER_PREFIX="${LAB_CONTAINER_PREFIX:-es9-lab-}"
export LAB_CONTAINER_PREFIX
