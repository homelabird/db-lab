#!/usr/bin/env bash
# REAL Helm offline validation. Refuses to substitute a fake renderer for Helm.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
command -v helm >/dev/null || { echo 'NOT RUN: real helm is not installed.' >&2; exit 127; }
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
helm version --short
help_text=$(helm upgrade --help)
grep -q -- '--reset-then-reuse-values' <<< "$help_text" || {
  echo 'Helm must support --reset-then-reuse-values (Helm 3.14+).' >&2; exit 1;
}
for profile in default full kafka-only mariadb-only kafka mariadb distributed; do
  args=()
  [[ "$profile" == default ]] || args=(-f "$ROOT/helmchart/profiles/$profile.yaml")
  helm lint --strict "$ROOT/helmchart" "${args[@]}"
  helm template alpha "$ROOT/helmchart" -n db-lab "${args[@]}" > "$TMP/$profile.yaml"
  python3 "$ROOT/scripts/check-manifests.py" "$TMP/$profile.yaml" alpha
done
helm template beta "$ROOT/helmchart" -n db-lab -f "$ROOT/helmchart/profiles/full.yaml" > "$TMP/beta.yaml"
python3 "$ROOT/scripts/check-manifests.py" "$TMP/beta.yaml" beta
# Each unsafe configuration must fail with its intended schema/template reason.
# A failure for an unrelated reason (missing tool, broken chart) is not a pass.
reject() {
  local name=$1 pattern=$2
  shift 2
  if helm template negative "$ROOT/helmchart" -n db-lab "$@" > "$TMP/rejected-$name.log" 2>&1; then
    echo "FAIL: unsafe Helm values were accepted: $name" >&2
    return 1
  fi
  if ! grep -Eq -- "$pattern" "$TMP/rejected-$name.log"; then
    echo "FAIL: $name failed for an unexpected reason" >&2
    cat "$TMP/rejected-$name.log" >&2
    return 1
  fi
  printf 'PASS: rejected unsafe profile %s\n' "$name"
}
reject kafka-without-zookeeper 'ZooKeeper-only' --set kafka.enabled=true --set zookeeper.enabled=false
reject kafka-external 'External Kafka listeners are not implemented' --set kafka.external.enabled=true
reject legacy-without-consent 'legacy lab' --set elasticsearch.enabled=true --set elasticsearch.allowLegacy=false
reject recovery-without-consent 'recovery.confirmed=true' --set mariadb.enabled=true --set mariadb.recovery.bootstrapOrdinal=0
reject bootstrap-and-recovery 'mutually exclusive' --set mariadb.enabled=true --set mariadb.bootstrapNewCluster=true --set mariadb.recovery.bootstrapOrdinal=0 --set mariadb.recovery.confirmed=true
reject unknown-value 'Additional property.*not allowed|additional properties' --set misspelledOption=true
printf 'PASS: real Helm lint/render only; no cluster, image, durability, or HA test was run.\n'
