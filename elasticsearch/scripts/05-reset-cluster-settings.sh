#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
if [[ -f "$LAB_ROOT/reports/faults/active.json" ]]; then
  echo '[error] Active fault drill: run ./lab.sh fault recover first.' >&2
  exit 1
fi
export PYTHONPATH="$LAB_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 - <<'PY'
from lablib import APIError, ESClient, INDICES, LAYOUT
c=ESClient(); c.assert_lab()
keys=['cluster.routing.allocation.exclude._name','cluster.routing.allocation.enable',
      'cluster.routing.rebalance.enable','cluster.routing.allocation.awareness.attributes',
      'cluster.routing.allocation.disk.watermark.low','cluster.routing.allocation.disk.watermark.high',
      'cluster.routing.allocation.disk.watermark.flood_stage']
print(c.request('PUT','/_cluster/settings',{scope:{key:None for key in keys} for scope in ['persistent','transient']}))
for index in INDICES:
    settings={'index.routing.allocation.require._name':None,'index.routing.allocation.include._name':None,
              'index.routing.allocation.exclude._name':None,'index.blocks.read_only_allow_delete':None,
              'index.number_of_replicas':LAYOUT[index][1],'index.refresh_interval':'1s'}
    try: print(index,c.request('PUT',f'/{index}/_settings',settings))
    except APIError as exc:
        if exc.status != 404: raise
        print(index,'not present')
print('[ok] Restored this lab baseline. No data was deleted. Start any stopped nodes separately.')
PY
