#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
source "$ROOT/scripts/common.sh"

usage() {
  cat <<'EOF'
Elasticsearch lab controller

Usage:
  ./lab.sh <command> [options]

Lifecycle:
  doctor|check       Check host prerequisites
  up|start           Start Elasticsearch, Cerebro, and Kibana
  demo               Start → seed smoke data → verify everything
  down [--purge --yes]
  status|ps          Show cluster, node, and shard status
  ui                 Print browser URLs
  logs [service...]  Follow Compose logs
  compose <args...>  Run the configured Compose provider
  k8s <subcommand>   Deploy, verify, and upgrade the Kubernetes lab
  helm <args...>      Run Helm against ./helm/elasticsearch-lab

Data and queries:
  seed [options]     Generate and load the lab data
  verify             Verify seeded data and cluster layout
  size               Show dataset storage sizes
  purge --yes        Delete only the five lab seed indices
  query [args...]    Run a catalog query example (no args: list)
  pit [options]      Run PIT/search_after pagination example
  load [options]     Run the live write load
  reset              Restore lab cluster settings without deleting data

Operations:
  fault [args...]    Run the managed fault-lab controller
  scenario <name>    Run a shard/topology scenario (use "scenario list")
  offline-tests      Run shell and Python offline tests
  help               Show this help

Quick start:
  ./lab.sh demo                 # safe 5MiB smoke dataset
  ./lab.sh seed                 # default 100MiB dataset
  ./lab.sh verify
  ./lab.sh ui

Scenario names are the script names without .sh:
  scenario list
  scenario 01
  scenario 01-manual-shard-move
  scenario manual-shard-move
  scenario node-failure-and-recovery --test --yes
  scenario scale-out-node --remove
  scenario list
  k8s apply
  k8s upgrade docker.elastic.co/elasticsearch/elasticsearch:7.17.29

Typical seed workflow:
  ./lab.sh up
  ./lab.sh seed --size-mb 5       # quick smoke test
  ./lab.sh seed --size-mb 100 --max-docs-per-shard 10000 --recreate --yes
  ./lab.sh seed --size-mb 100 --max-source-mb-per-shard 2 --recreate --yes
  ./lab.sh seed                   # default 100 MiB synthetic dataset
  ./lab.sh verify                 # counts, mappings, shards, and queries
  ./lab.sh size                   # current Lucene store size

seed creates five deterministic indices with different mappings and shard layouts.

The reported "source" size is compact JSON _source bytes, not disk usage.
The final manifest is written to reports/seed-manifest.json.
EOF
}

demo() {
  bash "$ROOT/scripts/01-up.sh"
  python3 "$ROOT/scripts/generate_and_load.py" --size-mb 5
  python3 "$ROOT/scripts/verify_seed.py"
}

ui() {
  cat <<EOF
Elasticsearch: ${ES_URL}
Cerebro:       http://127.0.0.1:${CEREBRO_PORT:-9000}
Kibana:        http://127.0.0.1:${KIBANA_PORT:-5601}
Kibana Console: http://127.0.0.1:${KIBANA_PORT:-5601}/app/dev_tools#/console
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
  if [[ "$requested" == "list" || "$requested" == "help" || "$requested" == "--help" ]]; then
    if [[ "$requested" != "list" ]]; then
      cat <<'EOF'
Usage:
  ./lab.sh scenario list
  ./lab.sh scenario <number|name> [options]

Examples:
  ./lab.sh scenario 01
  ./lab.sh scenario manual-shard-move
  ./lab.sh scenario 06 --test --yes
  ./lab.sh scenario scale-out-node --remove

Scenario modes:
  01, 04, 09, 12, 14  Read/observe or manual workflows
  02, 03, 05, 06, 07, 08, 10, 11  Fault drills; use --test --yes
  13                   Add es06, or remove it with --remove

The numbered shell files remain compatibility entrypoints. Prefer this
command for stable argument handling and run `scenario list` for names.
EOF
    fi
    while IFS= read -r candidate; do
      name="${candidate##*/}"; name="${name%.sh}"
      case "$name" in
        01-*) description="manual shard move" ;;
        02-*) description="drain a node" ;;
        03-*) description="too many replicas" ;;
        04-*) description="allocation explain" ;;
        05-*) description="impossible allocation filter" ;;
        06-*) description="node failure and recovery" ;;
        07-*) description="disable allocation" ;;
        08-*) description="zone awareness" ;;
        09-*) description="cancel replica recovery" ;;
        10-*) description="rebalance control" ;;
        11-*) description="disk watermark" ;;
        12-*) description="primary vs replica" ;;
        13-*) description="scale out es06" ;;
        14-*) description="relocation under write load" ;;
        15-*) description="Kubernetes rolling upgrade" ;;
        *) description="scenario" ;;
      esac
      printf '%-38s %s\n' "$name" "$description"
    done < <(find "$ROOT/scenarios" -maxdepth 1 -type f -name '*.sh' -printf '%f\n' | sort)
    return
  fi
  if [[ "$requested" =~ ^[0-9]{1,2}$ ]]; then
    requested="$(printf '%02d' "$((10#$requested))")"
  fi
  candidate="$ROOT/scenarios/$requested"
  [[ "$candidate" == *.sh ]] || candidate+=".sh"
  if [[ ! -f "$candidate" ]]; then
    candidate="$(find "$ROOT/scenarios" -maxdepth 1 -type f \
      \( -name "${requested}-*.sh" -o -name "*-${requested}.sh" \) -print -quit)"
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
  demo|quickstart) demo ;;
  ui|urls|open) ui ;;
  doctor|check) run_script "$ROOT/scripts/00-doctor.sh" "$@" ;;
  up|start) run_script "$ROOT/scripts/01-up.sh" "$@" ;;
  down|stop) run_script "$ROOT/scripts/02-down.sh" "$@" ;;
  status|ps) run_script "$ROOT/scripts/03-status.sh" "$@" ;;
  seed) run_python generate_and_load.py "$@" ;;
  reset) run_script "$ROOT/scripts/05-reset-cluster-settings.sh" "$@" ;;
  purge) run_script "$ROOT/scripts/06-purge-lab-indices.sh" "$@" ;;
  size|dataset-size) run_script "$ROOT/scripts/07-dataset-size.sh" "$@" ;;
  query|queries)
    if [[ "${1:-}" == "" ]]; then
      run_python query_examples.py list
    else
      run_python query_examples.py "$@"
    fi
    ;;
  verify) run_python verify_seed.py "$@" ;;
  pit|pagination) run_python pit_pagination.py "$@" ;;
  load|live-load) run_python live_load.py "$@" ;;
  offline-tests|test) run_script "$ROOT/scripts/12-offline-tests.sh" "$@" ;;
  fault|fault-lab) run_python fault_lab.py "$@" ;;
  scenario) scenario_script "$@" ;;
  k8s) run_script "$ROOT/scripts/k8s_lab.sh" "$@" ;;
  helm) command -v helm >/dev/null 2>&1 || { echo '[error] helm is required' >&2; exit 127; }; helm "$@" ;;
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
