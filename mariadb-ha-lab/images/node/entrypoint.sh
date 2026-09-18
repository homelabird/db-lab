#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${1:-mariadbd} == mariadbd ]] || exec "$@"
mode=${LAB_MODE:-galera}
case "$mode" in
  galera|standalone) ;;
  *) echo '[lab] LAB_MODE must be galera or standalone.' >&2; exit 64 ;;
esac
# Lifecycle flags are managed by the lab, not arbitrary caller overrides.
if (( $# > 1 )); then
  echo '[lab] Additional mariadbd arguments are not supported; use documented .env settings.' >&2
  exit 64
fi
node=${NODE_NAME:-galera1}
[[ $node =~ ^[a-zA-Z0-9_-]+$ ]] || exit 64
[[ ${SST_PASSWORD:-} =~ ^[A-Za-z0-9_-]{12,128}$ ]] || { echo '[lab] Invalid SST_PASSWORD.' >&2; exit 64; }
[[ ${MARIADB_ROOT_PASSWORD:-} =~ ^[A-Za-z0-9_-]{12,128}$ ]] || { echo '[lab] Invalid MARIADB_ROOT_PASSWORD.' >&2; exit 64; }
mkdir -p /var/lib/mysql /var/lib/labctl /run/mysqld
chown mysql:mysql /var/lib/mysql /run/mysqld
chmod 1777 /run/mysqld
bootstrap=no
if [[ -f /var/lib/labctl/bootstrap-once ]]; then
  [[ $mode == galera ]] || { echo 'Bootstrap token on non-Galera node' >&2; exit 1; }
  bootstrap=yes
  # Consume BEFORE server startup. A failed startup must never silently re-bootstrap.
  rm -f /var/lib/labctl/bootstrap-once
fi
if [[ ! -d /var/lib/mysql/mysql ]]; then
  echo '[lab] Initializing local system tables with wsrep OFF and TCP disabled.'
  rm -f /var/lib/labctl/init-complete /run/mysqld/lab-init.pid
  /usr/local/bin/docker-entrypoint.sh mariadbd --wsrep-on=OFF --skip-networking \
    --pid-file=/run/mysqld/lab-init.pid &
  init_pid=$!
  cleanup_init() {
    kill -TERM "$init_pid" 2>/dev/null || true
    wait "$init_pid" 2>/dev/null || true
  }
  trap cleanup_init EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  initialized=no
  for ((i=0; i<300; i++)); do
    kill -0 "$init_pid" 2>/dev/null || { wait "$init_pid"; exit 1; }
    # The official entrypoint's TEMPORARY server has a different PID.
    # Wait until it execs its FINAL daemon, after all init scripts and temp shutdown.
    if [[ -f /run/mysqld/lab-init.pid ]] && [[ $(cat /run/mysqld/lab-init.pid) == "$init_pid" ]]; then
      if MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb --protocol=socket -uroot -Nse 'SELECT 1' >/dev/null 2>&1; then
        initialized=yes; break
      fi
    fi
    sleep 1
  done
  [[ $initialized == yes ]] || { echo '[lab] Initialization timeout.' >&2; exit 1; }
  MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb-admin --protocol=socket -uroot shutdown
  wait "$init_pid"
  trap - EXIT INT TERM
  touch /var/lib/labctl/init-complete
elif [[ ! -f /var/lib/labctl/init-complete ]]; then
  echo '[lab] Data exists but initialization marker is missing. Refusing an unsafe partial init.' >&2
  echo '[lab] Use lab.sh rebuild NODE --confirm-rebuild (healthy cluster) or reset a disposable lab.' >&2
  exit 1
fi
cat > /etc/mysql/conf.d/81-lab-runtime.cnf <<EOF
[mariadb]
innodb_buffer_pool_size=${BUFFER_POOL_SIZE:-256M}
EOF
chmod 644 /etc/mysql/conf.d/81-lab-runtime.cnf
args=(mariadbd "--server-id=${SERVER_ID:-1}")
if [[ $mode == galera ]]; then
  # Resolve this container's CURRENT address. No static host subnet or host networking required.
  address=$(python3 -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("192.0.2.1",9)); print(s.getsockname()[0]); s.close()')
  args+=(--wsrep-on=ON --wsrep-provider=/opt/lab/libgalera_smm.so
    --wsrep-cluster-name=mariadb-study "--wsrep-node-name=$node"
    "--wsrep-node-address=$address" "--wsrep-node-incoming-address=$node:3306"
    --wsrep-cluster-address=gcomm://galera1,galera2,galera3
    --wsrep-sst-method=mariabackup "--wsrep-sst-auth=sst:${SST_PASSWORD}"
    --wsrep-slave-threads=2
    "--wsrep-provider-options=gcache.size=${GCACHE_SIZE:-128M};gcache.recover=yes;pc.recovery=TRUE")
  if [[ $bootstrap == yes ]]; then
    args+=(--wsrep-new-cluster --wsrep-cluster-address=gcomm://)
  fi
fi
python3 /opt/lab/health.py &
exec /usr/local/bin/gosu mysql "${args[@]}"
