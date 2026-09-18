#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
if [[ -f "$LAB_ROOT/reports/faults/active.json" ]]; then
  echo '[error] Recover the active managed fault before changing topology.' >&2
  exit 1
fi
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != '--remove' ) ]]; then
  echo 'Usage: ./scenarios/13-scale-out-node.sh [--remove]' >&2; exit 2
fi
guard_lab
if [[ "${1:-}" == '--remove' ]]; then
  compose -f compose.yaml -f compose.scaleout.yaml stop es06
  compose -f compose.yaml -f compose.scaleout.yaml rm -f es06
  export SCALE_MODE=remove
else
  compose -f compose.yaml -f compose.scaleout.yaml up -d es06
  export SCALE_MODE=add
fi
export PYTHONPATH="$LAB_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 - <<'PY'
import os
from fault_lab import Drill, FaultClient
c=FaultClient(); c.guard()
d=Drill(client=c,timeout=float(os.getenv('FAULT_TIMEOUT','180')))
expected=[f'es0{i}' for i in range(1,6 if os.environ['SCALE_MODE']=='remove' else 7)]
result=d.stable(expected)
print('[PASS] Expected node membership and stable green confirmed:',result)
print('No volume was deleted. Removal without drain is a lab fault, not a rolling-maintenance recipe.')
PY
