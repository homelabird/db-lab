#!/usr/bin/env bash
# Fresh, disposable kind acceptance for MariaDB bootstrap, replication and PVC rejoin.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for tool in kind kubectl helm docker; do
  command -v "$tool" >/dev/null 2>&1 || { echo "BLOCKED: $tool is required." >&2; exit 127; }
done

cluster="db-lab-maria-accept-${BASHPID}"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/db-lab-maria-accept.XXXXXX")
kubeconfig=$tmp/kubeconfig
config=$tmp/kind.yaml
namespace="db-lab-maria-${BASHPID}"
release="maria-accept-${BASHPID}"
created=0

cleanup() {
  local rc=$?
  if (( rc != 0 && created )); then
    echo "[diagnostic] disposable MariaDB acceptance failed; collecting pod states and logs" >&2
    kubectl --kubeconfig "$kubeconfig" --context "kind-$cluster" -n "$namespace" get pods,pvc,events >&2 || true
    kubectl --kubeconfig "$kubeconfig" --context "kind-$cluster" -n "$namespace" describe pods >&2 || true
    for pod in $(kubectl --kubeconfig "$kubeconfig" --context "kind-$cluster" -n "$namespace" get pods -o name 2>/dev/null); do
      kubectl --kubeconfig "$kubeconfig" --context "kind-$cluster" -n "$namespace" logs "$pod" --all-containers --previous --tail=100 >&2 || true
      kubectl --kubeconfig "$kubeconfig" --context "kind-$cluster" -n "$namespace" logs "$pod" --all-containers --tail=100 >&2 || true
      if [[ "$pod" == pod/* ]]; then
        echo "[diagnostic] Galera state: $pod" >&2
        kubectl --kubeconfig "$kubeconfig" --context "kind-$cluster" -n "$namespace" exec "$pod" -- /bin/bash -c \
          'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; client=/opt/bitnami/mariadb/bin/mariadb; if "$client" -h 127.0.0.1 -uroot --connect-timeout=2 -N -B -e "SELECT 1" >/dev/null 2>&1; then echo "[diagnostic] localhost:3306 SQL reachable"; else echo "[diagnostic] localhost:3306 SQL unavailable"; fi; "$client" -h 127.0.0.1 -uroot -N -B -e "SHOW GLOBAL VARIABLES" 2>/dev/null | grep -E "^wsrep_(cluster_address|node_address|provider_options|cluster_name)[[:space:]]" || true; "$client" -h 127.0.0.1 -uroot -N -B -e "SHOW GLOBAL STATUS" 2>/dev/null | grep -E "^wsrep_(cluster_status|cluster_size|incoming_addresses|connected|ready|local_state_comment|local_state_uuid)[[:space:]]" || true; IFS=, read -ra peers <<< "$GALERA_PEERS"; for peer in "${peers[@]}"; do for port in 4567 4568 4444; do if timeout 2 bash -c "</dev/tcp/$peer/$port" 2>/dev/null; then echo "[diagnostic] peer $peer:$port TCP reachable"; else echo "[diagnostic] peer $peer:$port TCP unavailable"; fi; done; done' >&2 || true
      fi
    done
  fi
  if (( created )); then kind delete cluster --name "$cluster" >/dev/null || rc=1; fi
  rm -rf -- "$tmp"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cat >"$config" <<'YAML'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
  - role: worker
YAML
created=1
kind create cluster --name "$cluster" --config "$config" \
  --image "${DB_LAB_KIND_NODE_IMAGE:-kindest/node:v1.31.2}" \
  --kubeconfig "$kubeconfig" --wait 120s

export DB_LAB_CONTEXT="kind-$cluster" DB_LAB_ALLOWED_CONTEXTS="kind-$cluster"
export DB_LAB_KUBECONFIG="$kubeconfig" DB_LAB_NAMESPACE="$namespace" DB_LAB_RELEASE="$release"
export DB_LAB_TIMEOUT=${DB_LAB_TIMEOUT:-10m}
kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" wait \
  --for=condition=Ready nodes --all --timeout=180s
cd "$ROOT"
bash ./all.sh k8s init
bash ./all.sh k8s up -f ./helmchart/profiles/mariadb-only.yaml --set mariadb.bootstrapNewCluster=true

pods=$(kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" get pods -l "app.kubernetes.io/instance=$release" --no-headers | wc -l)
[[ "$pods" == 3 ]] || { echo "Expected 3 MariaDB pods, got $pods." >&2; exit 1; }
for ordinal in 0 1 2; do
  pod="$release-db-lab-mariadb-$ordinal"
  code=$(kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    get pod "$pod" -o jsonpath='{.status.initContainerStatuses[0].state.terminated.exitCode}')
  [[ "$code" == 0 ]] || { echo "$pod peer gate did not complete successfully." >&2; exit 1; }
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    exec "$pod" -- /bin/bash /opt/db-lab/mariadb-ready.sh >/dev/null
done
echo '[ok] 3 MariaDB nodes are Primary/Synced with cluster_size=3; bootstrap is sealed.'

db_query() {
  local pod=$1 sql=$2
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    exec "$pod" -- /bin/bash -c \
    'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; exec /opt/bitnami/mariadb/bin/mariadb -h 127.0.0.1 -uroot -N -B -e "$1"' \
    _ "$sql"
}

db_query "$release-db-lab-mariadb-0" \
  'CREATE DATABASE IF NOT EXISTS db_lab_acceptance; CREATE TABLE IF NOT EXISTS db_lab_acceptance.probe (id VARCHAR(80) PRIMARY KEY, value INT NOT NULL); REPLACE INTO db_lab_acceptance.probe VALUES ("kind-runtime-acceptance", 1)' >/dev/null
for ordinal in 0 1 2; do
  count=$(db_query "$release-db-lab-mariadb-$ordinal" 'SELECT COUNT(*) FROM db_lab_acceptance.probe')
  [[ "$count" == 1 ]] || { echo "Synthetic row did not replicate to MariaDB node $ordinal." >&2; exit 1; }
done
echo '[ok] Synthetic row is visible on all three nodes.'

pvc="data-$release-db-lab-mariadb-2"
before=$(kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
  get pvc "$pvc" -o jsonpath='{.metadata.uid}:{.spec.volumeName}')
pod="$release-db-lab-mariadb-2"
kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" delete pod "$pod" --wait=true --timeout=90s
kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" wait \
  --for=condition=Ready "pod/$pod" --timeout=180s
after=$(kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
  get pvc "$pvc" -o jsonpath='{.metadata.uid}:{.spec.volumeName}')
[[ "$before" == "$after" ]] || { echo 'MariaDB PVC identity changed during pod recreation.' >&2; exit 1; }
for ordinal in 0 1 2; do
  pod="$release-db-lab-mariadb-$ordinal"
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    exec "$pod" -- /bin/bash /opt/db-lab/mariadb-ready.sh >/dev/null
  count=$(db_query "$pod" 'SELECT COUNT(*) FROM db_lab_acceptance.probe')
  [[ "$count" == 1 ]] || { echo "Synthetic row missing after PVC rejoin on node $ordinal." >&2; exit 1; }
done
echo '[ok] Recreated pod rejoined using the same PVC; cluster_size=3 and synthetic row persisted.'
echo '[scope] Disposable kind only: no full-cluster outage/recovery or cross-provider claim.'
