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

# Keep the caller's exported values above .env, but allow a newly generated
# .env to replace defaults initialized later in this file.
declare -Ag LAB_CALLER_ENV=()
while IFS= read -r key; do LAB_CALLER_ENV["$key"]=1; done < <(compgen -e)

load_lab_env() {
  # Simple KEY=value config, not executable shell. Caller environment wins.
  while IFS='=' read -r key value || [[ -n "${key:-}" ]]; do
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
    [[ ${LAB_CALLER_ENV[$key]+set} ]] && continue
    value="${value%$'\r'}"
    if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then value="${value:1:${#value}-2}"; fi
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$LAB_ROOT/.env"
}
[[ -f "$LAB_ROOT/.env" ]] && load_lab_env
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
  local net="${1:-$(network_name)}" subnet inspect
  case "${RUNTIME:-}" in
    docker) subnet="$(docker network inspect "$net" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null)" ;;
    podman)
      subnet="$(podman network inspect "$net" --format '{{range .Subnets}}{{.Subnet}} {{end}}' 2>/dev/null || true)"
      if [[ -z "$subnet" ]]; then
        inspect="$(podman network inspect "$net" 2>/dev/null || true)"
        subnet="$(python3 -c 'import ipaddress,json,sys
def walk(v):
 if isinstance(v,dict):
  for k,x in v.items():
   if k.lower() in ("subnet","ipnet") and isinstance(x,str):
    try: yield str(ipaddress.ip_interface(x).network)
    except ValueError: pass
   yield from walk(x)
 elif isinstance(v,list):
  for x in v: yield from walk(x)
try:
 values=list(dict.fromkeys(walk(json.load(sys.stdin))))
 print(values[0] if values else "")
except (ValueError,TypeError): print("")' <<<"$inspect")"
      fi
      ;;
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
  local net="${1:-$(network_name)}" subnet="" code node_missing=0
  local -a nodes
  read -r -a nodes <<< "$LAB_ES_NODES"
  echo ''
  echo '[error] The Elasticsearch cluster did not become ready.'
  printf '[ok] Runtime: %s\n' "${RUNTIME:-unknown}"
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
      node_missing=1
    fi
  done
  echo ''
  echo '--- Step 2: is Elasticsearch listening inside each container? ---'
  code="$node_missing"
  for name in "${nodes[@]}"; do
    if container_running "$name"; then
      if eng_exec "$(container_name "$name")" curl -sS --max-time 5 http://localhost:9200/ >/dev/null 2>&1; then
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
  if (( code != 0 )); then
    echo '[info] Skipping network/firewall diagnosis because a node is not running or its local HTTP listener is unavailable.'
  elif (( ${#nodes[@]} >= 2 )) && container_running "${nodes[0]}" && container_running "${nodes[1]}"; then
    if eng_exec "$(container_name "${nodes[0]}")" curl -sS --max-time 5 "http://${nodes[1]}:9200/" >/dev/null 2>&1; then
      printf '[ok]   %s can reach %s:9200 on the Compose network.\n' "${nodes[0]}" "${nodes[1]}"
    else
      printf '[error] %s cannot connect to %s:9200.\n' "${nodes[0]}" "${nodes[1]}"
      printf '[error] Container-to-container connection failed while Elasticsearch is listening locally.\n'
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
  if curl -sS --max-time 3 "http://127.0.0.1:${ES_PORT:-9200}/" >/dev/null 2>&1; then
    printf '[ok]   Host can reach 127.0.0.1:%s (published ES port).\n' "${ES_PORT:-9200}"
  else
    printf '[error] Host cannot reach 127.0.0.1:%s.\n' "${ES_PORT:-9200}"
  fi
  printf '[hint] Elasticsearch logs: ./lab.sh logs %s\n' "${nodes[*]}"
  printf '[hint] Host prerequisites: ./lab.sh doctor\n'
  return 1
}

ensure_lab_env() {
  [[ -f "$LAB_ROOT/.env" ]] && return 0
  if [[ ! -f "$LAB_ROOT/.env.example" ]]; then
    printf '[error] Missing both %s/.env and its template .env.example\n' "$LAB_ROOT" >&2
    return 1
  fi
  (umask 077; cp -- "$LAB_ROOT/.env.example" "$LAB_ROOT/.env")
  load_lab_env
  printf '[info] Created %s/.env from .env.example (mode 600).\n' "$LAB_ROOT"
}

host_package_manager() {
  local id=""
  [[ -r /etc/os-release ]] && . /etc/os-release && id="${ID:-} ${ID_LIKE:-}"
  case " $id " in
    *debian*|*ubuntu*) command -v apt-get >/dev/null && { echo apt-get; return; } ;;
    *fedora*|*rhel*|*centos*|*rocky*|*almalinux*)
      command -v dnf >/dev/null && { echo dnf; return; }
      command -v yum >/dev/null && { echo yum; return; } ;;
    *alpine*) command -v apk >/dev/null && { echo apk; return; } ;;
    *arch*|*manjaro*) command -v pacman >/dev/null && { echo pacman; return; } ;;
    *opensuse*|*suse*) command -v zypper >/dev/null && { echo zypper; return; } ;;
  esac
  for manager in apt-get dnf yum apk pacman zypper; do
    command -v "$manager" >/dev/null && { echo "$manager"; return; }
  done
  return 1
}

install_host_packages() {
  local manager="$1"; shift
  local -a sudo_cmd=()
  if (( EUID != 0 )); then
    command -v sudo >/dev/null || { echo '[error] sudo is required to install host packages.' >&2; return 1; }
    sudo_cmd=(sudo)
  fi
  printf '[info] Installing host packages with %s: %s\n' "$manager" "$*"
  case "$manager" in
    apt-get) "${sudo_cmd[@]}" apt-get update && "${sudo_cmd[@]}" apt-get install -y "$@" ;;
    dnf) "${sudo_cmd[@]}" dnf install -y "$@" ;;
    yum) "${sudo_cmd[@]}" yum install -y "$@" ;;
    apk) "${sudo_cmd[@]}" apk add --no-cache "$@" ;;
    pacman) "${sudo_cmd[@]}" pacman -Sy --needed --noconfirm "$@" ;;
    zypper) "${sudo_cmd[@]}" zypper --non-interactive install "$@" ;;
    *) echo "[error] Unsupported package manager: $manager" >&2; return 1 ;;
  esac
}

check_host_packages() {
  local install_missing="${1:-false}" manager package required_count
  local -a packages=() required=()
  command -v python3 >/dev/null || { packages+=(python3); required+=(python3); }
  command -v curl >/dev/null || { packages+=(curl); required+=(curl); }
  if ! command -v iptables >/dev/null && ! command -v iptables-legacy >/dev/null; then
    printf '[hint] iptables is not installed; firewall diagnosis requires it only if container networking is blocked.\n'
    packages+=(iptables)
  fi
  (( ${#packages[@]} == 0 )) && return 0
  required_count="${#required[@]}"
  if [[ "$install_missing" != true ]]; then
    (( required_count == 0 )) || printf '[hint] Install missing required tools with: ./lab.sh doctor --install-missing\n'
    (( required_count == 0 )) && printf '[hint] Optional firewall tool can be installed with: ./lab.sh doctor --install-missing\n'
    if (( required_count > 0 )); then return 1; fi
    return 0
  fi
  manager="$(host_package_manager)" || {
    echo '[error] Could not detect a supported package manager (apt, dnf, yum, apk, pacman, zypper).' >&2
    echo "[hint] Install these packages manually: ${packages[*]}" >&2
    return 1
  }
  case "$manager" in
    pacman)
      local -a mapped=()
      for package in "${packages[@]}"; do
        [[ "$package" != python3 ]] || package=python
        mapped+=("$package")
      done
      packages=("${mapped[@]}")
      ;;
  esac
  install_host_packages "$manager" "${packages[@]}" || return 1
  for package in python3 curl; do
    if ! command -v "$package" >/dev/null; then
      printf '[error] %s is still unavailable after package installation.\n' "$package" >&2
      return 1
    fi
  done
}

check_kibana_auth() {
  local key value
  for key in XPACK_SECURITY_ENABLED KIBANA_STACK_MONITORING_ENABLED KIBANA_STACK_MANAGEMENT_ENABLED; do
    value="${!key:-false}"
    [[ "$value" == true || "$value" == false ]] || {
      printf '[error] %s must be exactly true or false (got %s).\n' "$key" "$value" >&2
      return 1
    }
  done
  if [[ "${KIBANA_STACK_MANAGEMENT_ENABLED:-true}" == false && "${XPACK_SECURITY_ENABLED:-false}" != true ]]; then
    echo '[error] Hiding Stack Management requires XPACK_SECURITY_ENABLED=true so Kibana can enforce role permissions.' >&2
    echo '[hint] Set XPACK_SECURITY_ENABLED=true, ELASTIC_PASSWORD, KIBANA_PASSWORD, and KIBANA_LOGIN_PASSWORD in .env.' >&2
    return 1
  fi
  [[ "${XPACK_SECURITY_ENABLED:-false}" == true ]] || return 0
  if [[ -z "${ELASTIC_PASSWORD:-}" || -z "${KIBANA_PASSWORD:-}" ||
        -z "${KIBANA_LOGIN_USERNAME:-}" || -z "${KIBANA_LOGIN_PASSWORD:-}" ]]; then
    echo '[error] Secure Kibana startup requires ELASTIC_PASSWORD, KIBANA_PASSWORD, KIBANA_LOGIN_USERNAME, and KIBANA_LOGIN_PASSWORD in .env.' >&2
    return 1
  fi
}

ensure_security_users() {
  [[ "${XPACK_SECURITY_ENABLED:-false}" == true ]] || return 0
  PYTHONPATH="$(cd "$LAB_ROOT/../lib/es-lab" && pwd)${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$LAB_ROOT/../lib/es-lab/security-bootstrap.py"
}

kibana_status_available() {
  local port="${1:-${KIBANA_PORT:-5601}}" body
  local -a auth=()
  if [[ "${XPACK_SECURITY_ENABLED:-false}" == true ]]; then
    auth=(--user "${KIBANA_LOGIN_USERNAME}:${KIBANA_LOGIN_PASSWORD}")
  fi
  body="$(curl -fsS --connect-timeout 2 --max-time 5 "${auth[@]}" \
    "http://127.0.0.1:${port}/api/status" 2>/dev/null)" || return 1
  python3 -c 'import json,sys
d=json.load(sys.stdin)
overall=d.get("status",{}).get("overall",{})
state=overall.get("level") or overall.get("state")
sys.exit(0 if state in ("available", "green") else 1)' <<< "$body"
}

verify_install() {
  local service
  local -a services=(es01 es02 es03 es04 es05 kibana)
  if [[ "${LAB_HAS_CEREBRO:-false}" == true && "${XPACK_SECURITY_ENABLED:-false}" != true ]]; then
    services+=(cerebro)
  fi
  compose config >/dev/null || { echo '[FAIL] Compose configuration is invalid.' >&2; return 1; }
  check_kibana_auth
  for service in "${services[@]}"; do
    if ! container_running "$service"; then
      printf '[FAIL] Container for %s is not running.\n' "$service" >&2
      # podman-compose's `ps` command does not accept a service positional
      # argument; print the whole stack so failure diagnostics work for every
      # supported provider.
      compose ps >&2 || true
      compose logs --tail=80 "$service" >&2 || true
      return 1
    fi
  done
  printf '[PASS] Compose services running: %s\n' "${services[*]}"
  local -a args=(--es-version "$LAB_ES_VERSION" --kibana-port "${KIBANA_PORT:-5601}")
  if [[ "${LAB_HAS_CEREBRO:-false}" == true && "${XPACK_SECURITY_ENABLED:-false}" != true ]]; then
    args+=(--cerebro-port "${CEREBRO_PORT:-9000}")
  fi
  PYTHONPATH="$(cd "$LAB_ROOT/../lib/es-lab" && pwd)${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$LAB_ROOT/../lib/es-lab/verify-install.py" "${args[@]}"
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
