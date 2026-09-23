#!/usr/bin/env bash
# Real SSH transport and remote Ansible module transfer against an isolated local sshd.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for tool in ansible-playbook sshd ssh-keygen; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "BLOCKED: $tool is required for real SSH acceptance." >&2
    exit 127
  fi
done
cd "$ROOT"
python3 -B -m unittest discover -s ansible/tests -p 'test_ssh_target.py' -v
printf '\nPASS: real SSH transport to disposable local sshd; no DB engine was started.\n'
