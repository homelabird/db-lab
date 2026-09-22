#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
check_exposure
status=0
for bin in python3 curl; do command -v "$bin" || status=1; done
python3 -c 'import sys; assert sys.version_info >= (3,9), "Python 3.9+ required"' || status=1
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
printf 'Security: %s (opt-in; security=true additionally requires TLS, see README)\n' \
  "${XPACK_SECURITY_ENABLED:-false}"
value=$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)
echo "vm.max_map_count=$value (elasticsearch 8/9 expects >=262144 with mmap enabled)"
if (( value < 262144 )); then echo 'Set on the host: sudo sysctl -w vm.max_map_count=262144'; status=1; fi
free -h || true
echo 'ES9 stack: 5 ES 9.5.x nodes (default 640 MiB heap each) + Kibana 9.5.x.'
echo 'Practical lab guideline: >=8 GiB VM/host RAM, 6 GiB available; 10 GiB+ free disk for images/data.'
echo 'Ports: 9201 (ES9), 5602 (Kibana9). The legacy 7.x lab uses 9200/9000/5601; both labs can coexist.'
echo 'Bootstrap: cluster.initial_master_nodes is injected ONLY on fresh data volumes; restarts join via seed_hosts.'
echo 'Use ./lab.sh compose config to check the resolved Compose configuration.'
exit "$status"