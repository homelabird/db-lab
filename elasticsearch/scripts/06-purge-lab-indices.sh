#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
if [[ -f "$LAB_ROOT/reports/faults/active.json" ]]; then
  echo '[error] Active fault drill: run ./scripts/13-fault-lab.sh recover first.' >&2
  exit 1
fi
[[ "${1:-}" == '--yes' ]] || { echo 'Deletes only the three seed indices. Use: ./scripts/06-purge-lab-indices.sh --yes' >&2; exit 2; }
export PYTHONPATH="$LAB_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 - <<'PY'
from lablib import APIError, ESClient, INDICES
c=ESClient(); c.assert_lab()
for index in INDICES:
    try: print(index,c.request('DELETE','/'+index))
    except APIError as e:
        if e.status != 404: raise
        print(index,'already absent')
PY
