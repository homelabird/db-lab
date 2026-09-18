#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
wait_es >/dev/null
INDEX="${INDEX:-lab-transactions-v1}"
cat <<MSG
=== $INDEX shard layout ===
P = primary, R = replica. The same shard ID should not have primary and replica on the same node.
MSG
curl -fsS "$ES_URL/_cat/shards/$INDEX?v&s=shard,prirep&h=index,shard,prirep,state,docs,store,node"
echo -e '\n=== per-node shard count ==='
curl -fsS "$ES_URL/_cat/shards/$INDEX?h=node,state" | awk '$2=="STARTED"{c[$1]++} END{for(n in c) print n,c[n]}' | sort
