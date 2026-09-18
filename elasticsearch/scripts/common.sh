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
