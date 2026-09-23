#!/usr/bin/env bash
# Deploys DB Lab and exercises a Redis lifecycle only inside a disposable Debian 12 SSH container.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for tool in ssh-keygen python3 ansible-playbook timeout; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "BLOCKED: $tool is required for disposable bootstrap acceptance." >&2
    exit 127
  fi
done
case "${DB_LAB_BOOTSTRAP_TEST_RUNTIME:-auto}" in
  auto)
    if command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then RUNTIME=podman
    elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then RUNTIME=docker
    else echo 'BLOCKED: a responsive Podman or Docker runtime is required.' >&2; exit 127; fi ;;
  podman|docker)
    RUNTIME=$DB_LAB_BOOTSTRAP_TEST_RUNTIME
    command -v "$RUNTIME" >/dev/null 2>&1 && "$RUNTIME" info >/dev/null 2>&1 || {
      echo "BLOCKED: selected runtime is unavailable: $RUNTIME" >&2; exit 127; }
    ;;
  *) echo 'BLOCKED: DB_LAB_BOOTSTRAP_TEST_RUNTIME must be auto, podman, or docker.' >&2; exit 2 ;;
esac
IMAGE=debian:12-slim
NAME="db-lab-bootstrap-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
TMP=$(mktemp -d)
umask 077
if [[ $RUNTIME == podman ]]; then
  mkdir -p "$TMP/runroot" "$TMP/tmpdir" "$TMP/networks"
  printf '{}\n' > "$TMP/auth.json"
  runtime() {
    local command=$1
    shift
    if [[ $command == pull ]]; then
      timeout --signal=TERM --kill-after=5 90 podman \
        --root "$TMP/storage" --runroot "$TMP/runroot" --tmpdir "$TMP/tmpdir" \
        --storage-driver=vfs --network-config-dir "$TMP/networks" \
        pull --authfile "$TMP/auth.json" "$@"
    else
      timeout --signal=TERM --kill-after=5 90 podman \
        --root "$TMP/storage" --runroot "$TMP/runroot" --tmpdir "$TMP/tmpdir" \
        --storage-driver=vfs --network-config-dir "$TMP/networks" "$command" "$@"
    fi
  }
  PRIVILEGED=(--privileged)
else
  runtime() { timeout --signal=TERM --kill-after=5 90 docker "$@"; }
  # The nested Debian target runs its own Podman engine; keep privilege scoped to this disposable container.
  PRIVILEGED=(--privileged)
fi
IMAGE_PREEXISTED=0
CONTAINER_MAY_EXIST=0
cleanup() {
  if runtime rm -f "$NAME" >/dev/null 2>&1 || (( CONTAINER_MAY_EXIST == 0 )); then
    if [[ $RUNTIME == docker ]] && (( IMAGE_PREEXISTED == 0 )); then
      runtime image rm "$IMAGE" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$TMP"
  else
    echo "WARNING: could not remove disposable container $NAME; container state may remain and test files are retained at $TMP" >&2
  fi
}
trap cleanup EXIT

if [[ $RUNTIME == docker ]]; then
  if runtime image inspect "$IMAGE" >/dev/null 2>&1; then
    IMAGE_PREEXISTED=1
  else
    image_check_status=$?
    if (( image_check_status != 1 )); then
      IMAGE_PREEXISTED=1
      echo 'BLOCKED: could not determine whether the base image already exists.' >&2
      exit "$image_check_status"
    fi
  fi
elif runtime image exists "$IMAGE"; then
  IMAGE_PREEXISTED=1
else
  image_check_status=$?
  if (( image_check_status != 1 )); then
    echo 'BLOCKED: could not inspect the isolated Podman image store.' >&2
    exit "$image_check_status"
  fi
fi

if (( IMAGE_PREEXISTED == 0 )); then
  for attempt in 1 2 3; do
    if runtime pull "$IMAGE"; then break; fi
    if (( attempt == 3 )); then exit 1; fi
    sleep 2
  done
fi
ssh-keygen -q -t ed25519 -N '' -f "$TMP/client_key"
CONTAINER_MAY_EXIST=1
runtime run -d "${PRIVILEGED[@]}" --name "$NAME" -p 127.0.0.1::22 "$IMAGE" sleep infinity >/dev/null
runtime exec "$NAME" apt-get update >/dev/null
runtime exec "$NAME" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  openssh-server python3 sudo >/dev/null
runtime exec "$NAME" useradd --create-home --shell /bin/bash labops
runtime exec "$NAME" python3 -c 'import secrets,subprocess; subprocess.run(["chpasswd"],input=("labops:"+secrets.token_urlsafe(32)+"\n").encode(),check=True)'
runtime exec "$NAME" install -d -o labops -g labops -m 0700 /home/labops/.ssh
runtime cp "$TMP/client_key.pub" "$NAME:/tmp/client_key.pub" >/dev/null
runtime exec "$NAME" install -o labops -g labops -m 0600 /tmp/client_key.pub /home/labops/.ssh/authorized_keys
runtime exec "$NAME" sh -c 'printf "%s\n" "labops ALL=(root) NOPASSWD: ALL" > /etc/sudoers.d/db-lab-test && chmod 0440 /etc/sudoers.d/db-lab-test && visudo -cf /etc/sudoers.d/db-lab-test'
runtime exec "$NAME" ssh-keygen -A >/dev/null
runtime exec "$NAME" install -d -m 0755 /run/sshd
runtime exec "$NAME" /usr/sbin/sshd -t -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no -o PubkeyAuthentication=yes \
  -o PermitRootLogin=no -o AuthorizedKeysFile=/home/labops/.ssh/authorized_keys -o StrictModes=no \
  -o UsePAM=no -o AllowUsers=labops -o PidFile=/run/db-lab-test-sshd.pid
runtime exec -d "$NAME" sh -c 'exec /usr/sbin/sshd -D -e -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no -o PubkeyAuthentication=yes \
  -o PermitRootLogin=no -o AuthorizedKeysFile=/home/labops/.ssh/authorized_keys -o StrictModes=no \
  -o UsePAM=no -o AllowUsers=labops -o PidFile=/run/db-lab-test-sshd.pid >/tmp/sshd.log 2>&1'

MAPPING=$(runtime port "$NAME" 22/tcp)
PORT=${MAPPING##*:}
HOST_KEY=$(runtime exec "$NAME" cat /etc/ssh/ssh_host_ed25519_key.pub)
export DB_LAB_TEST_TMP="$TMP" DB_LAB_TEST_NAME="$NAME" DB_LAB_TEST_PORT="$PORT" \
  DB_LAB_TEST_HOST_KEY="$HOST_KEY"
python3 - <<'PY'
import json, os
from pathlib import Path
tmp = Path(os.environ['DB_LAB_TEST_TMP'])
key_type, key_data, *_ = os.environ['DB_LAB_TEST_HOST_KEY'].split()
(tmp / 'known_hosts').write_text(f"[127.0.0.1]:{os.environ['DB_LAB_TEST_PORT']} {key_type} {key_data}\n")
inventory = {'all': {'children': {'db_lab': {'hosts': {'debian12': {
    'ansible_host': '127.0.0.1', 'ansible_user': 'labops',
    'ansible_port': int(os.environ['DB_LAB_TEST_PORT']),
    # Bootstrap itself opts into become only on the package-install task.
    'ansible_become_method': 'sudo',
    'ansible_python_interpreter': '/usr/bin/python3',
    'ansible_ssh_private_key_file': str(tmp / 'client_key'),
    'ansible_ssh_common_args': f"-o UserKnownHostsFile={tmp / 'known_hosts'} -o StrictHostKeyChecking=yes",
}}}}}}
(tmp / 'inventory.json').write_text(json.dumps(inventory))
(tmp / 'bootstrap.json').write_text(json.dumps({
    'db_lab_engine': 'podman', 'db_lab_allow_changes': True,
    'db_lab_bootstrap_confirmation': 'INSTALL:podman:bookworm-backports',
}))
PY
export ANSIBLE_CONFIG="$ROOT/ansible/ansible.cfg"
export ANSIBLE_LIBRARY="$ROOT/ansible/library"
export ANSIBLE_MODULE_UTILS="$ROOT/ansible/module_utils"
export ANSIBLE_LOCAL_TEMP="$TMP/ansible-local"
export ANSIBLE_HOST_KEY_CHECKING=True ANSIBLE_NOCOLOR=1
for _ in $(seq 1 40); do
  if ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
    -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
  labops@127.0.0.1 true >/dev/null 2>&1; then break; fi
  sleep 0.25
done
if ! ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
  -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
  labops@127.0.0.1 true; then
  runtime exec "$NAME" cat /tmp/sshd.log >&2 || true
  exit 1
fi
if [[ $(ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
  -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
  labops@127.0.0.1 sudo -n id -u) != 0 ]]; then
  echo 'FAIL: disposable SSH account cannot elevate through the configured sudo rule.' >&2
  exit 1
fi

run_bootstrap() {
  timeout --signal=TERM --kill-after=10 900 ansible-playbook -i "$TMP/inventory.json" "$ROOT/ansible/bootstrap.yml" \
    --limit debian12 -e "@$TMP/bootstrap.json"
}
run_bootstrap >"$TMP/first.log" 2>&1 || { tail -80 "$TMP/first.log" >&2; exit 1; }
grep -F 'TASK [Verify the selected engine and Compose frontend are installed]' "$TMP/first.log" >/dev/null || { tail -80 "$TMP/first.log" >&2; exit 1; }
run_bootstrap >"$TMP/repeat.log" 2>&1 || { tail -80 "$TMP/repeat.log" >&2; exit 1; }
grep -F 'changed=0' "$TMP/repeat.log" >/dev/null || { tail -80 "$TMP/repeat.log" >&2; exit 1; }
runtime exec "$NAME" install -d -m 0700 /run/containers/storage /var/lib/containers/storage
runtime exec "$NAME" sh -c 'printf "%s\n" "[storage]" "driver = \"vfs\"" "runroot = \"/run/containers/storage\"" "graphroot = \"/var/lib/containers/storage\"" > /etc/containers/storage.conf'
# The outer disposable container does not delegate writable child cgroups.
# Nested Podman therefore inherits its parent's cgroup; resource limits are not tested here.
runtime exec "$NAME" sh -c 'printf "%s\n" "[containers]" "cgroups = \"disabled\"" > /etc/containers/containers.conf'
runtime exec "$NAME" sh -c 'printf "%s\n" "#!/bin/sh" "exec sudo -n /usr/bin/podman \"\$@\"" > /usr/local/bin/podman && chmod 0755 /usr/local/bin/podman'

python3 - "$ROOT" "$TMP" <<'PY'
import json, sys
from pathlib import Path
root, tmp = map(Path, sys.argv[1:])
(tmp / 'deploy.json').write_text(json.dumps({
    'db_lab_source_root': str(root),
    'db_lab_deploy_root': '/home/labops/db-lab-deploy',
    'db_lab_allow_changes': True,
}))
PY
run_deploy() {
  local mode=$1
  local mode_args=()
  [[ -z $mode ]] || mode_args+=("$mode")
  timeout --signal=TERM --kill-after=10 900 ansible-playbook -i "$TMP/inventory.json" "$ROOT/ansible/deploy.yml" \
    "${mode_args[@]}" --limit debian12 -e "@$TMP/deploy.json"
}
run_deploy --check >"$TMP/deploy-check.log" 2>&1 || { tail -80 "$TMP/deploy-check.log" >&2; exit 1; }
if ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
  -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
  labops@127.0.0.1 test -e /home/labops/db-lab-deploy; then
  echo 'FAIL: Ansible deploy check mode created target files.' >&2; exit 1
fi
run_deploy '' >"$TMP/deploy.log" 2>&1 || { tail -80 "$TMP/deploy.log" >&2; exit 1; }
run_deploy '' >"$TMP/deploy-repeat.log" 2>&1 || { tail -80 "$TMP/deploy-repeat.log" >&2; exit 1; }
grep -F 'changed=0' "$TMP/deploy-repeat.log" >/dev/null || { tail -80 "$TMP/deploy-repeat.log" >&2; exit 1; }
RELEASE_ID=$(python3 - "$TMP/deploy.log" <<'PY'
import re, sys
text=open(sys.argv[1], encoding='utf-8').read()
matches=re.findall(r'release=([a-f0-9]{64}) path=', text)
if not matches: raise SystemExit('Could not find the deployed content-addressed release ID.')
print(matches[-1])
PY
)
python3 - "$TMP" "$RELEASE_ID" <<'PY'
import json, sys
from pathlib import Path
tmp, release = Path(sys.argv[1]), sys.argv[2]
(tmp/'control-base.json').write_text(json.dumps({
    'db_lab_project_root':f'/home/labops/db-lab-deploy/releases/{release}',
    'db_lab_target':'redis',
}))
PY
run_control() {
  local action=$1 consent=${2:-false}
  python3 - "$TMP" "$action" "$consent" <<'PY'
import json, sys
from pathlib import Path
tmp, action, consent = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'true'
data=json.loads((tmp/'control-base.json').read_text())
data['db_lab_request']={'action':action}
if consent: data['db_lab_allow_changes']=True
(tmp/'control.json').write_text(json.dumps(data))
PY
  timeout --signal=TERM --kill-after=10 1800 ansible-playbook -i "$TMP/inventory.json" "$ROOT/ansible/control.yml" \
    --limit debian12 -e "@$TMP/control.json"
}
for action in init doctor up health status down; do
  consent=false
  [[ $action == init || $action == up || $action == down ]] && consent=true
  run_control "$action" "$consent" >"$TMP/control-$action.log" 2>&1 || {
    tail -100 "$TMP/control-$action.log" >&2
    RELEASE_PATH="/home/labops/db-lab-deploy/releases/$RELEASE_ID"
    if [[ $action == doctor ]]; then
      ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
        -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
        labops@127.0.0.1 bash "$RELEASE_PATH/all.sh" doctor redis >"$TMP/doctor-native.log" 2>&1 || true
      echo '[diagnostic] native doctor output from the disposable target:' >&2
      tail -80 "$TMP/doctor-native.log" >&2
      ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
        -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
        labops@127.0.0.1 podman info --format '{{.Host.Security.Rootless}}' >&2 || true
    fi
LOG_DIR=$(python3 - "$TMP/control-$action.log" <<'PY'
import re, sys
text=open(sys.argv[1], encoding='utf-8').read()
match=re.search(r'"log_directory": "([^"]+)"', text)
if match: print(match.group(1))
PY
)
      if [[ -n $LOG_DIR ]]; then
        ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
          -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
          labops@127.0.0.1 cat "$RELEASE_PATH/redis-lab/.env" >"$TMP/remote.env" || true
        for stream in 00.stdout.log 00.stderr.log; do
          ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
            -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
            labops@127.0.0.1 cat "$LOG_DIR/$stream" >"$TMP/$stream" || true
        done
        python3 - "$TMP/remote.env" "$TMP/00.stdout.log" "$TMP/00.stderr.log" <<'PY' >&2
import sys
from pathlib import Path
secrets=[line.split('=',1)[1] for line in Path(sys.argv[1]).read_text().splitlines()
         if '=' in line and line.split('=',1)[0].endswith('PASSWORD')]
for name in sys.argv[2:]:
    lines=Path(name).read_text(errors='replace').splitlines()
    print(f'[diagnostic] sanitized native {Path(name).name}:')
    for line in lines[-100:]:
        for secret in secrets: line=line.replace(secret,'<redacted>')
        print(line)
PY
      fi
      ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
        -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
        labops@127.0.0.1 podman ps -a >&2 || true
    echo "FAIL: remote Ansible Redis action '$action' failed." >&2
    exit 1
  }
done
RELEASE_PATH="/home/labops/db-lab-deploy/releases/$RELEASE_ID"
ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
  -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
  labops@127.0.0.1 test -f "$RELEASE_PATH/redis-lab/.env"
ssh -p "$PORT" -i "$TMP/client_key" -o BatchMode=yes \
  -o UserKnownHostsFile="$TMP/known_hosts" -o StrictHostKeyChecking=yes \
  labops@127.0.0.1 podman volume inspect rslab-redis-1-data >/dev/null
printf 'PASS: Debian 12 backports apt bootstrap, source-only release deploy/reuse, and Redis init/doctor/up/health/down/status over non-root SSH; nested Podman inherits the disposable parent cgroup, so resource-limit enforcement was not tested (%s).\n' "$RUNTIME"
