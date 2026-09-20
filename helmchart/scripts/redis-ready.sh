#!/bin/sh
set -eu
export REDISCLI_AUTH="$REDIS_PASSWORD"
[ "$(redis-cli --raw ping)" = PONG ]
info=$(redis-cli --raw INFO replication | tr -d '\r')
if printf '%s\n' "$info" | grep -qx 'role:master'; then
  printf '%s\n' "$info" | awk -F: '/^connected_slaves:/{ok=($2>=1)} END{exit !ok}'
else
  printf '%s\n' "$info" | grep -qx 'master_link_status:up'
fi
