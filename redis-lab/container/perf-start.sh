#!/bin/sh
set -eu
umask 077
: "${REDIS_PASSWORD:?Run ./lab.sh init first}"
case "$REDIS_PASSWORD" in *[!A-Za-z0-9_.@%+-]*|'') echo 'Unsafe password characters' >&2; exit 64;; esac
mkdir -p /data/db
# A fresh base config on every restart is intentional for this disposable perf node.
# Main HA nodes continue using the original, persistent Sentinel/Redis runtime configs.
cat > /data/perf.conf <<EOF
bind 0.0.0.0
protected-mode yes
port 6379
requirepass $REDIS_PASSWORD
daemonize no
logfile ""
dir /data/db
save ""
appendonly no
dbfilename dump.rdb
maxmemory 256mb
maxmemory-policy noeviction
activedefrag no
latency-monitor-threshold 2
slowlog-log-slower-than 1000
slowlog-max-len 128
maxclients 512
timeout 0
tcp-keepalive 30
EOF
exec /usr/local/bin/docker-entrypoint.sh redis-server /data/perf.conf
