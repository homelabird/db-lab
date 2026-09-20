#!/bin/sh
# Requires a majority of persisted Sentinels; never guesses that ordinal 0 is master.
set -eu
umask 077
case "$REDIS_PASSWORD" in *[!A-Za-z0-9_-]*|'') echo 'Invalid Redis Secret (24..128 URL-safe characters required).' >&2; exit 1;; esac
[ "${#REDIS_PASSWORD}" -ge 24 ] && [ "${#REDIS_PASSWORD}" -le 128 ] || exit 1
export REDISCLI_AUTH="$REDIS_PASSWORD"
mkdir -p /data
fingerprint=$(printf %s "$REDIS_PASSWORD" | sha256sum | cut -d' ' -f1)
if [ -f /data/auth.sha256 ] && [ "$(cat /data/auth.sha256)" != "$fingerprint" ]; then
  echo 'Secret changed. Coordinated credential rotation is required; refusing a partial rotation.' >&2; exit 1
fi
printf '%s\n' "$fingerprint" > /data/auth.sha256
master=''
round=0
while [ "$round" -lt 120 ]; do
  votes=/data/votes.$$
  : > "$votes"
  for ordinal in 0 1 2; do
    reply=$(timeout 3 redis-cli --raw -h "$SENTINEL_PREFIX-$ordinal.$SENTINEL_SERVICE" -p 26379 SENTINEL get-master-addr-by-name mymaster 2>/dev/null || true)
    host=$(printf '%s\n' "$reply" | sed -n '1p')
    port=$(printf '%s\n' "$reply" | sed -n '2p')
    # Sentinels in this chart announce stable, fully qualified hostnames.
    for node in 0 1 2; do
      [ "$host" != "$REDIS_PREFIX-$node.$REDIS_SERVICE" ] || {
        [ "$port" != 6379 ] || printf '%s\n' "$host" >> "$votes"
      }
    done
  done
  master=$(sort "$votes" | uniq -c | awk '$1 >= 2 { print $2; exit }')
  rm -f "$votes"
  [ -z "$master" ] || break
  round=$((round + 1)); sleep 2
 done
[ -n "$master" ] || { echo 'No agreeing Sentinel majority; refusing unsafe bootstrap.' >&2; exit 1; }
self="$POD_NAME.$REDIS_SERVICE"
if [ ! -s /data/redis.conf ]; then
  cat > /data/redis.conf <<EOF
bind 0.0.0.0
protected-mode yes
port 6379
dir /data
appendonly yes
appendfsync everysec
requirepass $REDIS_PASSWORD
masterauth $REDIS_PASSWORD
replica-announce-ip $self
replica-announce-port 6379
min-replicas-to-write 1
min-replicas-max-lag 10
EOF
fi
# Keep persistent AOF and tunings; change only the startup role discovered above.
sed '/^replicaof /d; /^slaveof /d' /data/redis.conf > /data/redis.conf.next
if [ "$self" != "$master" ]; then printf 'replicaof %s 6379\n' "$master" >> /data/redis.conf.next; fi
mv /data/redis.conf.next /data/redis.conf
exec redis-server /data/redis.conf
