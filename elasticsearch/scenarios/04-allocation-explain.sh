#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
export PYTHONPATH="$LAB_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 - <<'PYCODE'
import json, os
from fault_lab import Drill, FaultClient, require
from lablib import INDICES
c=FaultClient(); c.guard()
index=os.getenv('INDEX')
if index:
    require(index in INDICES or index.startswith('lab-fault-'), 'Only this lab index is allowed')
if 'SHARD' in os.environ or 'PRIMARY' in os.environ:
    index=index or INDICES[0]
    shard=int(os.getenv('SHARD','0')); primary=os.getenv('PRIMARY','false').lower()
    require(shard>=0 and primary in ('true','false'), 'SHARD >=0 and PRIMARY=true|false')
    result=c.request('GET','/_cluster/allocation/explain?include_disk_info=true&include_yes_decisions=true',
                     {'index':index,'shard':shard,'primary':primary=='true'})
else:
    result=Drill(client=c).explain(index)
print(json.dumps(result,ensure_ascii=False,indent=2))
PYCODE
