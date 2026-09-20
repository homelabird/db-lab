#!/usr/bin/env bash
# Isolated, bounded SQL-accounting trials. No original SQL writes or payment gateway.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ $# != 1 || $1 != --yes ]]; then
  echo 'Usage: bash scripts/test-runtime-transactions.sh --yes (prepared pinned local MVP)' >&2
  exit 2
fi
command -v docker >/dev/null || { echo 'BLOCKED: Docker unavailable; no real SQL-accounting trial performed.' >&2; exit 127; }
cd "$ROOT"
umask 077
OUT="mvp-lab/reports/transaction-acceptance/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$OUT"
# Each case creates one isolated MariaDB plus client and verifies ownership before removal.
# Preflight requires the six original containers healthy, but never starts or changes them.
for scenario in stock-race duplicate-checkout checkout-rollback commit-ambiguity idempotency-conflict refund-race; do
  bash ./all.sh mvp drills run "$scenario" --clients 4 --seed 42 --yes 2>&1 | tee "$OUT/$scenario.log"
done
printf 'PASS for this run only: negative controls observed and protected SQL cases audited. Logs: %s\n' "$OUT"
