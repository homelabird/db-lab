#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh

NAMESPACE="${K8S_NAMESPACE:-elasticsearch-lab}"
ES_IMAGE="${K8S_ES_IMAGE:-docker.elastic.co/elasticsearch/elasticsearch:7.17.29}"
KIBANA_IMAGE="${K8S_KIBANA_IMAGE:-docker.elastic.co/kibana/kibana:7.17.29}"

usage() {
  cat <<'EOF'
Kubernetes lab controller

Usage:
  ./lab.sh k8s doctor
  ./lab.sh k8s apply
  ./lab.sh k8s status
  ./lab.sh k8s verify
  ./lab.sh k8s render --nodes 100
  ./lab.sh k8s upgrade docker.elastic.co/elasticsearch/elasticsearch:7.17.29
  ./lab.sh k8s delete

K8S_NAMESPACE changes the namespace (default: elasticsearch-lab).
K8S_ES_IMAGE and K8S_KIBANA_IMAGE select images for apply.
render only generates a Helm manifest; it does not contact Kubernetes or start containers.
upgrade is restricted to the same Elasticsearch major/minor line and performs
a StatefulSet rolling update; take a snapshot and test compatibility first.
EOF
}

need_kubectl() { command -v kubectl >/dev/null 2>&1 || { echo '[error] kubectl is required' >&2; exit 127; }; }
render_lab() {
  command -v helm >/dev/null 2>&1 || { echo '[error] helm is required' >&2; exit 127; }
  local nodes="${1:-3}"
  [[ "$nodes" =~ ^[0-9]+$ ]] && (( nodes >= 3 && nodes <= 100 )) || {
    echo '[error] --nodes must be an integer from 3 through 100' >&2
    exit 2
  }
  helm template elasticsearch-lab ./helm/elasticsearch-lab \
    --set "elasticsearch.replicas=${nodes}"
}
doctor() {
  need_kubectl
  kubectl version --client
  kubectl cluster-info >/dev/null
  kubectl get storageclass
}
apply_lab() {
  need_kubectl
  kubectl apply -f k8s/elasticsearch.yaml
  kubectl apply -f k8s/ui.yaml
  kubectl -n "$NAMESPACE" set image statefulset/elasticsearch "elasticsearch=$ES_IMAGE"
  kubectl -n "$NAMESPACE" set image deployment/kibana "kibana=$KIBANA_IMAGE"
  kubectl -n "$NAMESPACE" rollout status statefulset/elasticsearch --timeout="${K8S_TIMEOUT:-15m}"
  kubectl -n "$NAMESPACE" rollout status deployment/kibana --timeout="${K8S_TIMEOUT:-15m}"
}
status_lab() {
  need_kubectl
  kubectl -n "$NAMESPACE" get pods,svc,pdb,statefulset,deploy -o wide
}
verify_lab() {
  need_kubectl
  local pod
  pod="$(kubectl -n "$NAMESPACE" get pod -l app=elasticsearch -o jsonpath='{.items[0].metadata.name}')"
  kubectl -n "$NAMESPACE" exec "$pod" -- curl -fsS 'http://127.0.0.1:9200/_cluster/health?pretty'
  kubectl -n "$NAMESPACE" get pods -l app=elasticsearch --field-selector=status.phase=Running --no-headers | wc -l | awk '$1 < 3 { exit 1 }'
}
upgrade() {
  need_kubectl
  local target="${1:-}"
  [[ "$target" =~ :7\.17\.[0-9]+$ ]] || {
    echo '[error] Only explicit Elasticsearch 7.17.x images are supported by this rolling lab scenario.' >&2
    echo '[hint] Elasticsearch 8.x requires a documented migration/upgrade path, not a direct image swap.' >&2
    exit 2
  }
  local current
  current="$(kubectl -n "$NAMESPACE" get statefulset/elasticsearch -o jsonpath='{.spec.template.spec.containers[?(@.name=="elasticsearch")].image}')"
  [[ "$current" =~ :7\.17\. ]] || { echo "[error] Current image is not a supported 7.17.x line: $current" >&2; exit 2; }
  kubectl -n "$NAMESPACE" patch statefulset elasticsearch --type='strategic' \
    -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"elasticsearch\",\"image\":\"$target\"}]}}}}"
  kubectl -n "$NAMESPACE" rollout status statefulset/elasticsearch --timeout="${K8S_TIMEOUT:-15m}"
  verify_lab
}
cmd="${1:-help}"; shift || true
case "$cmd" in
  help|-h|--help) usage ;;
  doctor) doctor ;;
  render)
    [[ "${1:-}" == '--nodes' ]] || { echo 'Usage: ./lab.sh k8s render --nodes 3..100' >&2; exit 2; }
    render_lab "${2:-}"
    ;;
  apply|install) apply_lab ;;
  status) status_lab ;;
  verify) verify_lab ;;
  upgrade) upgrade "${1:-}" ;;
  delete|uninstall) need_kubectl; kubectl delete namespace "$NAMESPACE" ;;
  *) echo "[error] Unknown k8s command: $cmd" >&2; usage >&2; exit 2 ;;
esac
