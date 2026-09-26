#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
source "$ROOT/scripts/common.sh"

usage() {
  cat <<'EOF'
Elasticsearch 9 lab controller (core stack + shared snapshot repository)

Usage:
  ./lab.sh <command> [options]

Lifecycle:
  doctor|check [--install-missing]
                     Check topology; optionally install python3, curl, and
                     iptables with the detected system package manager
  up|start           Start Elasticsearch 9.5.x (5 nodes) + Kibana 9.5.x
  down [--purge --yes]
  status|ps          Show cluster, node, and shard status
  verify-install     Verify running containers, green cluster, snapshot, Kibana
  ui                 Print browser URLs
  logs [service...]  Follow Compose logs
  compose <args...>  Run the configured Compose provider

Data:
  seed [options]     Generate and load the realistic lab data (same generator
                     and schema as the legacy 7.x lab, shared lib/es-lab/datagen)
  benchmark [options] Run a bounded load and save a comparable JSON report
  verify             Verify seeded data, shard layout and query examples
  size               Show dataset storage sizes
  purge --yes        Delete only the five lab seed indices
  query [args...]    Run a catalog query example (no args: list)
  features [--clean] ES9-only modern features: data stream+ILM, ES|QL,
                     kNN vector search, async search (writes reports/features.json)
  drills <command>   Recoverable ES9 node-outage and master-quorum simulations
  snapshot           (Re)register + verify the shared snapshot repository

Tests:
  offline-tests      Run shell and Python offline tests
  help               Show this help

Notes:
  - Security is OFF by default (unauthenticated, HTTP). Setting
    XPACK_SECURITY_ENABLED=true creates transport TLS certificates; set all
    four passwords/login values in .env first. Stack visibility options:
    KIBANA_STACK_MONITORING_ENABLED and KIBANA_STACK_MANAGEMENT_ENABLED.
  - cluster.initial_master_nodes is injected ONLY on a fresh data volume by the
    entrypoint shim; restarts join via discovery.seed_hosts (ES 8/9 semantics).
  - Ports default to 9201/5602 so this lab can coexist with the legacy 7.x lab
    (9200/9000/5601). Both labs share the lib/es-lab core helpers and the
    realistic seed generator.

Quick start:
  cp .env.example .env        # first run only; edit ports/provider if needed
  ./lab.sh help                 # commands and examples
  ./lab.sh doctor               # check prerequisites and network
  ./lab.sh up                   # start/reuse the ES 9 stack
  ./lab.sh status               # cluster and shard health
  ./lab.sh logs es02            # inspect one service
  ./lab.sh ui                   # print browser URLs
  ./lab.sh down                 # stop while preserving data
  ./lab.sh up
  ./lab.sh doctor
  ./lab.sh snapshot
  ./lab.sh seed --size-mb 5   # quick smoke dataset
  ./lab.sh verify
  ./lab.sh seed               # default 100MiB dataset
  ./lab.sh query list

seed creates the same five deterministic indices as the legacy lab and writes
reports/seed-manifest.json. ES9 drills are bounded node-outage and master-quorum
exercises; they do not modify legacy seed indices and require explicit --yes.
EOF
}

ui() {
  cat <<EOF
Elasticsearch: ${ES_URL}
Kibana:        http://127.0.0.1:${KIBANA_PORT:-5602}
Kibana Console: http://127.0.0.1:${KIBANA_PORT:-5602}/app/dev_tools#/console
EOF
}

run_script() {
  local script="$1"
  shift
  [[ -f "$script" ]] || {
    echo "[error] Script not found: $script" >&2
    exit 127
  }
  exec bash "$script" "$@"
}

run_python() {
  local module="$1"
  shift
  exec python3 "$ROOT/scripts/$module" "$@"
}

command="${1:-help}"
shift || true

case "$command" in
  help|-h|--help) usage ;;
  ui|urls|open) ui ;;
  doctor|check) run_script "$ROOT/scripts/00-doctor.sh" "$@" ;;
  up|start) run_script "$ROOT/scripts/01-up.sh" "$@" ;;
  down|stop) run_script "$ROOT/scripts/02-down.sh" "$@" ;;
  status|ps) run_script "$ROOT/scripts/03-status.sh" "$@" ;;
  verify-install) verify_install ;;
  snapshot|snapshot-repo) ensure_snapshot_repo "$@" ;;
  seed) run_python generate_and_load.py "$@" ;;
  benchmark) python3 "$LAB_ROOT/../lib/es-lab/benchmark.py" "$@" ;;
  verify) run_python verify_seed.py "$@" ;;
  size|dataset-size) run_script "$ROOT/scripts/07-dataset-size.sh" "$@" ;;
  features) run_python features.py "$@" ;;
  drills) run_python drills.py "$@" ;;
  purge) run_script "$ROOT/scripts/06-purge-lab-indices.sh" "$@" ;;
  query|queries)
    if [[ "${1:-}" == "" ]]; then
      run_python query_examples.py list
    else
      run_python query_examples.py "$@"
    fi
    ;;
  offline-tests|test) run_script "$ROOT/scripts/12-offline-tests.sh" "$@" ;;
  compose) compose "$@" ;;
  logs)
    compose logs "$@"
    ;;
  *)
    echo "[error] Unknown command: $command" >&2
    usage >&2
    exit 2
    ;;
esac
