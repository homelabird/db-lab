#!/usr/bin/env bash
set -e
exec podman exec -it mariadb-learning mariadb -ulab -plabpassword commerce_lab
