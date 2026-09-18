#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
cd "$ROOT"
bash -n lab.sh
for f in scripts/*.sh; do bash -n "$f"; done
python3 -m unittest discover -s tests -v
