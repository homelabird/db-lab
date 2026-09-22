#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
export PYTHONPATH="$LAB_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 - <<'PY'
import json
from lablib import ESClient, INDICES, ROOT
c=ESClient()
stats=c.request('GET','/'+','.join(INDICES)+'/_stats/store,docs')
print('index                     docs        primary MiB   with replicas MiB')
for index in INDICES:
    value=stats['indices'][index]
    print(f'{index:26s} {value["primaries"]["docs"]["count"]:9,d} {value["primaries"]["store"]["size_in_bytes"]/1048576:14.3f} {value["total"]["store"]["size_in_bytes"]/1048576:19.3f}')
manifest=ROOT/'reports/seed-manifest.json'
if manifest.exists():
    data=json.loads(manifest.read_text())
    if data.get('cluster_uuid')==c.request('GET','/').get('cluster_uuid') and data.get('status')=='loaded':
        print(f'\nLast seed raw JSON source: {data["source_bytes"]/1048576:.3f} MiB, {data["documents"]:,} documents')
print('Raw JSON != primary Lucene store != store including replicas != filesystem use (translog etc.).')
PY
