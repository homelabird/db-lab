#!/usr/bin/env bash
# Run from the project root. NOT a validation script; NOT_REPRODUCED (3) is reported, not hidden.
set -euo pipefail
./ops.sh up
./ops.sh load --seconds 20 --rate 300 --workers 8
for scenario in bigkey fragmentation eviction connections slow-consumer persistence latency cache-stampede stream-pending; do
    rc=0
    ./ops.sh run "$scenario" --yes || rc=$?
    case "$rc" in
      0) printf '%s: PASS\n' "$scenario" ;;
      3) printf '%s: NOT_REPRODUCED — inspect the report\n' "$scenario" ;;
      *) printf '%s: failed/blocked (%s); stopping\n' "$scenario" "$rc"; exit "$rc" ;;
    esac
done
./ops.sh results
