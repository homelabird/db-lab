#!/bin/sh
set -eu
umask 077
case "$REDIS_PASSWORD" in *[!A-Za-z0-9_-]*|'') echo 'Invalid Redis Secret.' >&2; exit 1;; esac
[ "${#REDIS_PASSWORD}" -ge 24 ] && [ "${#REDIS_PASSWORD}" -le 128 ] || exit 1
mkdir -p /data
fingerprint=$(printf %s "$REDIS_PASSWORD" | sha256sum | cut -d' ' -f1)
if [ -f /data/auth.sha256 ] && [ "$(cat /data/auth.sha256)" != "$fingerprint" ]; then
  echo 'Coordinated credential rotation required.' >&2; exit 1
fi
printf '%s\n' "$fingerprint" > /data/auth.sha256
# Sentinel rewrites this writable PVC-backed file following topology changes.
# Never replace it with redis-0 on an ordinary restart.
if [ ! -s /data/sentinel.conf ]; then
  cat > /data/sentinel.conf <<EOF
bind 0.0.0.0
protected-mode yes
port 26379
dir /data
requirepass $REDIS_PASSWORD
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip $POD_NAME.$SENTINEL_SERVICE
sentinel announce-port 26379
sentinel sentinel-pass $REDIS_PASSWORD
sentinel monitor mymaster $REDIS_PREFIX-0.$REDIS_SERVICE 6379 2
sentinel auth-pass mymaster $REDIS_PASSWORD
sentinel down-after-milliseconds mymaster 10000
sentinel failover-timeout mymaster 60000
sentinel parallel-syncs mymaster 1
EOF
fi
exec redis-server /data/sentinel.conf --sentinel
