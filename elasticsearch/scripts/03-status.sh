#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
wait_es >/dev/null

echo '=== cluster health ==='
es GET '/_cluster/health?pretty'
echo -e '\n=== nodes ==='
es GET '/_cat/nodes?v&h=name,ip,node.role,master,heap.percent,ram.percent,cpu,load_1m,disk.used_percent,disk.avail'
echo -e '\n=== shards ==='
es GET '/_cat/shards?v&s=index,shard,prirep&h=index,shard,prirep,state,docs,store,node,unassigned.reason'
