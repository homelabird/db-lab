#!/usr/bin/env bash
set -euo pipefail
# Password alphabet is deliberately restricted by lab.py, so SQL/config quoting is unambiguous.
for name in LAB_PASSWORD READONLY_PASSWORD SST_PASSWORD; do
  [[ ${!name:-} =~ ^[A-Za-z0-9_-]{12,128}$ ]] || { echo "Invalid $name" >&2; exit 1; }
done
MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb --protocol=socket -uroot <<SQL
CREATE USER IF NOT EXISTS 'sst'@'localhost' IDENTIFIED BY '${SST_PASSWORD}';
GRANT RELOAD, PROCESS, LOCK TABLES, BINLOG MONITOR, REPLICA MONITOR ON *.* TO 'sst'@'localhost';
CREATE USER IF NOT EXISTS 'lab'@'%' IDENTIFIED BY '${LAB_PASSWORD}';
GRANT ALL PRIVILEGES ON commerce_lab.* TO 'lab'@'%';
GRANT ALL PRIVILEGES ON lab_ops.* TO 'lab'@'%';
CREATE USER IF NOT EXISTS 'readonly'@'%' IDENTIFIED BY '${READONLY_PASSWORD}';
GRANT SELECT, SHOW VIEW ON commerce_lab.* TO 'readonly'@'%';
GRANT SELECT, SHOW VIEW ON lab_ops.* TO 'readonly'@'%';
SQL
