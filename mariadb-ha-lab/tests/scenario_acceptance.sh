#!/usr/bin/env bash
# Run only on an already started disposable lab. This intentionally interrupts its DB nodes.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ${RUN_DISRUPTIVE_TESTS:-0} != 1 ]]; then
  echo 'Run only on the lab: RUN_DISRUPTIVE_TESTS=1 bash tests/scenario_acceptance.sh' >&2
  exit 2
fi
mkdir -p reports
exec > >(tee "reports/scenario-acceptance-$(date +%Y%m%d-%H%M%S).log") 2>&1
bash lab.sh status
bash lab.sh scenario slow --rows 5000 --leave-broken
bash lab.sh scenario fix slow
bash lab.sh scenario fragmentation --rows 5000 --payload-bytes 1024 --leave-broken
bash lab.sh scenario fix fragmentation
bash lab.sh scenario lock --hold 6
bash lab.sh scenario node-failure --node galera1 --hold 25
bash lab.sh scenario node-hang --node galera1 --hold 30
if [[ ${RUN_QUORUM_TEST:-0} == 1 ]]; then
  bash lab.sh scenario quorum --hold 45 --confirm-quorum
fi
bash lab.sh scenario report
bash lab.sh verify
echo 'PASS: requested scenario commands returned success. See reports/scenarios/ for measured evidence.'
