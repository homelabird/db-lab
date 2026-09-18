#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
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
echo 'Data is NOT seeded automatically. Next: ./scripts/04-seed-data.sh'
