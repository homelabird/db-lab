#!/bin/bash
# Conservative Galera bootstrap: fresh data and recovery are different operations.
set -euo pipefail
for key in MARIADB_ROOT_PASSWORD MARIADB_GALERA_MARIABACKUP_PASSWORD; do
  value=${!key}
  [[ "$value" =~ ^[A-Za-z0-9_-]{24,128}$ ]] || { echo "Invalid Secret key for $key" >&2; exit 1; }
done
ordinal=${POD_NAME##*-}
export MARIADB_GALERA_NODE_NAME="$POD_NAME"
export MARIADB_GALERA_NODE_ADDRESS="$POD_NAME.$PEER_SERVICE"
export MARIADB_GALERA_CLUSTER_BOOTSTRAP=no
export MARIADB_GALERA_CLUSTER_ADDRESS="gcomm://$GALERA_PEERS"
existing=no
[[ ! -d /bitnami/mariadb/data/mysql ]] || existing=yes
if [[ "$RECOVERY_ORDINAL" == "$ordinal" ]]; then
  [[ "$existing" == yes ]] || { echo 'Recovery requires existing data.' >&2; exit 1; }
  grep -Eq '^safe_to_bootstrap:[[:space:]]*1[[:space:]]*$' /bitnami/mariadb/data/grastate.dat || {
    echo 'Recovery refused: offline seqno/UUID review and safe_to_bootstrap=1 are required.' >&2; exit 1;
  }
  export MARIADB_GALERA_CLUSTER_BOOTSTRAP=yes
  export MARIADB_GALERA_CLUSTER_ADDRESS=gcomm://
elif [[ "$ordinal" == 0 && "$BOOTSTRAP_NEW_CLUSTER" == yes && "$existing" == no ]]; then
  # This flag must be sealed to false after first install, before allowing writes.
  export MARIADB_GALERA_CLUSTER_BOOTSTRAP=yes
  export MARIADB_GALERA_CLUSTER_ADDRESS=gcomm://
fi
# Never force safe_to_bootstrap or choose a recovery node merely by its ordinal.
exec /opt/bitnami/scripts/mariadb-galera/entrypoint.sh /opt/bitnami/scripts/mariadb-galera/run.sh
