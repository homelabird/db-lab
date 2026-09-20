#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh

check_exposure

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
# Normal startup never modifies host firewall rules. Network failures are diagnosed
# after application readiness, not by a one-shot request during process startup.
python3 ./scripts/lablib.py wait --nodes 5 --yellow --seconds "${WAIT_SECONDS:-300}"
KIBANA_WAIT_SECONDS="${KIBANA_WAIT_SECONDS:-180}"
kibana_ready=0
for _ in $(seq 1 "$((KIBANA_WAIT_SECONDS / 2))"); do
  if curl -fsS --connect-timeout 2 --max-time 5 \
      "http://127.0.0.1:${KIBANA_PORT:-5601}/api/status" >/dev/null 2>&1; then
    kibana_ready=1
    break
  fi
  sleep 2
done
if (( kibana_ready == 0 )); then
  echo "[error] Kibana did not become ready within ${KIBANA_WAIT_SECONDS}s." >&2
  echo "[hint] Inspect with: ./lab.sh logs kibana" >&2
  exit 1
fi
guard_lab
bash ./lab.sh status
printf '\nHost port binding: %s (Elasticsearch %s, Cerebro %s, Kibana %s)\n' \
  "${ES_BIND_IP:-127.0.0.1}" "${ES_PORT:-9200}" "${CEREBRO_PORT:-9000}" "${KIBANA_PORT:-5601}"
printf 'Local script Elasticsearch URL: %s\n' "$ES_URL"
if [[ "${ES_BIND_IP:-127.0.0.1}" == "0.0.0.0" ]]; then
  printf 'Remote Cerebro: http://<SERVER_IP>:%s\nRemote Kibana: http://<SERVER_IP>:%s\nRemote Elasticsearch: http://<SERVER_IP>:%s\n' \
    "${CEREBRO_PORT:-9000}" "${KIBANA_PORT:-5601}" "${ES_PORT:-9200}"
  echo 'WARNING: No authentication/TLS. Restrict these ports to trusted lab clients; do not expose them to the Internet.'
else
  printf 'Cerebro: http://%s:%s\nKibana: http://%s:%s\nElasticsearch: http://%s:%s\n' \
    "${ES_BIND_IP:-127.0.0.1}" "${CEREBRO_PORT:-9000}" "${ES_BIND_IP:-127.0.0.1}" "${KIBANA_PORT:-5601}" "${ES_BIND_IP:-127.0.0.1}" "${ES_PORT:-9200}"
fi
echo 'Data is NOT seeded automatically. Next: ./lab.sh seed'
