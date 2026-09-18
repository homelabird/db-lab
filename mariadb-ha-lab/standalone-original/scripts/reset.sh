#!/usr/bin/env bash
set -e
podman compose -f mariadb.yaml down -v
podman compose -f mariadb.yaml up -d
