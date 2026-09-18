#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
export PYTHONPATH="$LAB_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 - <<'PY'
import os
from fault_lab import Drill, FaultClient, require
from lablib import INDICES
c=FaultClient(); c.guard()
index=os.getenv('INDEX','lab-web-logs-v1'); shard=int(os.getenv('SHARD','0'))
require(index in INDICES and shard>=0,'INDEX must be a seed index; SHARD must be >= 0')
d=Drill(client=c,timeout=float(os.getenv('FAULT_TIMEOUT','180')))
require(not d.active.exists(),'Finish the active managed drill first (13-fault-lab.sh recover)')
d.stable(); baseline=d.read_seeds()
rows=c.request('GET',f'/_cat/shards/{index}?format=json')
replica=next((s for s in rows if int(s['shard'])==shard and s['prirep']=='r' and s['state']=='STARTED'),None)
require(replica is not None,'No STARTED replica found')
body={'commands':[{'cancel':{'index':index,'shard':shard,'node':replica['node'],'allow_primary':False}}]}
c.request('POST','/_cluster/reroute?dry_run=true',body)
c.request('POST','/_cluster/reroute',body)
print(f'Cancelled replica {index}/{shard} on {replica["node"]}; waiting for reallocation.')
d.stable()
require(d.read_seeds()==baseline,'Seed count/index UUID/sample changed')
print('[PASS] Replica cancellation submitted; stable green and seed invariants verified.')
PY
