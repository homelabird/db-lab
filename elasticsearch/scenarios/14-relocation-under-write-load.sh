#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/common.sh
wait_es >/dev/null
cat <<'MSG'
This scenario needs two terminals.

Terminal A - continuous indexing (~100 docs/s):
  RATE=100 ./lab.sh load

Terminal B - while writes continue:
  ./lab.sh scenario 01
  watch -n1 'curl -s localhost:9200/_cat/recovery?v&active_only=true'

Things to observe:
  - writes continue while a shard is relocating
  - source/target recovery progress
  - after relocation completes the shard is STARTED on the target
  - document count continues to increase

Current document count:
MSG
curl -fsS "$ES_URL/lab-transactions-v1/_count?pretty"
