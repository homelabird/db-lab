#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --faults ) ]]; then
    printf 'Usage: %s [--faults]\n' "$0" >&2; exit 1
fi
mkdir -p reports
report="reports/run-$(date -u +%Y%m%dT%H%M%SZ)-live-test-$$"
mkdir -p "$report"
exec > >(tee "$report/output.log") 2>&1
trap 'rc=$?; printf "%s\n" "$rc" > "$report/exit-code.txt"' EXIT
./lab.sh doctor
./lab.sh health
./lab.sh smoke
if [[ "${1:-}" == --faults ]]; then
    for scenario in broker-failover controller-failover min-isr zk-one zk-quorum lag hot-key oversize retention; do
        ./lab.sh demo "$scenario"
    done
fi
./lab.sh health
printf '\nLIVE TEST PASS. 이 출력은 실제 호스트 실행이 성공했을 때만 나옵니다.\n'
