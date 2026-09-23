#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
if [[ "${1:-}" == --help ]]; then
  echo 'Usage: ./lab.sh status [--shards]'
  echo 'Default output summarizes shard health; --shards prints every shard.'
  exit 0
fi
if [[ $# -gt 0 && "$1" != --shards ]]; then
  echo 'Usage: ./lab.sh status [--shards]' >&2
  exit 2
fi
wait_es >/dev/null

echo '=== cluster health ==='
es GET '/_cluster/health?pretty'
echo -e '\n=== nodes ==='
es GET '/_cat/nodes?v&h=name,ip,node.role,master,heap.percent,ram.percent,cpu,load_1m,disk.used_percent,disk.avail'
if [[ "${1:-}" == --shards ]]; then
  echo -e '\n=== shards ==='
  es GET '/_cat/shards?v&s=index,shard,prirep&h=index,shard,prirep,state,docs,store,node,unassigned.reason'
else
  echo -e '\n=== shard summary ==='
  es GET '/_cat/shards?format=json&h=index,shard,prirep,state,unassigned.reason' |
    python3 -c 'import json,sys
rows=json.load(sys.stdin)
bad=[r for r in rows if r.get("state")!="STARTED"]
print(f"[ok] {len(rows)-len(bad)}/{len(rows)} shard copies STARTED")
for r in bad: print("[warn] {}/{} {}: {} {}".format(r.get("index"), r.get("shard"), r.get("prirep"), r.get("state"), r.get("unassigned.reason", "")))'
fi
