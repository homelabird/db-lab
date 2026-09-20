#!/usr/bin/env bash
# Explicit LIVE acceptance; never boot/reset/prune/source-volume-delete automatically.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ $# != 1 || $1 != --yes ]]; then
  printf 'Usage: bash scripts/test-runtime-mvp.sh --yes\nRequires an already ready, pinned, local Docker MVP. Writes synthetic orders, injects network faults, and creates/disposes scratch DBs. No automatic stack startup or source-volume deletion.\n' >&2
  exit 2
fi
command -v docker >/dev/null || { echo 'BLOCKED: Docker not available; no live tests executed.' >&2; exit 127; }
cd "$ROOT"
OUT="mvp-lab/reports/runtime-acceptance/$(date -u +%Y%m%dT%H%M%SZ)-$$"
umask 077
mkdir -p "$OUT"
step() {
  local name=$1
  shift
  printf 'Running %s\n' "$name"
  # pipefail preserves the command's failure, including inconclusive status 2.
  bash ./all.sh mvp "$@" 2>&1 | tee "$OUT/$name.log"
}
# Preparation builds just the opt-in netem helper, not the source DB images.
step doctor doctor
step smoke smoke
step baseline simulate run baseline --seed 42 --workload write-heavy --yes
step prepare drills prepare --yes
step network-delay simulate run db-network-delay --seed 42 --workload write-heavy --yes
step network-loss simulate run db-network-loss --seed 42 --workload write-heavy --yes
step restore drills run backup-restore --yes
step disk-full drills run redis-disk-full --yes
step oom drills run redis-oom --yes
step deadlock drills run deadlock --yes
printf 'PASS for this explicit run. Inspect all evidence under %s and the drill report paths. No performance/HA certification.\n' "$OUT"
