#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh

map_count=$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)
if ! [[ "$map_count" =~ ^[0-9]+$ ]] || (( map_count < 262144 )); then
  cat >&2 <<EOF
[error] Elasticsearch requires vm.max_map_count >= 262144.
[error] Current value: ${map_count}
[hint] Run: sudo sysctl -w vm.max_map_count=262144
[hint] Persist it with: echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-elasticsearch.conf
EOF
  exit 1
fi

compose up -d
python3 ./scripts/lablib.py wait --nodes 5 --yellow --seconds "${WAIT_SECONDS:-300}"
guard_lab
./scripts/03-status.sh
printf '\nHost port binding: %s (Elasticsearch %s, Cerebro %s)\n' "${ES_BIND_IP:-0.0.0.0}" "${ES_PORT:-9200}" "${CEREBRO_PORT:-9000}"
printf 'Local script Elasticsearch URL: %s\n' "$ES_URL"
if [[ "${ES_BIND_IP:-0.0.0.0}" == "0.0.0.0" ]]; then
  printf 'Remote Cerebro: http://<SERVER_IP>:%s\nRemote Elasticsearch: http://<SERVER_IP>:%s\n' "${CEREBRO_PORT:-9000}" "${ES_PORT:-9200}"
  echo 'WARNING: No authentication/TLS. Restrict these ports to trusted lab clients; do not expose them to the Internet.'
else
  printf 'Cerebro: http://%s:%s\nElasticsearch: http://%s:%s\n' "${ES_BIND_IP}" "${CEREBRO_PORT:-9000}" "${ES_BIND_IP}" "${ES_PORT:-9200}"
fi
echo 'Data is NOT seeded automatically. Next: ./lab.sh seed'
