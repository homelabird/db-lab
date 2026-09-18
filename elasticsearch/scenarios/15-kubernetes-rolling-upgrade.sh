#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
target="${1:-docker.elastic.co/elasticsearch/elasticsearch:7.17.29}"
./lab.sh k8s doctor
./lab.sh k8s apply
./lab.sh k8s verify
echo "[scenario] rolling upgrade target: $target"
./lab.sh k8s upgrade "$target"
./lab.sh k8s verify
