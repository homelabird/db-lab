#!/usr/bin/env bash
# REAL Ansible tests with a harmless controller fixture. Does not run DB engines.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if ! command -v ansible-playbook >/dev/null 2>&1; then
  echo 'BLOCKED: ansible-playbook not installed. Install ansible/requirements.txt in an isolated controller virtualenv.' >&2
  exit 127
fi
cd "$ROOT/ansible"
ansible-playbook --version
python3 -B -m unittest discover -s tests -p 'test_playbooks_live.py' -v
printf '\nPASS: real Ansible + harmless controller fixture only, not SSH/DB runtime certification.\n'
