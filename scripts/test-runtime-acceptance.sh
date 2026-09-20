#!/usr/bin/env bash
# No implicit up/pull/build/reset. Source/driver/DB checks precede any synthetic writes.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
exec bash "$ROOT/all.sh" mvp verify run "$@"
