#!/bin/bash
set -euo pipefail
trap 'exit 143' TERM INT

ordinal=${POD_NAME##*-}
bootstrap_ordinal=$RECOVERY_ORDINAL
if (( bootstrap_ordinal < 0 )); then
  [[ "$BOOTSTRAP_NEW_CLUSTER" == yes ]] || exit 0
  bootstrap_ordinal=0
fi
[[ "$ordinal" == "$bootstrap_ordinal" ]] && exit 0
export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"

for ((attempt=0; attempt<180; attempt++)); do
  if getent hosts "$GALERA_BOOTSTRAP_HOST" >/dev/null 2>&1; then
    status=$(/opt/bitnami/mariadb/bin/mariadb -h "$GALERA_BOOTSTRAP_HOST" -uroot -N -B \
      --connect-timeout=1 \
      -e "SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_ready','wsrep_connected','wsrep_cluster_status','wsrep_local_state_comment')" \
      2>/dev/null || true)
    if [[ "$status" == *$'wsrep_ready\tON'* \
      && "$status" == *$'wsrep_connected\tON'* \
      && "$status" == *$'wsrep_cluster_status\tPrimary'* \
      && "$status" == *$'wsrep_local_state_comment\tSynced'* ]]; then
      exit 0
    fi
  fi
  sleep 1
done

echo "Timed out waiting for a Primary/Synced Galera bootstrap peer at $GALERA_BOOTSTRAP_HOST" >&2
exit 1
