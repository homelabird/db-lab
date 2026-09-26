#!/usr/bin/env bash
# Root controller for four labs, the integrated MVP, and Helm deployments.
# Never source a child lab: each one owns its environment, runtime and safeguards.
set -Eeuo pipefail

if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
  printf 'ERROR: all.sh requires Bash 4.4 or newer.\n' >&2
  exit 2
fi

# Resolve symlinks, and work independently of the caller's current directory.
SCRIPT=${BASH_SOURCE[0]}
while [[ -L "$SCRIPT" ]]; do
  SCRIPT_DIR=$(cd -- "$(dirname -- "$SCRIPT")" && pwd -P)
  SCRIPT=$(readlink -- "$SCRIPT")
  [[ "$SCRIPT" == /* ]] || SCRIPT="$SCRIPT_DIR/$SCRIPT"
done
ROOT=$(cd -- "$(dirname -- "$SCRIPT")" && pwd -P)
CALLER_DIR=$PWD
DRY_RUN=0
FAIL_FAST=0
PROJECTS=(elasticsearch kafka mariadb redis)
LIFECYCLE_PROJECTS=("${PROJECTS[@]}" mvp)
SELECTED=()
RUN_FILE=
RUN_ACTION=
LOCK_PID=
JSON_OUTPUT=0

trap 'printf "\nInterrupted; no further projects will be started.\n" >&2; exit 130' INT
trap 'printf "\nTerminated; no further projects will be started.\n" >&2; exit 143' TERM

error() { printf 'ERROR: %s\n' "$*" >&2; }
usage_error() { error "$*"; exit 2; }

finish_run() {
  local code=$1
  if [[ -n "${LOCK_PID:-}" ]]; then
    kill "$LOCK_PID" 2>/dev/null || true
    wait "$LOCK_PID" 2>/dev/null || true
    LOCK_PID=
  fi
  [[ -n "$RUN_FILE" ]] || return 0
  python3 "$ROOT/scripts/control.py" record "$RUN_FILE" "$RUN_ACTION" __finish__ "$code" || true
  printf 'Run receipt: %s\n' "$RUN_FILE" >&2
}
begin_operation() {
  (( ! DRY_RUN )) || return 0
  [[ -z "$RUN_FILE" ]] || return 0
  command -v flock >/dev/null 2>&1 || { error 'flock is required for root-level mutation locking.'; return 127; }
  command -v python3 >/dev/null 2>&1 || { error 'python3 is required for root run receipts.'; return 127; }
  # Fail closed on redirected runtime storage, before creating receipts/locks.
  # These are local accident guards, not isolation from a privileged operator.
  local storage
  for storage in "$ROOT/.state" "$ROOT/reports" "$ROOT/.state/all.lock"; do
    [[ ! -L "$storage" ]] || { error 'Symlinked runtime storage is refused; inspect the original path.'; return 1; }
  done
  for storage in "$ROOT/.state" "$ROOT/reports"; do
    [[ ! -e "$storage" || -d "$storage" ]] || { error 'Runtime storage must be a directory.'; return 1; }
  done
  (umask 077; mkdir -p "$ROOT/.state" "$ROOT/reports") || return
  # Hold the lock in a background subshell so child labs/containers never
  # inherit it. Holding it on a shell fd (exec 9>>...) leaks into every
  # child via fork, and container supervisors keep it open after all.sh
  # exits, blocking all later lifecycle operations.
  ( flock -n 200 || exit 1; exec sleep infinity ) 200>>"$ROOT/.state/all.lock" &
  LOCK_PID=$!
  sleep 0.2
  if ! kill -0 "$LOCK_PID" 2>/dev/null; then
    wait "$LOCK_PID" 2>/dev/null || true
    LOCK_PID=
    error 'Another root lifecycle operation is active; no stale lock deletion is necessary.'
    return 1
  fi
  RUN_ACTION=$1
  RUN_FILE="$ROOT/reports/all-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
  trap 'finish_run "$?"' EXIT
  python3 "$ROOT/scripts/control.py" record "$RUN_FILE" "$RUN_ACTION" __start__ 0 || return
}
record_project() {
  [[ -n "$RUN_FILE" ]] || return 0
  python3 "$ROOT/scripts/control.py" record "$RUN_FILE" "$RUN_ACTION" "$1" "$2"
}

usage() {
  cat <<'HELP'
db-lab root controller

Usage:
  ./all.sh [--dry-run] [--fail-fast] COMMAND [PROJECT ...]
  ./all.sh [--dry-run] PROJECT [LAB_COMMAND [ARGS ...]]
  ./all.sh [--dry-run] k8s COMMAND [HELM_ARGS ...]
  ./all.sh [--dry-run] mvp COMMAND [ARGS ...]
  # Mutating k8s commands require DB_LAB_CONTEXT and an allowed context.
  # k8s init creates/preserves credentials; no passwords are printed.

Projects:
  elasticsearch (es) | kafka (kafka-lab) | mariadb (mariadb-ha-lab)
  redis (redis-lab)  | mvp (integrated six-container study stack)
  elasticsearch-9 (es9, opt-in, 9.x; NOT in default batches) | all

Common commands (up/down/restart include the four labs and MVP; other commands default to the four labs):
  list                    List project directories and entrypoints
  init                    Initialize .env; preserve existing settings/secrets
  preflight [--json]      Read-only engine/settings/port-plan checks
  health [--json]         Application/topology checks; nonzero when unhealthy
  doctor | check          Run each lab's prerequisite checks
  benchmark DATABASE     Run bounded workload; --repeat 3 compares fresh runs
  up | start             Initialize and start four labs plus MVP, sequentially
  status | ps            Run each lab's native status command
  down | stop            Stop four labs and MVP in reverse order; retain volumes
  restart                Restart four labs and MVP; retain volumes
  test | offline-tests   Run the existing host-only test suites
  self-test              Test all.sh itself, without container runtimes
  reset PROJECT... --yes  Delete selected HA-lab data; MVP is not reset here
  logs PROJECT [ARGS...] Forward log arguments to ONE lab
  help                    Show this help

Project-specific operations are passed through without rewriting arguments:
  ./all.sh kafka seed --kind payments --count 1000
  ./all.sh redis up --no-build
  ./all.sh mariadb sql galera1 'SELECT 1;'
  ./all.sh es scenario list
  ./all.sh logs kafka kafka1 --follow
  ./all.sh logs redis redis-1 --follow
  ./all.sh mariadb --help

Benchmark operations target an already-running lab; they do not start, reset,
or delete its services. See ./all.sh benchmark --help for workload options.

MVP (independent six-container order system; included in common up/down/restart):
  ./all.sh mvp up                   Build/start the small integrated study lab
  ./all.sh mvp diagnose             Dependency status, outbox and Kafka lag
  ./all.sh mvp smoke                Write one fake order and verify search delivery
  ./all.sh mvp stop kafka --yes     Deliberately stop only the MVP broker
  ./all.sh mvp resume kafka         Restart it with the same volume
  ./all.sh mvp experiment redis-spare --yes
  ./all.sh mvp --help               See all commands and scoped experiments

Kubernetes (independent of the four Compose/container labs):
  ./all.sh k8s init                 Create/preserve namespace and credentials
  ./all.sh k8s preflight [HELM_ARGS...]  Validate explicit target and manifests
  ./all.sh k8s lint [HELM_ARGS...]
  ./all.sh k8s template [HELM_ARGS...]
  ./all.sh k8s up [HELM_ARGS...]       upgrade --install, wait for readiness
  ./all.sh k8s status [HELM_ARGS...]
  ./all.sh k8s history [HELM_ARGS...]
  ./all.sh k8s values
  ./all.sh k8s down --yes [HELM_ARGS...]  uninstall this release
  Environment: DB_LAB_RELEASE=db-lab, DB_LAB_NAMESPACE=db-lab,
               DB_LAB_TIMEOUT=10m, DB_LAB_CONTEXT (required for writes),
               DB_LAB_ALLOWED_CONTEXTS=kind-db-lab,k3d-db-lab,minikube.
               DB_LAB_KUBECONFIG is optional; otherwise KUBECONFIG is inherited.
  'helm' and 'helmchart' are aliases for 'k8s'.

Options:
  --dry-run      Print commands without running children or writing files.
  --fail-fast    Stop a common/batch operation on its first failure.
                 Default: continue with the other labs and report failures.
  --yes, -y      Required for common reset and k8s down only.
                 Common reset requires a project or literal 'all'.
  Put global options BEFORE project-specific / k8s commands.

Examples:
  ./all.sh init
  ./all.sh up kafka redis
  ./all.sh --dry-run down
  ./all.sh --fail-fast up
  ./all.sh reset redis --yes
  ./all.sh reset all --yes
  ./all.sh k8s up -f ./my-values.yaml --set kafka.enabled=false

No arguments means help, never automatic startup or deletion.
MVP is included only in default up/down/restart batches; other defaults remain the four HA labs. Common reset does not delete MVP data.
The root controller does not use sudo, prune, or delete PVCs/namespaces.
Native project commands retain their OWN meanings and confirmation flags.
Native relative file arguments resolve inside that project's directory;
Helm file arguments resolve from your current working directory.
Exit: 0=commands succeeded (not a health guarantee), 1=batch failure,
      2=usage error. Direct child/Helm calls preserve their exit status.
HELP
}

canonical_project() {
  case "$1" in
    es|elastic|elasticsearch) printf '%s\n' elasticsearch ;;
    es9|elasticsearch-9|elasticsearch9) printf '%s\n' elasticsearch9 ;;
    kafka|kafka-lab) printf '%s\n' kafka ;;
    maria|mariadb|mariadb-ha-lab) printf '%s\n' mariadb ;;
    redis|redis-lab) printf '%s\n' redis ;;
    mvp) printf '%s\n' mvp ;;
    *) return 1 ;;
  esac
}

project_dir() {
  case "$1" in
    elasticsearch) printf '%s/elasticsearch\n' "$ROOT" ;;
    elasticsearch9) printf '%s/elasticsearch-9\n' "$ROOT" ;;
    kafka) printf '%s/kafka-lab\n' "$ROOT" ;;
    mariadb) printf '%s/mariadb-ha-lab\n' "$ROOT" ;;
    redis) printf '%s/redis-lab\n' "$ROOT" ;;
    mvp) printf '%s/mvp-lab\n' "$ROOT" ;;
    *) error "Unknown project: $1"; return 2 ;;
  esac
}

test_script() {
  case "$1" in
    elasticsearch|elasticsearch9) printf '%s\n' scripts/12-offline-tests.sh ;;
    kafka) printf '%s\n' scripts/test-static.sh ;;
    mariadb) printf '%s\n' tests/validate.sh ;;
    redis) printf '%s\n' tests/check.sh ;;
    *) return 2 ;;
  esac
}

run_at() {
  local dir=$1
  shift
  if (( DRY_RUN )); then
    printf '(cd %q && ' "$dir"
    printf '%q ' "$@"
    printf ')\n'
  else
    (cd -- "$dir" && "$@")
  fi
}

require_file() {
  [[ -f "$1" && -r "$1" ]] || { error "Missing/unreadable file: $1"; return 1; }
}

run_lab() {
  local dir
  dir=$(project_dir "$1") || return
  shift
  require_file "$dir/lab.sh" || return
  # Execute with bash even when ZIP extraction did not retain executable bits.
  run_at "$dir" bash "$dir/lab.sh" "$@"
}

copy_env_if_missing() {
  local dir=$1
  if [[ -e "$dir/.env" || -L "$dir/.env" ]]; then
    require_file "$dir/.env" || return
    printf 'Preserved %s/.env\n' "$dir"
    return 0
  fi
  require_file "$dir/.env.example" || return
  if (( DRY_RUN )); then
    printf '[dry-run] create %q from %q (0600, no overwrite)\n' \
      "$dir/.env" "$dir/.env.example"
    return 0
  fi
  # noclobber also refuses a file created after the existence check above.
  (umask 077; set -C; cat -- "$dir/.env.example" > "$dir/.env") || return
  printf 'Created %s/.env (0600)\n' "$dir"
}

init_project() {
  local project=$1 dir
  dir=$(project_dir "$project") || return
  case "$project" in
    elasticsearch|elasticsearch9|kafka) copy_env_if_missing "$dir" ;;
    # Native initializers generate random passwords and preserve valid ones.
    mariadb|redis|mvp) run_lab "$project" init ;;
  esac
}

select_projects() {
  local item project seen existing
  if (( $# == 0 )); then
    case "$action" in
      up|down|restart) SELECTED=("${LIFECYCLE_PROJECTS[@]}") ;;
      *) SELECTED=("${PROJECTS[@]}") ;;
    esac
    return
  fi
  SELECTED=()
  for item in "$@"; do
    if [[ "$item" == all ]]; then
      (( $# == 1 )) || usage_error "Use 'all' alone, not with other project names."
      case "$action" in
        up|down|restart) SELECTED=("${LIFECYCLE_PROJECTS[@]}") ;;
        *) SELECTED=("${PROJECTS[@]}") ;;
      esac
      return
    fi
    project=$(canonical_project "$item") || usage_error "Unknown project: $item"
    [[ "$action" != reset || "$project" != mvp ]] || usage_error "Common reset does not delete MVP data; use './all.sh mvp down' to stop it."
    seen=0
    for existing in "${SELECTED[@]}"; do
      [[ "$existing" != "$project" ]] || seen=1
    done
    (( seen )) || SELECTED+=("$project")
  done
}

preflight() {
  local action=$1 project dir script
  # Check the whole selection before starting any project's operation.
  for project in "${SELECTED[@]}"; do
    dir=$(project_dir "$project") || return
    require_file "$dir/lab.sh" || return
    case "$action" in
      init|up|restart)
        if [[ "$project" == mvp ]]; then
          [[ ! -L "$dir/.env" ]] || { error 'Symlinked MVP .env is refused.'; return 1; }
          if [[ -e "$dir/.env" ]]; then require_file "$dir/.env" || return
          else require_file "$dir/.env.example" || return; fi
        elif [[ "$project" == elasticsearch || "$project" == elasticsearch9 || "$project" == kafka ]]; then
          if [[ ! -e "$dir/.env" && ! -L "$dir/.env" ]]; then
            require_file "$dir/.env.example" || return
          else
            require_file "$dir/.env" || return
          fi
        fi
        ;;
      test)
        [[ "$project" != mvp ]] || continue
        script=$(test_script "$project") || return
        require_file "$dir/$script" || return
        ;;
    esac
  done
}

perform_project() {
  local action=$1 project=$2 dir script
  case "$action" in
    init) init_project "$project" ;;
    up)
      init_project "$project" || return
      run_lab "$project" up
      ;;
    restart)
      # A failed shutdown must never be followed by an automatic startup.
      run_lab "$project" down || return
      init_project "$project" || return
      run_lab "$project" up
      ;;
    doctor|status) run_lab "$project" "$action" ;;
    down)
      if [[ "$project" == mvp && ! -e "$ROOT/mvp-lab/.env" && ! -L "$ROOT/mvp-lab/.env" ]]; then
        printf 'MVP is not initialized; nothing to stop.\n' >&2
        return 0
      fi
      run_lab "$project" down
      ;;
    reset)
      case "$project" in
        elasticsearch|elasticsearch9) run_lab "$project" down --purge --yes ;;
        kafka) run_lab "$project" reset --yes ;;
        mariadb) run_lab "$project" reset --confirm-delete-lab-data ;;
        redis) run_lab "$project" reset --yes ;;
      esac
      ;;
    test)
      if [[ "$project" == mvp ]]; then run_lab mvp test; return; fi
      dir=$(project_dir "$project") || return
      script=$(test_script "$project") || return
      run_at "$dir" bash "$dir/$script"
      ;;
    *) error "Unsupported common operation: $action"; return 2 ;;
  esac
}

run_batch() {
  local action=$1 project rc failed=0 stopped=0 i
  local ordered=("${SELECTED[@]}") results=()
  preflight "$action" || return
  case "$action" in
    up|restart)
      if (( DRY_RUN )); then
        printf '[dry-run] read-only preflight for: %s\n' "${SELECTED[*]}" >&2
      else
        python3 "$ROOT/scripts/control.py" preflight "${SELECTED[@]}" || return
      fi
      ;;
  esac
  case "$action" in init|up|restart|down|reset) begin_operation "$action" || return;; esac
  if [[ "$action" == down || "$action" == reset ]]; then
    ordered=()
    for (( i=${#SELECTED[@]}-1; i>=0; i-- )); do
      ordered+=("${SELECTED[i]}")
    done
  fi
  if [[ "$action" == reset ]]; then
    printf 'WARNING: deleting database data for: %s\n' "${ordered[*]}" >&2
  fi
  for project in "${ordered[@]}"; do
    if (( stopped )); then
      results+=("$project: SKIPPED")
      record_project "$project" -1
      continue
    fi
    printf '\n=== %s / %s ===\n' "$project" "$action" >&2
    if perform_project "$action" "$project"; then
      if (( DRY_RUN )); then results+=("$project: PLANNED"); else results+=("$project: OK"); record_project "$project" 0; fi
    else
      rc=$?
      results+=("$project: FAILED (exit $rc)")
      record_project "$project" "$rc"
      failed=1
      # Never continue a batch after an interrupted child, even without fail-fast.
      if (( rc == 130 || rc == 143 )); then
        printf '%s\n' "${results[@]}" >&2
        return "$rc"
      fi
      (( ! FAIL_FAST )) || stopped=1
    fi
  done
  printf '\n=== %s summary ===\n' "$action" >&2
  printf '%s\n' "${results[@]}" >&2
  return "$failed"
}

k8s_command() {
  local action=${1:-help} arg yes=0 approved=0 entry guard_result expect_value=0
  local release=${DB_LAB_RELEASE:-db-lab} namespace=${DB_LAB_NAMESPACE:-db-lab}
  local timeout=${DB_LAB_TIMEOUT:-10m} context=${DB_LAB_CONTEXT:-}
  local chart="$ROOT/helmchart" args=() target=() guard=()
  (( $# == 0 )) || shift
  case "$action" in
    help|-h|--help) usage; return ;;
    up|start|install|upgrade) action=up ;;
    down|stop|uninstall) action=down ;;
    init|lint|template|status|history|values|preflight) ;;
    *) usage_error "Unknown k8s command: $action" ;;
  esac
  for arg in "$@"; do
    if [[ "$action" == down && ( "$arg" == --yes || "$arg" == -y ) ]]; then yes=1
    else
      case "$arg" in --kube-context*|--kubeconfig*|--namespace*|-n|-n?*)
        usage_error 'Use DB_LAB_CONTEXT / DB_LAB_KUBECONFIG / DB_LAB_NAMESPACE; target overrides via Helm arguments are refused.';; esac
      args+=("$arg")
    fi
  done
  # Up/preflight accept only values flags. Do not let a post-renderer, --force,
  # --wait=false, or another values-reuse mode bypass the checked plan.
  if [[ "$action" == up || "$action" == preflight ]]; then
    for arg in "${args[@]}"; do
      if (( expect_value )); then expect_value=0; continue; fi
      case "$arg" in
        -f|--values|--set|--set-string|--set-json|--set-file) expect_value=1 ;;
        --values=*|--set=*|--set-string=*|--set-json=*|--set-file=*|-f?*) ;;
        *) usage_error 'k8s up/preflight accept only values flags (-f/--values/--set variants); lifecycle and validation options are controlled.' ;;
      esac
    done
    (( ! expect_value )) || usage_error 'A values option is missing its argument.'
  fi
  [[ "$action" != down || "$yes" == 1 ]] || usage_error 'Uninstall stops the release. Use: ./all.sh k8s down --yes'
  [[ "$namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#namespace} -le 63 ]] || usage_error 'Invalid lab namespace.'
  [[ "$release" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#release} -le 53 ]] || usage_error 'Invalid Helm release name.'
  if (( ! DRY_RUN )); then
    command -v helm >/dev/null 2>&1 || { error 'helm is required for k8s commands.'; return 127; }
  fi
  case "$action" in
    up|down|init|preflight)
      case "$namespace" in default|kube-system|kube-public|kube-node-lease) usage_error 'Protected namespace; select a dedicated lab namespace.';; esac
      if (( ! DRY_RUN )); then
        [[ -n "$context" ]] || usage_error 'Set DB_LAB_CONTEXT explicitly. The current context is never silently used for writes.'
        IFS=',' read -r -a allowed_contexts <<< "${DB_LAB_ALLOWED_CONTEXTS:-kind-db-lab,k3d-db-lab,minikube}"
        for entry in "${allowed_contexts[@]}"; do [[ "$context" != "$entry" ]] || approved=1; done
        (( approved )) || usage_error 'DB_LAB_CONTEXT is not in DB_LAB_ALLOWED_CONTEXTS.'
        command -v kubectl >/dev/null 2>&1 || { error 'kubectl is required for target and Secret preflight.'; return 127; }
      fi
      printf 'Kubernetes target: context=%s namespace=%s release=%s action=%s\n' "${context:-<set DB_LAB_CONTEXT>}" "$namespace" "$release" "$action" >&2
      ;;
  esac
  [[ -z "$context" ]] || target+=(--kube-context "$context")
  [[ -z "${DB_LAB_KUBECONFIG:-}" ]] || target+=(--kubeconfig "$DB_LAB_KUBECONFIG")
  guard=(--context "${context:-<set DB_LAB_CONTEXT>}" --namespace "$namespace" --release "$release" --chart "$chart")
  [[ -z "${DB_LAB_KUBECONFIG:-}" ]] || guard+=(--kubeconfig "$DB_LAB_KUBECONFIG")
  case "$action" in
    up|down|init) begin_operation "k8s:$action" || return ;;
  esac
  case "$action" in
    init)
      (( ${#args[@]} == 0 )) || usage_error 'k8s init takes no Helm flags; use DB_LAB_SECRET for a custom Secret name.'
      run_at "$CALLER_DIR" python3 "$ROOT/scripts/k8s_guard.py" init "${guard[@]}" --secret "${DB_LAB_SECRET:-db-lab-credentials}" ;;
    preflight) run_at "$CALLER_DIR" python3 "$ROOT/scripts/k8s_guard.py" preflight "${guard[@]}" -- "${args[@]}" ;;
    up)
      require_file "$chart/Chart.yaml" || return
      if (( DRY_RUN )); then
        run_at "$CALLER_DIR" python3 "$ROOT/scripts/k8s_guard.py" preflight "${guard[@]}" -- "${args[@]}"
        guard_result='{"seal_needed":false}'
      else
        guard_result=$(cd "$CALLER_DIR" && python3 "$ROOT/scripts/k8s_guard.py" preflight "${guard[@]}" -- "${args[@]}") || return
        printf '%s\n' "$guard_result" >&2
      fi
      run_at "$CALLER_DIR" helm upgrade --install "$release" "$chart" --namespace "$namespace" \
        --reset-then-reuse-values --create-namespace --wait --timeout "$timeout" "${target[@]}" "${args[@]}" || return
      if (( ! DRY_RUN )) && [[ "$guard_result" == *'"seal_needed": true'* ]]; then
        echo 'Sealing first-install/recovery flags before returning. Failure requires manual sealing before data loads.' >&2
        run_at "$CALLER_DIR" helm upgrade "$release" "$chart" --namespace "$namespace" "${target[@]}" \
          --reuse-values --wait --timeout "$timeout" --set elasticsearch.bootstrapNewCluster=false \
          --set mariadb.bootstrapNewCluster=false --set mariadb.recovery.bootstrapOrdinal=-1 --set mariadb.recovery.confirmed=false
      fi ;;
    down) run_at "$CALLER_DIR" helm uninstall "$release" --namespace "$namespace" "${target[@]}" "${args[@]}" ;;
    status|history) run_at "$CALLER_DIR" helm "$action" "$release" --namespace "$namespace" "${target[@]}" "${args[@]}" ;;
    lint) require_file "$chart/Chart.yaml" || return; run_at "$CALLER_DIR" helm lint "$chart" "${args[@]}" ;;
    template) require_file "$chart/Chart.yaml" || return; run_at "$CALLER_DIR" helm template "$release" "$chart" --namespace "$namespace" "${args[@]}" ;;
    values) require_file "$chart/Chart.yaml" || return; run_at "$CALLER_DIR" helm show values "$chart" "${args[@]}" ;;
  esac
}

main() {
  local action project item yes=0
  local targets=()
  while (( $# )); do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --fail-fast) FAIL_FAST=1; shift ;;
      -h|--help) usage; return ;;
      --) shift; break ;;
      -*) usage_error "Unknown global option: $1" ;;
      *) break ;;
    esac
  done
  action=${1:-help}
  (( $# == 0 )) || shift
  # A project-first invocation is intentionally a native pass-through.
  if project=$(canonical_project "$action"); then
    (( $# )) || set -- --help
    case "${1:-help}" in
      help|-h|--help|status|summary|health|doctor|check|config|scenarios|logs|zk|kraft-status) ;;
      *) begin_operation "native:$project:${1:-help}" || return ;;
    esac
    run_lab "$project" "$@"
    return
  fi
  case "$action" in
    help) usage; return ;;
    k8s|helm|helmchart) k8s_command "$@"; return ;;
    mvp|mvp-lab)
      require_file "$ROOT/mvp-lab/lab.sh" || return
      case "${1:-help}" in
        help|-h|--help|status|doctor|diagnose|logs|test) ;;
        *) begin_operation "mvp:${1:-help}" || return ;;
      esac
      [[ "${1:-}" != help ]] || shift
      run_at "$ROOT/mvp-lab" bash "$ROOT/mvp-lab/lab.sh" "$@"
      return ;;

    benchmark)
      (( $# )) || usage_error 'benchmark requires a database: es7, es9, mariadb, kafka, or redis.'
      require_file "$ROOT/scripts/benchmarks.py" || return
      local show_help=0 option
      for option in "$@"; do
        [[ "$option" == -h || "$option" == --help ]] && show_help=1
      done
      if (( DRY_RUN )); then
        run_at "$ROOT" python3 "$ROOT/scripts/benchmarks.py" run "$@" --dry-run
      elif (( show_help )); then
        run_at "$ROOT" python3 "$ROOT/scripts/benchmarks.py" run "$@"
      else
        begin_operation "benchmark:${1}" || return
        run_at "$ROOT" python3 "$ROOT/scripts/benchmarks.py" run "$@"
      fi
      return ;;

    list)
      (( $# == 0 )) || usage_error 'list does not take arguments.'
      printf '%-16s %-24s %s\n' PROJECT DIRECTORY ENTRYPOINT
      for project in "${PROJECTS[@]}"; do
        item=$(project_dir "$project")
        printf '%-16s %-24s %s\n' "$project" "${item##*/}/" lab.sh
      done
      printf '%-16s %-24s %s\n' elasticsearch9 elasticsearch-9/ 'all.sh es9 (opt-in; not in the default batch)'
      printf '%-16s %-24s %s\n' k8s helmchart/ 'all.sh k8s (separate deployment)'
      printf '%-16s %-24s %s\n' mvp mvp-lab/ 'all.sh mvp (included in default up/down/restart)'
      return
      ;;
    self-test)
      (( $# == 0 )) || usage_error 'self-test does not take arguments.'
      require_file "$ROOT/tests/test_all.py" || return
      run_at "$ROOT" python3 -B -m unittest discover -s tests -p test_all.py -v
      return
      ;;
    logs)
      (( $# )) || usage_error 'logs requires one project; e.g. logs kafka kafka1 --follow'
      project=$(canonical_project "$1") || usage_error "Unknown log project: $1"
      shift
      run_lab "$project" logs "$@"
      return
      ;;
    start) action=up ;;
    stop) action=down ;;
    ps) action=status ;;
    check) action=doctor ;;
    offline-tests) action=test ;;
    init|doctor|up|status|down|restart|reset|test|preflight|health) ;;
    *) usage_error "Unknown command: $action (use ./all.sh help)" ;;
  esac
  for item in "$@"; do
    case "$item" in
      --json) JSON_OUTPUT=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --fail-fast) FAIL_FAST=1 ;;
      --yes|-y) yes=1 ;;
      -h|--help) usage; return ;;
      -*) usage_error "Unknown common option: $item; use PROJECT COMMAND for native options." ;;
      *) targets+=("$item") ;;
    esac
  done
  if [[ "$action" == reset ]]; then
    (( yes )) || usage_error 'Data deletion requires: ./all.sh reset PROJECT... --yes'
    (( ${#targets[@]} )) || usage_error "reset requires explicit project names or 'all'."
  elif (( yes )); then
    usage_error '--yes is only valid for reset (or k8s down).'
  fi
  select_projects "${targets[@]}"
  if [[ "$action" == health || "$action" == preflight ]]; then
    local opts=()
    (( ! JSON_OUTPUT )) || opts+=(--json)
    run_at "$ROOT" python3 "$ROOT/scripts/control.py" "$action" "${SELECTED[@]}" "${opts[@]}"
  else
    (( ! JSON_OUTPUT )) || usage_error '--json is valid only for health/preflight.'
    run_batch "$action"
  fi
}

main "$@"
