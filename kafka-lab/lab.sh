#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1

say() { printf '\n== %s ==\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 명령이 필요합니다. README.md의 설치 절차를 확인하세요."; }
help_text() {
cat <<'HELP'
ZooKeeper 3 + Kafka 3 / Podman Compose 실습
  ./lab.sh doctor                       사전 확인
  ./lab.sh config                       실제 Compose 설정 출력
  ./lab.sh up                           빌드 → ZK quorum → broker → 토픽 생성
  ./lab.sh health                       ZK 역할 + 3 broker + full ISR 확인
  ./lab.sh smoke                        새 토픽에 120건 전송/읽기/중복·누락 검증
  ./lab.sh status                       컨테이너 + 클러스터 상태(JSON)
  ./lab.sh topics                       leader / replica / ISR (기본 Kafka CLI)
  ./lab.sh seed [--kind payments|access|metrics] [--count N | --mib N | --duration S]
                [--payload-bytes N] [--rate N] [--hot-key] [--topic lab.NAME]
  ./lab.sh read lab.payments [--max 10]  읽기만; group commit 없음
  ./lab.sh consume TOPIC GROUP [--duration 60] [--sleep-ms 100] [--count N]
  ./lab.sh lag TOPIC GROUP               committed offset / lag 확인
  ./lab.sh offsets TOPIC                 파티션별 log-start/log-end offset
  ./lab.sh zk                            ZooKeeper leader/follower 상태
  ./lab.sh zk-shell ls /brokers/ids      ZooKeeper znode 조회
  ./lab.sh cli <client.py 하위 명령>      Python client 직접 실행
  ./lab.sh kcli kafka-topics --list      Kafka 기본 CLI (bootstrap 자동 지정)
  ./lab.sh logs kafka1 [--follow]        해당 서비스의 최근 로그
  ./lab.sh ui up|down                    선택적 읽기 전용 Web UI

장애 유지: 관찰을 끝낸 뒤 반드시 recover
  ./lab.sh fault stop-broker 1           정상 종료(SIGTERM)
  ./lab.sh fault kill-broker 1           강제 종료(SIGKILL)
  ./lab.sh fault pause-broker 1          프로세스 정지(rootless 지원은 호스트 의존)
  ./lab.sh fault isolate-broker 1        lab 네트워크 전체 단절
  ./lab.sh fault stop-zk 1
  ./lab.sh fault zk-quorum               zk2 + zk3 종료
  ./lab.sh recover                       연결/정지 복원 → quorum/full ISR 확인

자동 데모: 종료·오류·Ctrl+C에서 노드 복구를 시도, reports/run-*/에 로그 저장
  ./lab.sh demo broker-failover|controller-failover|min-isr|zk-one|zk-quorum
  ./lab.sh demo lag|hot-key|oversize|retention

종료/삭제
  ./lab.sh down                          컨테이너/네트워크 제거, 데이터 볼륨 보존
  ./lab.sh reset --yes                   이 lab의 데이터까지 삭제(되돌릴 수 없음)
  ./lab.sh unlock --yes                  종료된 demo의 stale lock만 해제
  ./scripts/test-static.sh               호스트 단위/모의 테스트
  ./scripts/test-live.sh [--faults]       실제 Kafka 검증; --faults는 9개 데모 포함
HELP
}

cmd=${1:-help}
[[ $# -eq 0 ]] || shift
case "$cmd" in help|-h|--help) help_text; exit 0;; esac
[[ -f "$ROOT/.env" ]] || cp "$ROOT/.env.example" "$ROOT/.env"
# This lab's .env is a trusted shell-compatible KEY=value configuration file.
set -a
# shellcheck source=/dev/null
source "$ROOT/.env"
set +a
: "${LAB_NAME:=kzk-lab}" "${CP_VERSION:=7.9.0}" "${BIND_IP:=127.0.0.1}"
: "${ADVERTISED_HOST:=localhost}" "${STARTUP_TIMEOUT:=240}" "${UI_PORT:=8088}"
[[ "$LAB_NAME" =~ ^[a-z][a-z0-9-]{1,40}$ ]] || die 'LAB_NAME은 소문자/숫자/하이픈, 2..41자여야 합니다.'
[[ "$CP_VERSION" =~ ^7\.9\.[0-9]+$ ]] || die '이 Lab은 CP 7.9.x ZooKeeper 전용입니다. latest/8.x는 사용할 수 없습니다.'
[[ "$ADVERTISED_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ && "$ADVERTISED_HOST" != 0.0.0.0 ]] || die 'ADVERTISED_HOST에는 클라이언트가 접근할 실제 IPv4/호스트명을 넣으세요. CIDR/0.0.0.0 불가.'
[[ "$STARTUP_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die 'STARTUP_TIMEOUT must be a positive integer'
NETWORK="$LAB_NAME-net"
BS='kafka1:9092,kafka2:9092,kafka3:9092'
mkdir -p "$ROOT/.state" "$ROOT/reports"

pc() {
    (cd "$ROOT" && podman-compose --in-pod=false --env-file "$ROOT/.env" -p "$LAB_NAME" -f "$ROOT/compose.yaml" "$@")
}
pc_ui() {
    (cd "$ROOT" && podman-compose --in-pod=false --env-file "$ROOT/.env" -p "$LAB_NAME" -f "$ROOT/compose.yaml" -f "$ROOT/compose.ui.yaml" "$@")
}
valid_service() { [[ "$1" =~ ^(kafka[123]|zk[123]|tools|ui)$ ]] || die "허용되지 않은 서비스: $1"; }
valid_id() { [[ "$1" =~ ^[123]$ ]] || die '노드 번호는 1, 2, 3만 허용합니다.'; }
exists() { podman container exists "$LAB_NAME-$1"; }
owned() {
    local s=$1 label
    valid_service "$s"
    exists "$s" || { printf '없는 lab 컨테이너: %s\n' "$LAB_NAME-$s" >&2; return 1; }
    label=$(podman inspect --format '{{ index .Config.Labels "io.kzk.lab" }}' "$LAB_NAME-$s") || return 1
    [[ "$label" == "$LAB_NAME" ]] || { printf '다른 소유자의 컨테이너에는 작업하지 않습니다: %s\n' "$LAB_NAME-$s" >&2; return 1; }
}
client() {
    owned tools || return 1
    podman exec "$LAB_NAME-tools" python /opt/lab/client.py "$@"
}
no_active_demo() {
    [[ ! -d "$ROOT/.state/demo.lock" ]] || die 'demo lock이 있습니다. 다른 터미널의 demo 종료 후 작업하세요. 비정상 종료는 recover → unlock --yes.'
}
check_existing() {
    local s v label
    for s in zk1 zk2 zk3 kafka1 kafka2 kafka3 tools ui; do
        if exists "$s"; then owned "$s" || return 1; fi
    done
    for v in zk1-data zk1-log zk2-data zk2-log zk3-data zk3-log kafka1-data kafka2-data kafka3-data; do
        if podman volume exists "$LAB_NAME-$v"; then
            label=$(podman volume inspect --format '{{ index .Labels "io.kzk.lab" }}' "$LAB_NAME-$v") || return 1
            [[ "$label" == "$LAB_NAME" ]] || die "동일 이름의 다른 볼륨 발견: $LAB_NAME-$v"
        fi
    done
    if podman network exists "$NETWORK"; then
        label=$(podman network inspect --format '{{ index .Labels "io.kzk.lab" }}' "$NETWORK") || return 1
        [[ "$label" == "$LAB_NAME" ]] || die "동일 이름의 다른 네트워크 발견: $NETWORK"
    fi
}
doctor() {
    need podman; need podman-compose; need python3; need tee
    local usage
    usage=$(podman-compose --help) || return 1
    [[ "$usage" == *'--in-pod'* ]] || die '독립 컨테이너 네트워크가 필요합니다. --in-pod를 지원하는 podman-compose 1.x를 설치하세요.'
    podman --version
    podman-compose --version
    podman info --format 'rootless={{.Host.Security.Rootless}} network={{.Host.NetworkBackend}}'
    printf 'LAB_NAME=%s\nCP_VERSION=%s\nBIND_IP=%s ADVERTISED_HOST=%s\n' "$LAB_NAME" "$CP_VERSION" "$BIND_IP" "$ADVERTISED_HOST"
    printf '설계 권장: 4 vCPU / RAM 8GB / 여유 디스크 10GB 이상. 실제 사용량은 데이터와 호스트에 따라 다릅니다.\n'
    if [[ "$BIND_IP" == 0.0.0.0 ]]; then
        printf 'WARNING: 모든 IPv4 인터페이스에 인증 없는 Kafka/UI 포트를 바인딩합니다. 격리망과 방화벽을 사용하세요.\n' >&2
    fi
    pc config >/dev/null
}
health() {
    client zk-status --serving 3 --timeout "$STARTUP_TIMEOUT"
    client wait --brokers 3 --timeout "$STARTUP_TIMEOUT"
}
start_lab() {
    no_active_demo
    doctor
    check_existing
    say '클라이언트 이미지 빌드'
    pc build tools
    say 'ZooKeeper 3개 + 실습 도구 기동'
    pc up -d zk1 zk2 zk3 tools
    client zk-status --serving 3 --timeout "$STARTUP_TIMEOUT"
    say 'Kafka 3개 기동: ZooKeeper 모드'
    pc up -d kafka1 kafka2 kafka3
    client wait --brokers 3 --timeout "$STARTUP_TIMEOUT"
    client init
    health
    say '기동 완료. ./lab.sh smoke → ./lab.sh seed → ./lab.sh read lab.payments'
}
pick_broker() {
    local s running paused attached
    for s in kafka1 kafka2 kafka3; do
        exists "$s" || continue
        owned "$s" || return 1
        running=$(podman inspect --format '{{.State.Running}}' "$LAB_NAME-$s") || return 1
        paused=$(podman inspect --format '{{.State.Paused}}' "$LAB_NAME-$s") || return 1
        attached=$(podman inspect --format "{{if index .NetworkSettings.Networks \"$NETWORK\"}}yes{{end}}" "$LAB_NAME-$s") || return 1
        if [[ "$running" == true && "$paused" == false && "$attached" == yes ]]; then
            printf '%s\n' "$LAB_NAME-$s"; return 0
        fi
    done
    printf 'CLI를 실행할 연결된 broker가 없습니다. recover를 먼저 실행하세요.\n' >&2
    return 1
}
kcli() {
    local binary=${1:-} broker
    [[ $# -gt 0 ]] || die 'kcli 명령을 지정하세요.'
    shift
    case "$binary" in kafka-topics|kafka-configs|kafka-consumer-groups|kafka-console-producer|kafka-console-consumer|kafka-log-dirs|kafka-reassign-partitions|kafka-leader-election|kafka-get-offsets|kafka-broker-api-versions) ;;
      *) die '지원하는 Kafka CLI 이름은 README/HELP를 확인하세요.';; esac
    broker=$(pick_broker) || return 1
    podman exec -i "$broker" "$binary" --bootstrap-server "$BS" "$@"
}
node_stop() {
    owned "$1" || return 1
    podman stop --time 20 "$LAB_NAME-$1"
}
fault() {
    local kind=${1:-} id=${2:-}
    case "$kind" in
      stop-broker|kill-broker|pause-broker|isolate-broker)
        valid_id "$id"; owned "kafka$id" || return 1
        case "$kind" in
          stop-broker) node_stop "kafka$id";;
          kill-broker) podman kill --signal KILL "$LAB_NAME-kafka$id";;
          pause-broker) podman pause "$LAB_NAME-kafka$id";;
          isolate-broker) podman network disconnect "$NETWORK" "$LAB_NAME-kafka$id";;
        esac;;
      stop-zk) valid_id "$id"; node_stop "zk$id";;
      zk-quorum) node_stop zk2; node_stop zk3;;
      *) die 'fault 종류: stop-broker, kill-broker, pause-broker, isolate-broker, stop-zk, zk-quorum';;
    esac
    say '장애 상태 유지 중. status / topics / zk / logs로 관찰한 뒤 ./lab.sh recover'
}
restore_one() {
    local s=$1 paused running attached
    owned "$s" || return 1
    paused=$(podman inspect --format '{{.State.Paused}}' "$LAB_NAME-$s") || return 1
    if [[ "$paused" == true ]]; then podman unpause "$LAB_NAME-$s" || return 1; fi
    attached=$(podman inspect --format "{{if index .NetworkSettings.Networks \"$NETWORK\"}}yes{{end}}" "$LAB_NAME-$s") || return 1
    if [[ "$attached" != yes ]]; then
        podman network connect --alias "$s" "$NETWORK" "$LAB_NAME-$s" || return 1
    fi
    running=$(podman inspect --format '{{.State.Running}}' "$LAB_NAME-$s") || return 1
    if [[ "$running" != true ]]; then podman start "$LAB_NAME-$s" || return 1; fi
}
recover() {
    local s
    say '네트워크 별칭/paused/stopped 상태 복원'
    for s in tools zk1 zk2 zk3; do restore_one "$s" || return 1; done
    client zk-status --serving 3 --timeout "$STARTUP_TIMEOUT" || return 1
    for s in kafka1 kafka2 kafka3; do restore_one "$s" || return 1; done
    client wait --brokers 3 --timeout "$STARTUP_TIMEOUT" || return 1
    say '복구 확인: ZooKeeper quorum + 3 brokers + full ISR'
}
demo_exit() {
    local rc=$? recovered=0
    trap - EXIT INT TERM
    set +e
    recover
    recovered=$?
    if [[ $recovered -ne 0 ]]; then
        printf 'AUTO-RECOVERY FAILED. 로그를 확인하고 ./lab.sh recover를 실행하세요.\n' >&2
        rc=1
    fi
    rm -f "$ROOT/.state/demo.lock/pid"
    rmdir "$ROOT/.state/demo.lock" 2>/dev/null
    if [[ $rc -eq 0 ]]; then
        say "DEMO PASS (실제 실행 및 복구 완료): $DEMO_NAME"
    else
        say "DEMO FAIL/INTERRUPTED: $DEMO_NAME (exit=$rc)"
    fi
    printf '%s\n' "$rc" > "$DEMO_DIR/exit-code.txt"
    exit "$rc"
}
demo() {
    DEMO_NAME=${1:-}
    case "$DEMO_NAME" in broker-failover|controller-failover|min-isr|zk-one|zk-quorum|lag|hot-key|oversize|retention) ;;
      *) die '알 수 없는 demo 이름입니다. ./lab.sh help를 확인하세요.';; esac
    no_active_demo
    health
    mkdir "$ROOT/.state/demo.lock" || die '다른 demo가 시작되었습니다.'
    printf '%s\n' "$$" > "$ROOT/.state/demo.lock/pid"
    DEMO_DIR="$ROOT/reports/run-$(date -u +%Y%m%dT%H%M%SZ)-$DEMO_NAME-$$"
    mkdir -p "$DEMO_DIR"
    trap demo_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    exec > >(tee "$DEMO_DIR/output.log") 2>&1
    local topic="lab.demo.$DEMO_NAME.$(date -u +%Y%m%d%H%M%S).$$" id group
    say "데모: $DEMO_NAME / 전용 토픽: $topic"
    case "$DEMO_NAME" in
      broker-failover)
        client create --topic "$topic" --partitions 1 --assignment 1,2,3
        client wait --topic "$topic" --isr 3
        client seed --topic "$topic" --count 100 --rate 0
        id=$(client leader --topic "$topic")
        valid_id "$id"
        say "현재 partition leader인 kafka$id 강제 종료"
        fault kill-broker "$id"
        client wait --topic "$topic" --brokers 2 --isr 2 --leader-not "$id"
        client seed --topic "$topic" --count 100 --rate 0
        client read --topic "$topic" --max 3
        ;;
      controller-failover)
        client create --topic "$topic" --partitions 3
        client wait --topic "$topic" --isr 3
        id=$(client controller)
        valid_id "$id"
        say "현재 Kafka controller인 kafka$id 강제 종료"
        fault kill-broker "$id"
        client wait --topic "$topic" --brokers 2 --isr 2 --controller-not "$id"
        client seed --topic "$topic" --count 100 --rate 0
        ;;
      min-isr)
        client create --topic "$topic" --partitions 1 --assignment 1,2,3
        client wait --topic "$topic" --isr 3
        [[ $(client leader --topic "$topic") == 1 ]] || die '사전 조건 실패: leader가 1이 아닙니다.'
        client seed --topic "$topic" --count 10 --rate 0
        fault stop-broker 2
        client wait --topic "$topic" --brokers 2 --isr 2
        client seed --topic "$topic" --count 10 --rate 0
        fault stop-broker 3
        client wait --topic "$topic" --brokers 1 --isr 1
        say 'leader는 살아 있지만 min.insync.replicas=2 미달 → acks=all 거절 확인'
        client probe-failure --topic "$topic" --kind isr
        ;;
      zk-one)
        fault stop-zk 2
        client zk-status --serving 2 --timeout 90
        client create --topic "$topic" --partitions 3
        client seed --topic "$topic" --count 100 --rate 0
        ;;
      zk-quorum)
        client create --topic "$topic" --partitions 3
        client seed --topic "$topic" --count 100 --rate 0
        fault zk-quorum
        client zk-status --serving 0 --timeout 90
        say '기존 토픽 쓰기 관찰: 성공 또는 실패를 기록하며 특정 결과로 단정하지 않음'
        if client seed --topic "$topic" --count 10 --rate 0; then
            printf 'OBSERVED: 기존 데이터 경로는 이번 실행에서 동작했습니다.\n'
        else
            printf 'OBSERVED: 기존 데이터 경로의 쓰기가 이번 실행에서는 실패했습니다.\n'
        fi
        say '새 토픽 생성 관찰: quorum 복구 전에는 정상 관리를 기대할 수 없음'
        if client create --topic "$topic.new" --partitions 1 --timeout 5; then
            die 'quorum 상실 뒤 토픽 생성이 성공했습니다. 시나리오 전제와 로그를 점검하세요.'
        else
            printf 'OBSERVED: 생성 요청 실패. 상세 오류는 위 로그에 보존됩니다. 주된 검증은 srvr serving=0입니다.\n'
        fi
        ;;
      lag)
        client create --topic "$topic" --partitions 3
        client seed --topic "$topic" --count 5000 --rate 0 --payload-bytes 64
        group="lab-lag-$(date -u +%Y%m%d%H%M%S)-$$"
        say '느린 업무 처리(건당 25ms), 100건 처리 후 남은 backlog 관찰'
        client consume --topic "$topic" --group "$group" --sleep-ms 25 --count 100 --duration 60
        client offsets --topic "$topic" --group "$group"
        say '처리 지연 제거 → 같은 그룹의 남은 backlog 처리'
        client consume --topic "$topic" --group "$group" --duration 120 --idle-timeout 5
        client offsets --topic "$topic" --group "$group" --expect-lag 0
        ;;
      hot-key)
        client create --topic "$topic" --partitions 6
        client seed --topic "$topic" --count 3000 --rate 0 --hot-key --payload-bytes 64
        client offsets --topic "$topic" --expect-active 1
        say '6개 partition 중 한 곳만 쓰임: 같은 key를 반복하면 consumer만 늘려도 해결되지 않음'
        ;;
      oversize)
        client create --topic "$topic" --partitions 1 --config max.message.bytes=1024
        client wait --topic "$topic" --isr 3
        client probe-failure --topic "$topic" --kind oversize
        say '제한 아래 크기의 정상 메시지는 전송되는지 확인'
        client seed --topic "$topic" --count 1 --payload-bytes 0 --rate 0
        ;;
      retention)
        client create --topic "$topic" --partitions 3 \
            --config cleanup.policy=delete --config retention.ms=5000 \
            --config segment.ms=1000 --config segment.bytes=1048576 --config file.delete.delay.ms=1000
        client seed --topic "$topic" --count 6000 --payload-bytes 512 --rate 0
        client offsets --topic "$topic"
        say '닫힌 segment의 retention 삭제로 log-start-offset이 전진하는지 확인'
        client retention-wait --topic "$topic" --timeout 150
        ;;
    esac
    # EXIT trap validates restoration before printing DEMO PASS.
}

need podman
case "$cmd" in
  doctor) doctor;;
  config) need podman-compose; pc config;;
  up) need podman-compose; start_lab;;
  health) health;;
  smoke) health; client smoke;;
  status)
    podman ps -a --filter "label=io.kzk.lab=$LAB_NAME"
    client zk-status
    client status;;
  topics) kcli kafka-topics --describe "$@";;
  seed) client seed "$@";;
  read) [[ $# -ge 1 ]] || die 'read TOPIC [--max N]'; topic=$1; shift; client read --topic "$topic" "$@";;
  consume) [[ $# -ge 2 ]] || die 'consume TOPIC GROUP [options]'; topic=$1; group=$2; shift 2; client consume --topic "$topic" --group "$group" "$@";;
  lag) [[ $# -eq 2 ]] || die 'lag TOPIC GROUP'; client offsets --topic "$1" --group "$2";;
  offsets) [[ $# -eq 1 ]] || die 'offsets TOPIC'; client offsets --topic "$1";;
  zk) client zk-status "$@";;
  cli) client "$@";;
  kcli) kcli "$@";;
  zk-shell)
    broker=$(pick_broker)
    podman exec -i "$broker" zookeeper-shell 'zk1:2181,zk2:2181,zk3:2181' "$@";;
  logs)
    [[ $# -ge 1 ]] || die 'logs kafka1 [--follow]'; service=$1; shift; owned "$service"
    podman logs --tail 150 "$@" "$LAB_NAME-$service";;
  fault) no_active_demo; fault "$@";;
  recover) recover;;
  demo) demo "$@";;
  ui)
    need podman-compose; no_active_demo; check_existing
    case "${1:-}" in
      up) pc_ui up -d ui; printf 'UI: http://%s:%s (read-only)\n' "$ADVERTISED_HOST" "$UI_PORT";;
      down) if exists ui; then owned ui; podman rm -f "$LAB_NAME-ui"; fi;;
      *) die 'ui up|down';;
    esac;;
  down|reset)
    need podman-compose; no_active_demo; check_existing
    if [[ "$cmd" == reset ]]; then
        [[ "${1:-}" == --yes && $# -eq 1 ]] || die '데이터를 삭제하려면 ./lab.sh reset --yes (되돌릴 수 없음)'
    fi
    # Unpause/reconnect before removal. Do not require quorum just to shut down.
    for service in zk1 zk2 zk3 kafka1 kafka2 kafka3 tools ui; do
        if exists "$service"; then
            owned "$service"
            if [[ $(podman inspect --format '{{.State.Paused}}' "$LAB_NAME-$service") == true ]]; then podman unpause "$LAB_NAME-$service"; fi
        fi
    done
    if [[ "$cmd" == reset ]]; then pc_ui down -v; else pc_ui down; fi;;
  unlock)
    [[ "${1:-}" == --yes ]] || die 'unlock --yes'
    if [[ -d "$ROOT/.state/demo.lock" ]]; then
        pid=$(cat "$ROOT/.state/demo.lock/pid" 2>/dev/null || true)
        [[ "$pid" =~ ^[0-9]+$ ]] || die '유효한 lock pid가 없습니다. 다른 demo가 실행 중인지 수동 확인하세요.'
        if kill -0 "$pid" 2>/dev/null; then die "PID $pid 실행 중: lock 해제 거부"; fi
        rm -f "$ROOT/.state/demo.lock/pid"; rmdir "$ROOT/.state/demo.lock"
    fi;;
  *) die "알 수 없는 명령: $cmd (./lab.sh help)";;
esac
