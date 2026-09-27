#!/usr/bin/env bash
# Fresh, disposable kind acceptance for MariaDB bootstrap, replication and reviewed recovery.
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
release="maria-accept-${BASHPID}-galera"
created=0

cleanup() {
  local rc=$?
  if (( rc != 0 && created )); then
    if [[ ! -s "$kubeconfig" ]]; then
      echo "[diagnostic] kind cluster creation failed before kubeconfig was written" >&2
    else
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
status_value() {
  local pod=$1 name=$2
  db_query "$pod" 'SHOW GLOBAL STATUS' | awk -F $'\t' -v name="$name" '$1 == name { print $2 }'
}
wait_cluster_size() {
  local pod=$1 expected=$2 status size
  for _ in {1..60}; do
    status=$(status_value "$pod" wsrep_cluster_status 2>/dev/null || true)
    size=$(status_value "$pod" wsrep_cluster_size 2>/dev/null || true)
    [[ "$status" == Primary && "$size" == "$expected" ]] && return 0
    sleep 1
  done
  echo "Galera did not retain a Primary view with $expected members." >&2
  return 1
}

db_query "$release-db-lab-mariadb-0" \
  'CREATE DATABASE IF NOT EXISTS db_lab_acceptance; CREATE TABLE IF NOT EXISTS db_lab_acceptance.probe (id VARCHAR(80) PRIMARY KEY, value INT NOT NULL); REPLACE INTO db_lab_acceptance.probe VALUES ("kind-runtime-acceptance", 1)' >/dev/null
for ordinal in 0 1 2; do
  count=$(db_query "$release-db-lab-mariadb-$ordinal" 'SELECT COUNT(*) FROM db_lab_acceptance.probe')
  [[ "$count" == 1 ]] || { echo "Synthetic row did not replicate to MariaDB node $ordinal." >&2; exit 1; }
done
echo '[ok] Synthetic row is visible on all three nodes.'

initial_uuid=$(status_value "$release-db-lab-mariadb-0" wsrep_cluster_state_uuid)
initial_seqno=$(status_value "$release-db-lab-mariadb-0" wsrep_last_committed)
[[ -n "$initial_uuid" && "$initial_seqno" =~ ^[0-9]+$ ]] || { echo 'Could not record initial Galera UUID/seqno.' >&2; exit 1; }
declare -a pvc_identity
for ordinal in 0 1 2; do
  pvc="data-$release-db-lab-mariadb-$ordinal"
  pvc_identity[$ordinal]=$(kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    get pvc "$pvc" -o jsonpath='{.metadata.uid}:{.spec.volumeName}')
done

pod="$release-db-lab-mariadb-2"
kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" delete pod "$pod" --wait=true --timeout=90s
kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" wait \
  --for=condition=Ready "pod/$pod" --timeout=180s
for ordinal in 0 1 2; do
  pod="$release-db-lab-mariadb-$ordinal"
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    exec "$pod" -- /bin/bash /opt/db-lab/mariadb-ready.sh >/dev/null
  value=$(db_query "$pod" 'SELECT value FROM db_lab_acceptance.probe WHERE id="kind-runtime-acceptance"')
  after=$(kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    get pvc "data-$release-db-lab-mariadb-$ordinal" -o jsonpath='{.metadata.uid}:{.spec.volumeName}')
  [[ "$value" == 1 && "${pvc_identity[$ordinal]}" == "$after" ]] || {
    echo "Synthetic row or PVC identity changed after pod recreation on node $ordinal." >&2; exit 1;
  }
done
echo '[ok] Recreated pod rejoined using the same PVC; cluster_size=3 and synthetic row persisted.'

statefulset="$release-db-lab-mariadb"
for remaining in 2 1 0; do
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    scale "statefulset/$statefulset" --replicas="$remaining" >/dev/null
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    wait --for=delete "pod/$release-db-lab-mariadb-$remaining" --timeout=180s
  if (( remaining == 2 )); then wait_cluster_size "$release-db-lab-mariadb-0" 2; fi
done
echo '[ok] All MariaDB nodes shut down in order; no chart recovery flag was forced.'

mariadb_image=$(kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
  get statefulset "$statefulset" -o jsonpath='{.spec.template.spec.containers[?(@.name=="mariadb")].image}')
reviewed_uuid=''
highest_seqno=-1
candidate=-1
candidate_seqno=-1
safe_count=0
for ordinal in 0 1 2; do
  inspect_pod="galera-state-$ordinal"
  pvc="data-$release-db-lab-mariadb-$ordinal"
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $inspect_pod
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsUser: 1001
    runAsNonRoot: true
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: state
      image: "$mariadb_image"
      imagePullPolicy: IfNotPresent
      command: ["/bin/bash", "-c", "sleep 600"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: data
          mountPath: /bitnami/mariadb
          readOnly: true
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: "$pvc"
        readOnly: true
YAML
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    wait --for=condition=Ready "pod/$inspect_pod" --timeout=180s
  state=$(kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    exec "$inspect_pod" -- cat /bitnami/mariadb/data/grastate.dat)
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    delete pod "$inspect_pod" --wait=true --timeout=90s >/dev/null
  uuid=$(awk '$1 == "uuid:" {print $2}' <<<"$state")
  seqno=$(awk '$1 == "seqno:" {print $2}' <<<"$state")
  safe=$(awk '$1 == "safe_to_bootstrap:" {print $2}' <<<"$state")
  [[ "$uuid" == "$initial_uuid" && "$seqno" =~ ^-?[0-9]+$ && ( "$safe" == 0 || "$safe" == 1 ) ]] || {
    echo "Invalid offline Galera state for ordinal $ordinal." >&2; exit 1;
  }
  [[ -z "$reviewed_uuid" || "$reviewed_uuid" == "$uuid" ]] || {
    echo 'Galera PVCs contain different state UUIDs; recovery is unsafe.' >&2; exit 1;
  }
  reviewed_uuid=$uuid
  (( seqno > highest_seqno )) && highest_seqno=$seqno
  if [[ "$safe" == 1 ]]; then
    candidate=$ordinal
    candidate_seqno=$seqno
    safe_count=$((safe_count + 1))
  fi
  echo "[state] ordinal=$ordinal uuid=$uuid seqno=$seqno safe_to_bootstrap=$safe"
done
[[ "$safe_count" == 1 && "$candidate_seqno" == "$highest_seqno" && "$candidate_seqno" -ge "$initial_seqno" ]] || {
  echo 'No unique safe_to_bootstrap candidate matches the latest reviewed Galera seqno.' >&2; exit 1;
}
echo "[ok] Reviewed ordinal $candidate as the unique safe bootstrap candidate at seqno $candidate_seqno."

bash ./all.sh k8s up -f ./helmchart/profiles/mariadb-only.yaml \
  --set "mariadb.recovery.bootstrapOrdinal=$candidate" --set mariadb.recovery.confirmed=true
helm --kubeconfig "$kubeconfig" --kube-context "$DB_LAB_CONTEXT" get values \
  "$release" --namespace "$namespace" --all --output json | python3 -c \
  'import json,sys; m=json.load(sys.stdin)["mariadb"]; assert m["bootstrapNewCluster"] is False and m["recovery"]["bootstrapOrdinal"] == -1 and m["recovery"]["confirmed"] is False'
for ordinal in 0 1 2; do
  pod="$release-db-lab-mariadb-$ordinal"
  kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    exec "$pod" -- /bin/bash /opt/db-lab/mariadb-ready.sh >/dev/null
  uuid=$(status_value "$pod" wsrep_cluster_state_uuid)
  value=$(db_query "$pod" 'SELECT value FROM db_lab_acceptance.probe WHERE id="kind-runtime-acceptance"')
  after=$(kubectl --kubeconfig "$kubeconfig" --context "$DB_LAB_CONTEXT" -n "$namespace" \
    get pvc "data-$release-db-lab-mariadb-$ordinal" -o jsonpath='{.metadata.uid}:{.spec.volumeName}')
  [[ "$uuid" == "$initial_uuid" && "$value" == 1 && "${pvc_identity[$ordinal]}" == "$after" ]] || {
    echo "UUID, synthetic row, or PVC identity changed after full-cluster recovery on node $ordinal." >&2; exit 1;
  }
done
echo '[ok] Full-cluster recovery restored the same UUID, all three PVCs, and synthetic data; recovery flags were sealed.'
echo '[scope] Disposable kind Galera/PVC behavior only: no node-failure, cross-provider, or production claim.'
