#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
status=0
for bin in python3 curl; do command -v "$bin" || status=1; done
python3 -c 'import sys; assert sys.version_info >= (3,9), "Python 3.9+ required"' || status=1
compose version || status=1
value=$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)
echo "vm.max_map_count=$value (this ES 7.17 lab expects >=262144 with mmap enabled)"
if (( value < 262144 )); then echo 'Set on the host: sudo sysctl -w vm.max_map_count=262144'; status=1; fi
free -h || true
echo '5 ES nodes: default 512 MiB heap each (2.5 GiB total heap), plus native memory/page cache/Cerebro.'
echo 'Practical lab guideline: >=8 GiB VM/host RAM, 6 GiB available; 10 GiB+ free disk for images/data.'
echo 'Ports: 9200 (ES), 9000 (UI). Stop the old lab or change ES_PORT / CEREBRO_PORT / ES_URL in .env.'
echo 'Use ./lab.sh compose config to check the resolved Compose configuration.'
exit "$status"
