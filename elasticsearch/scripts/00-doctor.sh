#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
install_missing=false
if [[ "${1:-}" == '--install-missing' && $# -eq 1 ]]; then
  install_missing=true
elif [[ $# -gt 0 ]]; then
  echo 'Usage: ./lab.sh doctor [--install-missing]' >&2; exit 2
fi
ensure_lab_env
check_exposure
status=0
check_host_packages "$install_missing" || status=1
if command -v python3 >/dev/null; then
  python3 -c 'import sys; assert sys.version_info >= (3,9), "Python 3.9+ required"' || status=1
fi
compose version || status=1
resolve_engine || { echo 'No usable Compose provider (docker, podman, or podman-compose).' >&2; status=1; }
network_info() {
  local net subnet
  net="$(network_name)"
  if network_exists "$net"; then
    subnet="$(network_subnet "$net" || echo 'unknown subnet')"
    printf 'Compose network: %s (%s)\n' "$net" "$subnet"
    if [[ "$subnet" == 'unknown subnet' ]]; then
      printf 'Inspect it manually: %s network inspect %s\n' "$RUNTIME" "$net"
    fi
  else
    printf 'Compose network: %s (created on ./lab.sh up)\n' "$net"
  fi
}
network_info
printf 'Container runtime: %s\n' "$RUNTIME"
printf 'Compose provider: %s\n' "$COMPOSE_PROVIDER"
printf 'Snapshot repository: %s at %s (registered/verified by ./lab.sh up)\n' \
  "$SNAPSHOT_REPO_NAME" "$SNAPSHOT_PATH"
printf 'Security: %s (opt-in; see README for ELASTIC_USERNAME/ELASTIC_PASSWORD)\n' \
  "${XPACK_SECURITY_ENABLED:-false}"
value=$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)
echo "vm.max_map_count=$value (this ES 7.17 lab expects >=262144 with mmap enabled)"
if (( value < 262144 )); then echo 'Set on the host: sudo sysctl -w vm.max_map_count=262144'; status=1; fi
free -h || true
echo '5 ES nodes: default 640 MiB heap each (3.125 GiB total heap), plus native memory/page cache/Cerebro/Kibana.'
echo 'Practical lab guideline: >=8 GiB VM/host RAM, 6 GiB available; 10 GiB+ free disk for images/data.'
echo 'Ports: 9200 (ES), 9000 (Cerebro), 5601 (Kibana). Stop the old lab or change ES_PORT / CEREBRO_PORT / KIBANA_PORT / ES_URL in .env.'
echo 'Use ./lab.sh compose config to check the resolved Compose configuration.'
exit "$status"
