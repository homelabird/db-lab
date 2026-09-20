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
for profile in default full kafka-only mariadb-only; do
  args=()
  [[ "$profile" == default ]] || args=(-f "$ROOT/helmchart/profiles/$profile.yaml")
  helm lint --strict "$ROOT/helmchart" "${args[@]}"
  helm template alpha "$ROOT/helmchart" -n db-lab "${args[@]}" > "$TMP/$profile.yaml"
  python3 "$ROOT/scripts/check-manifests.py" "$TMP/$profile.yaml" alpha
 done
helm template beta "$ROOT/helmchart" -n db-lab -f "$ROOT/helmchart/profiles/full.yaml" > "$TMP/beta.yaml"
python3 "$ROOT/scripts/check-manifests.py" "$TMP/beta.yaml" beta
printf 'PASS: real Helm lint/render only; no cluster, image, durability, or HA test was run.\n'
