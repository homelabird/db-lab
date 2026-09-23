#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
ensure_lab_env
check_kibana_auth

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

net_name="$(network_name)"
printf '[info] Runtime: %s | Compose provider: %s\n' "$RUNTIME" "$COMPOSE_PROVIDER"
printf '[info] Compose network: %s\n' "$net_name"
if ! compose config >/dev/null; then
  echo '[error] Compose configuration is invalid; no services were started.' >&2
  exit 1
fi
echo '[ok] Compose configuration is valid.'

compose run --rm cert-init
compose up -d --no-deps es01 es02 es03 es04 es05

# Stage 1: containers must be running before we check application readiness.
printf '[info] Waiting for %s..%s containers to be running...\n' es01 es05
container_wait_seconds="${CONTAINER_WAIT_SECONDS:-90}"
deadline=$(( $(date +%s) + container_wait_seconds ))
while (( $(date +%s) < deadline )); do
  running=0
  for name in es01 es02 es03 es04 es05; do
    container_running "$name" && running=$((running + 1))
  done
  (( running == 5 )) && break
  sleep 2
done
if [[ "$running" != 5 ]]; then
  echo '[error] Not all Elasticsearch containers are running.' >&2
  es_network_diagnose "$net_name"
  exit 1
fi
printf '[ok] All 5 Elasticsearch containers are running.\n'

# Stage 2: Elasticsearch HTTP + cluster formation (all nodes joined).
# Normal startup never modifies host firewall rules; if the cluster cannot
# form, es_network_diagnose distinguishes startup problems from blocked
# inter-container traffic and prints a working firewall hint with the real subnet.
if ! python3 ./scripts/lablib.py wait --nodes 5 --green --seconds "${WAIT_SECONDS:-300}"; then
  es_network_diagnose "$net_name"
  exit 1
fi

# Stage 3: shared filesystem snapshot repository (register + verify) so
# snapshot/restore exercises work immediately after ./lab.sh up.
# Failures here are fatal for up: the lab bootstraps the repository by design.
ensure_snapshot_repo

# Configure built-in Kibana credentials and the optional restricted UI role
# only after the secured Elasticsearch API is ready.
ensure_security_users
compose up -d --no-deps kibana
if [[ "${XPACK_SECURITY_ENABLED:-false}" == true ]]; then
  compose stop cerebro >/dev/null 2>&1 || true
else
  compose up -d --no-deps cerebro
fi

# Stage 4: Kibana ready (its /api/status flips to green once ES is weak-healthy).
KIBANA_WAIT_SECONDS="${KIBANA_WAIT_SECONDS:-180}"
kibana_ready=0
for _ in $(seq 1 "$((KIBANA_WAIT_SECONDS / 2))"); do
  if kibana_status_available "${KIBANA_PORT:-5601}"; then
    kibana_ready=1
    break
  fi
  sleep 2
done
if (( kibana_ready == 0 )); then
  echo "[error] Kibana did not become ready within ${KIBANA_WAIT_SECONDS}s." >&2
  echo "[hint] Recent Kibana startup logs:" >&2
  compose logs --tail=80 kibana >&2 || true
  echo '[hint] Check Kibana logs, matching Kibana/Elasticsearch versions, and kibana_system credentials in .env.' >&2
  echo '[hint] KIBANA_STACK_MONITORING_ENABLED must be true or false.' >&2
  exit 1
fi

# Cerebro has no supported credentials in this lab configuration. Avoid
# starting a broken UI against an authenticated cluster.
if [[ "${XPACK_SECURITY_ENABLED:-false}" == true ]]; then
  echo '[info] Cerebro is skipped: this lab does not configure authenticated Cerebro access.'
else
# Stage 5: Cerebro answers on its HTTP port.
CEREBRO_WAIT_SECONDS="${CEREBRO_WAIT_SECONDS:-120}"
cerebro_ready=0
for _ in $(seq 1 "$((CEREBRO_WAIT_SECONDS / 2))"); do
  if curl -fsS --connect-timeout 2 --max-time 5 \
      "http://127.0.0.1:${CEREBRO_PORT:-9000}/" >/dev/null 2>&1; then
    cerebro_ready=1
    break
  fi
  sleep 2
done
if (( cerebro_ready == 0 )); then
  echo "[error] Cerebro did not answer within ${CEREBRO_WAIT_SECONDS}s." >&2
  echo "[hint] Inspect with: ./lab.sh logs cerebro" >&2
  exit 1
fi
fi

verify_install
printf '\n=== cluster status ===\n'
bash ./lab.sh status
bash ./lab.sh ui

subnet="$(network_subnet "$net_name" 2>/dev/null || echo unknown)"
cat <<EOF

Lab is ready.
  Runtime:            ${RUNTIME}
  Compose provider:   ${COMPOSE_PROVIDER}
  Compose network:    ${net_name} (${subnet})
  Cluster:            ${LAB_CLUSTER_NAME} (5 nodes)
  Snapshot repo:      ${SNAPSHOT_REPO_NAME} (${SNAPSHOT_PATH}, verified)
  Elasticsearch:      ${ES_URL}
  Cerebro:            http://${ES_BIND_IP:-127.0.0.1}:${CEREBRO_PORT:-9000}
  Kibana:             http://${ES_BIND_IP:-127.0.0.1}:${KIBANA_PORT:-5601}
  Kibana Console:     http://${ES_BIND_IP:-127.0.0.1}:${KIBANA_PORT:-5601}/app/dev_tools#/console
  Security:           ${XPACK_SECURITY_ENABLED:-false} (opt-in via .env; see README)
EOF
echo 'Data is NOT seeded automatically. Next: ./lab.sh seed'
