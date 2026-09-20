#!/bin/bash
set -euo pipefail
export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"
status=$(/opt/bitnami/mariadb/bin/mariadb -h 127.0.0.1 -uroot -N -B --connect-timeout=2 -e "SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_ready','wsrep_connected','wsrep_cluster_status','wsrep_local_state_comment')")
[[ "$status" == *$'wsrep_ready\tON'* && "$status" == *$'wsrep_connected\tON'* && "$status" == *$'wsrep_cluster_status\tPrimary'* && "$status" == *$'wsrep_local_state_comment\tSynced'* ]]
