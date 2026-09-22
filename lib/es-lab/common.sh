#!/usr/bin/env bash
set -euo pipefail
# Shared bash core for the Elasticsearch labs (elasticsearch/ = 7.x legacy,
# elasticsearch-9/ = 9.x). Version-neutral engine, network, container,
# diagnostic, snapshot and HTTP helpers.
#
# Required before sourcing: LAB_ROOT (lab directory that owns .env) and
# LAB_SCRIPTS (directory containing the lab's lablib.py). Each lab's
# scripts/common.sh sets them and sources this file.
[[ -n "${LAB_ROOT:-}" ]] || { echo '[error] lib/es-lab/common.sh requires LAB_ROOT to be set by the lab loader.' >&2; return 1; }
LAB_SCRIPTS="${LAB_SCRIPTS:-$LAB_ROOT/scripts}"
export LAB_SCRIPTS

# Simple KEY=value config, not executable shell. Existing exported env wins.
if [[ -f "$LAB_ROOT/.env" ]]; then
  while IFS='=' read -r key value || [[ -n "${key:-}" ]]; do
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
    [[ -v "$key" ]] && continue
    value="${value%$'\r'}"
    if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then value="${value:1:${#value}-2}"; fi
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$LAB_ROOT/.env"
fi
export ES_URL="${ES_URL:-http://127.0.0.1:9200}"
export LAB_CLUSTER_NAME="${LAB_CLUSTER_NAME:-cerebro-shard-lab}"
export SNAPSHOT_REPO_NAME="${SNAPSHOT_REPO_NAME:-lab-snapshots}"
export SNAPSHOT_PATH="${SNAPSHOT_PATH:-/usr/share/elasticsearch/snapshots}"
# Space-separated Elasticsearch nodes (default: the 5-node topology both labs use).
LAB_ES_NODES="${LAB_ES_NODES:-es01 es02 es03 es04 es05}"
# Fallback container-name prefix for labs that hardcode container_name in compose.
LAB_CONTAINER_PREFIX="${LAB_CONTAINER_PREFIX:-}"
CURL="${CURL:-curl}"

# Resolve the Compose provider and the underlying container runtime.
# COMPOSE_PROVIDER: auto|docker|podman|podman-compose
# RUNTIME: docker|podman
resolve_engine() {
  case "${COMPOSE_PROVIDER:-auto}" in
    auto)
      if command -v podman-compose >/dev/null 2>&1; then
        COMPOSE_PROVIDER=podman-compose; RUNTIME=podman
      elif command -v podman >/dev/null 2>&1; then
        COMPOSE_PROVIDER=podman; RUNTIME=podman
      elif command -v docker >/dev/null 2>&1; then
        COMPOSE_PROVIDER=docker; RUNTIME=docker
      else
        echo '[error] No Compose provider found: install podman + podman-compose, or Docker Compose.' >&2
        return 1
      fi ;;
    docker) COMPOSE_PROVIDER=docker; RUNTIME=docker ;;
    podman) COMPOSE_PROVIDER=podman; RUNTIME=podman ;;
    podman-compose) COMPOSE_PROVIDER=podman-compose; RUNTIME=podman ;;
    *) echo '[error] COMPOSE_PROVIDER=auto|docker|podman|podman-compose' >&2; return 1 ;;
  esac
  export COMPOSE_PROVIDER RUNTIME
}

compose() {
  resolve_engine
  local project="${COMPOSE_PROJECT_NAME:-es-lab}"
  case "$COMPOSE_PROVIDER" in
    docker) docker compose -p "$project" "$@" ;;
    podman) podman compose -p "$project" "$@" ;;
    podman-compose) podman-compose -p "$project" "$@" ;;
    *) echo '[error] No Compose provider resolved.' >&2; return 1 ;;
  esac
}

network_name() { printf '%s_es-lab\n' "${COMPOSE_PROJECT_NAME:-es-lab}"; }

network_exists() {
  local net="${1:-$(network_name)}"
  case "${RUNTIME:-}" in
    docker) docker network inspect "$net" >/dev/null 2>&1 ;;
    podman) podman network exists "$net" ;;
    *) return 1 ;;
  esac
}

# Print the first IPv4 subnet of the Compose network, or nothing.
network_subnet() {
  local net="${1:-$(network_name)}" subnet
  case "${RUNTIME:-}" in
    docker) subnet="$(docker network inspect "$net" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null)" ;;
    podman) subnet="$(podman network inspect "$net" --format '{{range .Subnets}}{{.Subnet}} {{end}}' 2>/dev/null)" ;;
    *) return 1 ;;
  esac
  [[ -n "${subnet:-}" ]] || return 1
  subnet="${subnet%% *}"
  [[ "$subnet" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] || return 1
  printf '%s\n' "$subnet"
}

container_exists() {
  case "${RUNTIME:-}" in
    docker) docker inspect "$1" >/dev/null 2>&1 ;;
    podman) podman container exists "$1" ;;
    *) return 1 ;;
  esac
}

# Resolve the actual Compose container name for a service: prefer the
# project-prefixed convention, then a lab container_name prefix.
container_name() {
  local svc="${1:-es01}" cand
  for cand in "${COMPOSE_PROJECT_NAME:-es-lab}-$svc" "${LAB_CONTAINER_PREFIX}$svc"; do
    if container_exists "$cand"; then printf '%s\n' "$cand"; return 0; fi
  done
  printf '%s\n' "${COMPOSE_PROJECT_NAME:-es-lab}-$svc"
}

container_running() {
  local name state
  name="$(container_name "$1")"
  case "${RUNTIME:-}" in
    docker) state="$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" ;;
    podman) state="$(podman inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" ;;
    *) return 1 ;;
  esac
  [[ "$state" == true ]]
}

eng_exec() {
  case "${RUNTIME:-}" in
    docker) docker exec "$@" ;;
    podman) podman exec "$@" ;;
    *) return 1 ;;
  esac
}

# Distinguish an Elasticsearch startup problem from blocked inter-container
# traffic, and print an executable host firewall hint with the real subnet.
es_network_diagnose() {
  local net="${1:-$(network_name)}" subnet="" node_url code
  local -a nodes
  read -r -a nodes <<< "$LAB_ES_NODES"
  echo ''
  echo '[error] The Elasticsearch cluster did not become ready.'
  printf '[info] Container runtime: %s\n' "${RUNTIME:-unknown}"
  printf '[info] Compose provider: %s\n' "${COMPOSE_PROVIDER:-unknown}"
  if network_exists "$net"; then
    subnet="$(network_subnet "$net" 2>/dev/null || true)"
    printf '[info] Compose network: %s (%s)\n' "$net" "${subnet:-unknown subnet}"
  else
    printf '[info] Compose network: %s (not created)\n' "$net"
  fi
  echo ''
  echo '--- Step 1: are the Elasticsearch containers running? ---'
  for name in "${nodes[@]}"; do
    if container_running "$name"; then
      printf '[ok]   %s is running\n' "$(container_name "$name")"
    else
      printf '[error] %s is NOT running\n' "$(container_name "$name")"
    fi
  done
  echo ''
  echo '--- Step 2: is Elasticsearch listening inside each container? ---'
  code=0
  for name in "${nodes[@]}"; do
    if container_running "$name"; then
      if eng_exec "$(container_name "$name")" curl -fsS --max-time 5 http://localhost:9200/ >/dev/null 2>&1; then
        printf '[ok]   %s localhost:9200 answers (HTTP is up inside the container)\n' "$name"
      else
        printf '[error] %s localhost:9200 does NOT answer\n' "$name"
        printf '[hint]   Elasticsearch is still bootstrapping or exited: ./lab.sh logs %s\n' "$name"
        code=1
      fi
    fi
  done
  if (( code == 0 )); then
    printf '[ok]   Elasticsearch is listening inside every running container.\n'
  fi
  echo ''
  echo '--- Step 3: can containers reach each other over the Compose network? ---'
  if (( ${#nodes[@]} >= 2 )) && container_running "${nodes[0]}" && container_running "${nodes[1]}"; then
    if eng_exec "$(container_name "${nodes[0]}")" curl -fsS --max-time 5 "http://${nodes[1]}:9200/_cluster/health" >/dev/null 2>&1; then
      printf '[ok]   %s can reach %s:9200 on the Compose network.\n' "${nodes[0]}" "${nodes[1]}"
    else
      printf '[error] %s cannot connect to %s:9200.\n' "${nodes[0]}" "${nodes[1]}"
      printf '[error] Cluster formation is blocked even though localhost:9200 works.\n'
      if [[ -n "$subnet" ]]; then
        echo '[hint] Allow container-to-container FORWARD traffic in the host firewall:'
        printf '       sudo iptables-legacy -I FORWARD 1 -s %s -d %s -j ACCEPT\n' "$subnet" "$subnet"
        echo '[hint] Your host may instead use nftables or firewalld. Confirm with: sudo iptables -t filter -L FORWARD -n'
      else
        echo '[hint] The subnet could not be read (see "unknown subnet" above). Inspect the network with:'
        printf '       %s network inspect %s\n' "$RUNTIME" "$net"
      fi
      code=1
    fi
  fi
  echo ''
  echo '--- Step 4: published host port ---'
  if curl -fsS --max-time 3 "http://127.0.0.1:${ES_PORT:-9200}/" >/dev/null 2>&1; then
    printf '[ok]   Host can reach 127.0.0.1:%s (published ES port).\n' "${ES_PORT:-9200}"
  else
    printf '[error] Host cannot reach 127.0.0.1:%s.\n' "${ES_PORT:-9200}"
  fi
  printf '[hint] Elasticsearch logs: ./lab.sh logs %s\n' "${nodes[*]}"
  printf '[hint] Host prerequisites: ./lab.sh doctor\n'
  return 1
}

guard_lab() { python3 "$LAB_SCRIPTS/lablib.py" guard; }
wait_es() { python3 "$LAB_SCRIPTS/lablib.py" wait --nodes "${1:-1}" --seconds "${WAIT_SECONDS:-300}"; }
ensure_snapshot_repo() { python3 "$LAB_SCRIPTS/lablib.py" snapshot-repo; }
es() {
  local method="$1" path="$2" data="${3:-}" auth=()
  if [[ "$method" != GET && "$method" != HEAD ]]; then guard_lab; fi
  if [[ "${XPACK_SECURITY_ENABLED:-false}" == true ]]; then
    auth=("-u" "${ELASTIC_USERNAME:-elastic}:${ELASTIC_PASSWORD:-}")
  fi
  if [[ -n "$data" ]]; then
    "$CURL" --globoff --fail-with-body -sS --connect-timeout 5 --max-time 130 ${auth[@]+"${auth[@]}"} -X "$method" "$ES_URL$path" -H 'Content-Type: application/json' --data-binary "$data"
  else
    "$CURL" --globoff --fail-with-body -sS --connect-timeout 5 --max-time 130 ${auth[@]+"${auth[@]}"} -X "$method" "$ES_URL$path"
  fi
}
pretty() {
  python3 -c 'import sys,json; text=sys.stdin.read()
try: print(json.dumps(json.loads(text),ensure_ascii=False,indent=2))
except ValueError: print(text,end="")'
}

# Does not change existing settings or host firewall rules.
check_exposure() {
  local bind=${ES_BIND_IP:-127.0.0.1}
  python3 - "$bind" <<'PY_IP'
import ipaddress, sys
try:
    address = ipaddress.ip_address(sys.argv[1])
    if address.version != 4: raise ValueError('only IPv4 publishing is supported')
except ValueError as exc:
    sys.exit('[error] ES_BIND_IP: ' + str(exc))
PY_IP
  if [[ "$bind" != 127.* ]]; then
    printf '[warning] Unauthenticated lab: bind=%s; ES=%s; no TLS by default.\n' \
      "$bind" "${ES_PORT:-9200}" >&2
    [[ "${ES_ALLOW_PUBLIC_BIND:-no}" == yes ]] || {
      echo '[error] Refusing remote binding. Use 127.0.0.1 (SSH tunnel), or explicitly set ES_ALLOW_PUBLIC_BIND=yes on an isolated network.' >&2
      return 1
    }
  fi
}

resolve_engine