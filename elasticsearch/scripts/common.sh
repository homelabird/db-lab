#!/usr/bin/env bash
set -euo pipefail
LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
CURL="${CURL:-curl}"

compose() {
  local project="${COMPOSE_PROJECT_NAME:-cerebro-seed-lab}"
  case "${COMPOSE_PROVIDER:-auto}" in
    podman-compose) podman-compose -p "$project" "$@" ;;
    podman) podman compose -p "$project" "$@" ;;
    docker) docker compose -p "$project" "$@" ;;
    auto)
      if command -v podman-compose >/dev/null 2>&1; then podman-compose -p "$project" "$@"
      elif command -v podman >/dev/null 2>&1; then podman compose -p "$project" "$@"
      elif command -v docker >/dev/null 2>&1; then docker compose -p "$project" "$@"
      else echo '[error] Install podman + podman-compose, or Docker Compose.' >&2; return 1; fi ;;
    *) echo '[error] COMPOSE_PROVIDER=auto|podman-compose|podman|docker' >&2; return 1 ;;
  esac
}

guard_lab() { python3 "$LAB_ROOT/scripts/lablib.py" guard; }
wait_es() { python3 "$LAB_ROOT/scripts/lablib.py" wait --nodes "${1:-1}" --seconds "${WAIT_SECONDS:-300}"; }
es() {
  local method="$1" path="$2" data="${3:-}"
  if [[ "$method" != GET && "$method" != HEAD ]]; then guard_lab; fi
  if [[ -n "$data" ]]; then
    "$CURL" --globoff --fail-with-body -sS --connect-timeout 5 --max-time 130 -X "$method" "$ES_URL$path" -H 'Content-Type: application/json' --data-binary "$data"
  else
    "$CURL" --globoff --fail-with-body -sS --connect-timeout 5 --max-time 130 -X "$method" "$ES_URL$path"
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
    printf '[warning] Unauthenticated legacy lab: bind=%s; ES=%s Cerebro=%s Kibana=%s; no TLS.\n' \
      "$bind" "${ES_PORT:-9200}" "${CEREBRO_PORT:-9000}" "${KIBANA_PORT:-5601}" >&2
    [[ "${ES_ALLOW_PUBLIC_BIND:-no}" == yes ]] || {
      echo '[error] Refusing remote binding. Use 127.0.0.1 (SSH tunnel), or explicitly set ES_ALLOW_PUBLIC_BIND=yes on an isolated network.' >&2
      return 1
    }
  fi
}
