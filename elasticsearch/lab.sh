#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
source "$ROOT/scripts/common.sh"

usage() {
  cat <<'EOF'
Elasticsearch 5-node lab controller

Usage:
  ./lab.sh <command> [options]

Lifecycle:
  doctor|check       Check host prerequisites
  up                 Start the cluster and Cerebro
  down [--purge --yes]
  status             Show cluster, node, and shard status
  logs [service...]  Follow Compose logs
  compose <args...>  Run the configured Compose provider

Data and queries:
  seed [options]     Generate and load the lab data
  verify             Verify seeded data and cluster layout
  size               Show dataset storage sizes
  purge --yes        Delete only the three lab seed indices
  query [args...]    Run a catalog query example (use "query list")
  pit [options]      Run PIT/search_after pagination example
  load [options]     Run the live write load
  reset              Restore lab cluster settings without deleting data

Operations:
  fault [args...]    Run the managed fault-lab controller
  scenario <name>    Run a shard/topology scenario
  offline-tests      Run shell and Python offline tests
  help               Show this help

Scenario names are the script names without .sh, for example:
  scenario 01-manual-shard-move
  scenario node-failure-and-recovery --test --yes
  scenario scale-out-node --remove
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

scenario_script() {
  local requested="${1:-}"
  local candidate
  [[ -n "$requested" ]] || {
    echo "[error] scenario requires a name; use: ./lab.sh scenario <name>" >&2
    exit 2
  }
  if [[ "$requested" == "list" ]]; then
    find "$ROOT/scenarios" -maxdepth 1 -type f -name '*.sh' -printf '%f\n' | sort
    return
  fi
  candidate="$ROOT/scenarios/$requested"
  [[ "$candidate" == *.sh ]] || candidate+=".sh"
  if [[ ! -f "$candidate" ]]; then
    candidate="$(find "$ROOT/scenarios" -maxdepth 1 -type f \
      -name "*-${requested}.sh" -print -quit)"
  fi
  [[ -n "$candidate" && -f "$candidate" ]] || {
    echo "[error] Unknown scenario: $requested" >&2
    echo "Available scenarios:" >&2
    find "$ROOT/scenarios" -maxdepth 1 -type f -name '*.sh' -printf '  %f\n' \
      | sort >&2
    exit 2
  }
  run_script "$candidate" "${@:2}"
}

command="${1:-help}"
shift || true

case "$command" in
  help|-h|--help) usage ;;
  doctor|check) run_script "$ROOT/scripts/00-doctor.sh" "$@" ;;
  up|start) run_script "$ROOT/scripts/01-up.sh" "$@" ;;
  down|stop) run_script "$ROOT/scripts/02-down.sh" "$@" ;;
  status|ps) run_script "$ROOT/scripts/03-status.sh" "$@" ;;
  seed) run_python generate_and_load.py "$@" ;;
  reset) run_script "$ROOT/scripts/05-reset-cluster-settings.sh" "$@" ;;
  purge) run_script "$ROOT/scripts/06-purge-lab-indices.sh" "$@" ;;
  size|dataset-size) run_script "$ROOT/scripts/07-dataset-size.sh" "$@" ;;
  query|queries) run_python query_examples.py "$@" ;;
  verify) run_python verify_seed.py "$@" ;;
  pit|pagination) run_python pit_pagination.py "$@" ;;
  load|live-load) run_python live_load.py "$@" ;;
  offline-tests|test) run_script "$ROOT/scripts/12-offline-tests.sh" "$@" ;;
  fault|fault-lab) run_python fault_lab.py "$@" ;;
  scenario) scenario_script "$@" ;;
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
