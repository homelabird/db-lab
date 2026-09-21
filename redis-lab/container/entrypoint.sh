#!/bin/sh
set -eu
umask 077
: "${NODE_NAME:?NODE_NAME missing}"
: "${NODE_IP:?NODE_IP missing}"
: "${REDIS_PASSWORD:?REDIS_PASSWORD missing}"
: "${SENTINEL_PASSWORD:?SENTINEL_PASSWORD missing}"
: "${MASTER_NAME:=mymaster}"
: "${PRIMARY_IP:?PRIMARY_IP missing}"
: "${LAB_MODE:=redis}"
: "${REDIS_MAXMEMORY:=192mb}"
: "${DOWN_AFTER_MS:=5000}"
: "${FAILOVER_TIMEOUT_MS:=30000}"
: "${CLUSTER_BUS_PORT:=16379}"
: "${CLUSTER_NODE_TIMEOUT_MS:=5000}"
for password in "$REDIS_PASSWORD" "$SENTINEL_PASSWORD"; do
    case "$password" in *[!A-Za-z0-9_.@%+-]*|'')
        echo 'Password must use A-Z a-z 0-9 _ . @ % + - only.' >&2; exit 64;;
    esac
done
mkdir -p /data/state /data/db
# Fingerprint catches accidental credential/address changes against existing volumes.
fingerprint=$(printf '%s\n' "$REDIS_PASSWORD" "$SENTINEL_PASSWORD" "$NODE_IP" "$PRIMARY_IP" "$MASTER_NAME" "$LAB_MODE" "$CLUSTER_BUS_PORT" "$CLUSTER_NODE_TIMEOUT_MS" | sha256sum | cut -d ' ' -f 1)
if [ -f /data/state/env.sha256 ] && [ "$(cat /data/state/env.sha256)" != "$fingerprint" ]; then
    echo 'Existing volume configuration differs from .env. Restore previous .env; see docs/06-operations.md. No data was removed.' >&2
    exit 78
fi
case "$LAB_MODE" in
    redis) config=/data/state/redis.conf; template=/opt/lab/config/redis.conf.template;;
    sentinel) config=/data/state/sentinel.conf; template=/opt/lab/config/sentinel.conf.template;;
    cluster) config=/data/state/redis.conf; template=/opt/lab/config/cluster.conf.template;;
    *) echo 'LAB_MODE must be redis, sentinel, or cluster' >&2; exit 64;;
esac
if [ -s "$config" ] && [ ! -s /data/state/env.sha256 ]; then
    echo 'Runtime config exists but its environment fingerprint is missing. Refusing unsafe reuse; restore matching state from backup.' >&2
    exit 78
fi
if [ "$LAB_MODE" = redis ]; then
    case "${INITIAL_ROLE:-replica}" in master|replica) ;; *) echo 'INITIAL_ROLE must be master or replica' >&2; exit 64;; esac
fi
if [ ! -s "$config" ]; then
    sed -e "s|@@REDIS_PASSWORD@@|$REDIS_PASSWORD|g" \
        -e "s|@@SENTINEL_PASSWORD@@|$SENTINEL_PASSWORD|g" \
        -e "s|@@NODE_IP@@|$NODE_IP|g" \
        -e "s|@@PRIMARY_IP@@|$PRIMARY_IP|g" \
        -e "s|@@MASTER_NAME@@|$MASTER_NAME|g" \
        -e "s|@@REDIS_MAXMEMORY@@|$REDIS_MAXMEMORY|g" \
        -e "s|@@DOWN_AFTER_MS@@|$DOWN_AFTER_MS|g" \
        -e "s|@@FAILOVER_TIMEOUT_MS@@|$FAILOVER_TIMEOUT_MS|g" \
        -e "s|@@CLUSTER_BUS_PORT@@|$CLUSTER_BUS_PORT|g" \
        -e "s|@@CLUSTER_NODE_TIMEOUT_MS@@|$CLUSTER_NODE_TIMEOUT_MS|g" \
        "$template" > "$config.tmp"
    if [ "$LAB_MODE" = redis ] && [ "${INITIAL_ROLE:-replica}" = replica ]; then
        printf '\nreplicaof %s 6379\n' "$PRIMARY_IP" >> "$config.tmp"
    fi
    printf '%s\n' "$fingerprint" > /data/state/env.sha256.tmp
    chmod 600 "$config.tmp" /data/state/env.sha256.tmp
    mv /data/state/env.sha256.tmp /data/state/env.sha256
    mv "$config.tmp" "$config"
    echo "Initialized $NODE_NAME ($LAB_MODE); later starts preserve runtime state."
else
    echo "Reusing $config; preserving previous failover state."
fi
# Older generated cluster configs only announced the bus port, but did not bind it.
# The fingerprint above has already checked the immutable settings. Add only the
# missing listener directive; retain node IDs, replication roles and all DB data.
if [ "$LAB_MODE" = cluster ] && ! grep -Eq '^[[:space:]]*cluster-port[[:space:]]' "$config"; then
    printf '\ncluster-port %s\n' "$CLUSTER_BUS_PORT" >> "$config"
fi
# Official entrypoint drops root to the image's redis user and fixes /data ownership.
# No host bind mounts, chmod 777, privileged mode, or SELinux disablement.
if [ "$LAB_MODE" = sentinel ]; then
    exec /usr/local/bin/docker-entrypoint.sh redis-server "$config" --sentinel
fi
exec /usr/local/bin/docker-entrypoint.sh redis-server "$config"
