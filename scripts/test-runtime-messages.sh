#!/usr/bin/env bash
# Ready local stack only. Writes 3 synthetic orders per trial; never resets original data.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ $# != 1 || $1 != --yes ]]; then
  echo 'Usage: bash scripts/test-runtime-messages.sh --yes (prepared, pinned local MVP only)' >&2
  exit 2
fi
command -v docker >/dev/null || { echo 'BLOCKED: Docker unavailable; no real message trial performed.' >&2; exit 127; }
cd "$ROOT"
umask 077
OUT="mvp-lab/reports/message-acceptance/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$OUT"
bash ./all.sh mvp smoke 2>&1 | tee "$OUT/smoke.log"
for scenario in poison-schema mapping-reject projection-commit-gap dlq-commit-gap replay-ordering version-collision; do
  bash ./all.sh mvp messages run "$scenario" --yes 2>&1 | tee "$OUT/$scenario.log"
done
printf 'PASS for this run only. Inspect evidence in %s and reports/messages/. No exactly-once, durability or HA certification.\n' "$OUT"
