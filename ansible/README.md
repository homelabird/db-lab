> **v10:** inventory의 request/target/동의값과 수집 위치를 play 기본값으로 덮어쓰던 구조를 제거했습니다.
> 기존 inventory에 변경 동의가 저장돼 있다면 적용 전 `--check`와 `--limit`로 실제 계획을 확인하세요.
> [현재 품질 가이드](../docs/QUALITY-GUIDE.md)의 검증 범위와 재빌드 조건도 함께 확인합니다.

# DB Lab — Ansible 제어

기준: simulation v8 · 추가 버전: v9-ansible · 작성: 2026-09-19

기존 `all.sh`를 Ansible에서 호출합니다. 프로젝트별 기동·삭제·엔진 pin·장애 marker·복구 정책은 원래 제어기에 남습니다. DB와 Compose/Helm 서비스 코드를 새 Ansible 구현으로 대체하지 않습니다.

**검증 범위:** `scripts/test-ansible.sh`는 실제 Ansible의 로컬 모듈 전송·playbook 처리를 무해한 CLI fixture로 확인합니다. `scripts/test-ansible-ssh.sh`는 일회용 SSH key/host key와 loopback 전용 임시 `sshd`를 만들고 SSH 인증, 모듈 전송, 원격 fixture 실행을 검사합니다. 둘 다 DB를 시작하지 않으며, loopback SSH 통과는 별도 원격 Linux 호스트 배포 증거가 아닙니다. 상세 범위는 `docs/ANSIBLE-IMPLEMENTATION.md`를 참고하세요.

`scripts/test-ansible-bootstrap-container.sh`는 격리된 Debian 12 SSH 컨테이너에서 비루트 `labops` 접속, task-scoped sudo become, 실제 apt bootstrap/idempotence, source-only 배포/재사용, Redis `init/doctor/up/health/status/down`을 검사합니다. `down` 뒤에도 `.env`와 named volume이 남는지 확인합니다. rootful nested Podman은 임시 Docker 컨테이너 안에서만 사용하며 중첩 cgroup 제한 때문에 자식 cgroup을 비활성화하므로, 이 acceptance는 리소스 제한 적용을 검증하지 않습니다. rootless Podman에서 filesystem이 `nosuid`이면 sudo become이 불가능할 수 있으므로 rootful Docker를 선택하세요. Docker/Podman, Ansible, SSH 도구와 registry/OS 저장소 네트워크가 필요합니다.

## 1. 파일과 구조

```text
제어 PC (Ansible)
    └─ local 또는 SSH
        └─ 실습 서버의 db_lab_control 모듈
            └─ 그 서버의 기존 all.sh
                └─ 기존 각 DB 실습 / MVP / Helm 제어기
```

`control.yml`은 제어, `collect.yml`은 공유용 결과 수집입니다. `library/`와 `module_utils/`는 이 프로젝트 전용 모듈이며 원격 서버에 상시 agent를 설치하지 않습니다. 추가 Galaxy collection은 필요하지 않습니다.

일반 `control.yml` 사용 전에는 실습 서버에 프로젝트가 준비되어 있어야 합니다. 선택형 `deploy.yml`은 Git worktree의 source-only archive를 계정 홈 바로 아래 전용 경로의 새 content-addressed release에 배치합니다. 둘 다 OS 패키지/Docker 설치·SSH 키 배포·비밀번호 회전·firewall 변경은 하지 않습니다. Ansible 실행 권한이 있는 사람이 임의 playbook을 작성하는 것까지 막는 보안 경계는 아닙니다.

## 2. 준비

제어기는 Linux/WSL2/macOS의 Python 3.12~3.14, 관리 대상은 **Linux + Python 3.10~3.14 + Bash4.4+ + flock**를 대상으로 합니다. 이 프로젝트의 추가 제한으로 native Windows 관리 대상은 지원하지 않습니다. 공식 ansible-core2.21의 Python 지원 범위는 제어기3.12~3.14/대상3.9~3.14이지만 DB Lab 자체가 Python3.10+를 사용합니다.[S1]

제어 PC에서 프로젝트 루트 기준:

```bash
python3 -m venv .venv-ansible
. .venv-ansible/bin/activate
python -m pip install -r ansible/requirements.txt
cd ansible
ansible-playbook --version
```

`ansible-core==2.21.4`는 기준일 공식 PyPI에 공개된 안정 버전으로 지정했습니다.[S2] 이 환경에서 설치·실행을 완료했다는 뜻은 아닙니다. 전이 의존성 전체 hash 잠금은 하지 않았습니다.

`cd ansible` 이후 아래 명령을 실행합니다. 프로젝트 경로에 symlink가 있으면 거부하므로 inventory에는 정규화된 실제 절대 경로를 지정하세요.

### localhost

```bash
# 설정 기본값: localhost + 상위 프로젝트 경로 + mvp status
ansible-playbook control.yml --check

# 실행할 명령 확인: 동의값이 없어도 계획은 볼 수 있음
ansible-playbook control.yml --check -e @examples/mvp-up.yml

# 기존 env가 있으면 보존; 없으면 원래 init으로 임의 암호 생성
ansible-playbook control.yml \
  -e '{"db_lab_request":{"action":"init"},"db_lab_allow_changes":true}'

ansible-playbook control.yml -e '{"db_lab_request":{"action":"doctor"}}'
ansible-playbook control.yml -e @examples/mvp-up.yml
ansible-playbook control.yml -e @examples/verify-core.yml
```

`--check`는 입력/경로 검사와 argv 계획만 수행합니다. **all.sh, .env 생성, DB 조회, fault 주입, report 작성은 수행하지 않습니다.** Ansible 자신의 접속·임시 전송 파일까지 없는 것은 아닙니다. 기동 성공을 예측하거나 readiness를 인증하는 모드도 아닙니다.[S3]

### SSH 원격 실습 서버

```bash
cp inventory/remote.example.yml inventory/hosts.yml
# hosts.yml의 예시 주소/사용자/프로젝트 경로를 실제 전용 실습 서버로 수정

ansible-playbook -i inventory/hosts.yml control.yml --limit lab1 --check \
  -e @examples/mvp-up.yml

ansible-playbook -i inventory/hosts.yml control.yml --limit lab1 \
  -e @examples/mvp-up.yml
```

**SSH로 서버에 들어간 뒤 해당 서버의 로컬 Docker를 제어**합니다. 기존 MVP의 원격 `DOCKER_HOST` 금지·엔진 pin을 우회하지 않습니다. 브라우저 API를 공개하지 않고 SSH 터널을 그대로 사용할 수 있습니다.

기존 프로젝트를 운영하던 **동일 사용자·동일 엔진 설정**으로 실행하세요. `become`은 거부합니다. 호스트 키 검사를 끄거나 Docker socket 권한을 느슨하게 바꾸지 않습니다. Ansible의 비대화형 SSH는 `.bashrc`의 alias/가상환경 활성화를 자동 보장하지 않으므로 대상의 `python3`, `docker`, `podman-compose` 등 필요한 명령이 해당 사용자 PATH에 있어야 합니다.

주소 예시는 문서용이고 자동 연결하지 않습니다. 실제 서버의 SSH 접근 권한, 디스크·메모리 여유, 컨테이너 런타임은 별도 준비 조건입니다.

### 선택형 소스 artifact 배포

`deploy.yml`은 기존 프로젝트 경로를 수정하지 않고 새 release를 만듭니다. 목적지는 관리 계정 홈 바로 아래의 새 디렉터리여야 하며, 배포 후 `.env`를 복사하지 않습니다. 비밀값·데이터를 포함하지 않은 `.env.example`에서 `init`으로 대상 설정을 생성하세요. 런타임/DB 설치나 `up`은 자동 실행하지 않습니다.

```bash
ansible-playbook -i inventory/hosts.yml deploy.yml --limit lab1 \
  -e '{"db_lab_source_root":"/home/me/Documents/db-lab","db_lab_deploy_root":"/home/lab/db-lab-deploy","db_lab_allow_changes":true}'
```

출력된 release 경로는 inventory를 편집하지 않고 `db_lab_project_root` extra var로 선택할 수 있습니다. 예를 들어 해당 release에서 Kafka benchmark를 실행하려면 `<RELEASE_ID>`를 출력된 ID로 바꿉니다.

```bash
ansible-playbook -i inventory/hosts.yml control.yml --limit lab1 \
  -e '{"db_lab_project_root":"/home/lab/db-lab-deploy/releases/<RELEASE_ID>","db_lab_target":"kafka","db_lab_request":{"action":"benchmark","options":{"seconds":30,"rate":1000}},"db_lab_allow_changes":true}'
```

artifact는 Git worktree에서 추적되거나 Git이 무시하지 않은 파일 중 `.env`, key/cert, reports, state, data, volume, backup 계열 경로를 제외해 만듭니다. 제외 목록은 [package helper](package_artifact.py)에서 확인하세요. archive는 결정적 SHA-256 주소를 가지며, 원격 전송 후 SHA-256을 확인하고 새 임시 경로에서 추출해 원자적으로 게시합니다. 같은 ID와 marker가 있으면 재사용하고 덮어쓰지 않습니다. 중간 실패 시 원격 `.incoming-*` 디렉터리는 진단을 위해 남을 수 있으므로 내용을 확인한 뒤 해당 경로만 수동 정리하세요.

`--check`는 대상 파일을 바꾸지 않지만 SSH 접속과 facts 조회를 합니다. 서로 다른 release가 같은 Compose project/container identity를 사용할 수 있으므로 버전 전환 전 해당 lab의 `.env`, project name, volume 연결을 검토하세요. 데이터 복사나 자동 rollback은 하지 않습니다.

### 선택형 OS prerequisite 설치

`bootstrap.yml`은 **Ubuntu 24.04의 Docker/Podman** 또는 **Debian 12의 Podman**을 대상으로 합니다. `python3`가 이미 있어야 Ansible facts와 모듈을 실행할 수 있습니다. Debian 12의 기본 `podman-compose 1.0.3`은 DB Lab Compose 설정을 처리하지 못하므로, Podman bootstrap 동의 시 Debian 공식 서명 저장소인 `bookworm-backports`를 추가하고 거기서 `podman-compose`를 설치합니다. 외부 제3자 저장소나 key는 추가하지 않습니다.

```bash
# package plan만 확인: check mode에서는 apt/become task가 실행되지 않음
ansible-playbook -i inventory/hosts.yml bootstrap.yml --limit lab1 --check \
  -e '{"db_lab_engine":"podman"}'

# 실제 설치: apt task에서만 become을 요청하며 변경 확인 문구가 필수
ansible-playbook -i inventory/hosts.yml bootstrap.yml --limit lab1 --ask-become-pass \
  -e '{"db_lab_engine":"podman","db_lab_allow_changes":true,"db_lab_bootstrap_confirmation":"INSTALL:podman:bookworm-backports"}'
```

전역 `ansible_become` inventory/CLI override는 사용하지 마세요. 승인 문자열은 Ubuntu에서 `INSTALL:docker` 또는 `INSTALL:podman`, Debian 12에서 `INSTALL:podman:bookworm-backports`입니다. play는 한 호스트만 허용하고 패키지 제거, 전체 업그레이드, Docker group 추가, firewall rule, 제3자 apt 저장소 설정은 하지 않습니다. Debian backports source는 위 승인으로만 추가하고 기존 backports priority에 따라 Compose 패키지만 가져옵니다. Docker 배포판 패키지는 설치 과정에서 daemon을 시작할 수 있습니다. 패키지 설치 뒤에도 lab별 `doctor`와 `up/health` acceptance가 필요합니다. 패키지 정보: [Ubuntu Docker Compose v2](https://packages.ubuntu.com/noble/docker-compose-v2), [Ubuntu Podman Compose](https://packages.ubuntu.com/noble/podman-compose), [Debian 12 Podman Compose](https://packages.debian.org/bookworm/podman-compose), [Debian 12 Backports Podman Compose](https://packages.debian.org/bookworm-backports/podman-compose).

## 3. DB 프로젝트와 Helm 제어

`db_lab_target`은 `mvp|all|elasticsearch|elasticsearch9|kafka|mariadb|redis|k8s`입니다. `elasticsearch9`는 opt-in이며 기본 `all`에 포함되지 않습니다. 여기서 `all`은 기존 네 개 기본 실습만 뜻하며, ES9/MVP/Helm까지 자동 포함하지 않습니다.

```bash
# Kafka만 시작: 공통 up 경로의 init/preflight를 유지
ansible-playbook control.yml \
  -e '{"db_lab_target":"kafka","db_lab_request":{"action":"up"},"db_lab_allow_changes":true}'

# ES9는 기본 all에 섞이지 않으며 명시적으로 선택
ansible-playbook control.yml \
  -e '{"db_lab_target":"elasticsearch9","db_lab_request":{"action":"health"}}'

# 기존 네 실습의 건강 상태 판정; 단순 status와 다름
ansible-playbook control.yml \
  -e '{"db_lab_target":"all","db_lab_request":{"action":"health"}}'

# 데이터 볼륨을 남기고 Redis 실습 종료
ansible-playbook control.yml \
  -e '{"db_lab_target":"redis","db_lab_request":{"action":"down"},"db_lab_allow_changes":true}'
```

기존 네 독립 Compose 랩과 ES9에서 지원하는 action: `help`, `list`, `init`, `doctor`, `preflight`, `health`, `status`, `up`, `down`, `restart`, `test`, `reset`, `benchmark`. `benchmark`는 `elasticsearch|elasticsearch9|kafka|mariadb|redis` 중 하나만 허용하고 root의 공통 반복/보고서 도구에 연결합니다. 부하 실행은 데이터를 쓰므로 `db_lab_allow_changes: true`가 필요하며 `--check`는 계획만 출력합니다. `all`, MVP, Helm 대상으로 benchmark를 실행하지 않습니다. MVP는 별도 제한 action 집합을 사용합니다. 프로젝트별 모든 원시 CLI 명령을 무제한 전달하는 인터페이스는 넣지 않았습니다.

Benchmark 옵션은 DB별 bounded runner 계약으로 제한됩니다: `seconds` 5–600; ES의 `rate`/`batch`, Kafka의 `rate`/`payload`, MariaDB의 `workers`, Redis의 `rate`/`workers`/`keys`/`payload`만 허용합니다. 각 범위는 공통 runner와 같습니다. 예시:

```bash
ansible-playbook control.yml -i inventory/hosts.yml --limit lab1 \
  -e @examples/benchmark-kafka.yml
```

비교용 run은 같은 DB, 같은 옵션, 같은 데이터 조건으로 각각 실행하고, 생성된 report 경로들을 root `scripts/benchmarks.py`에 전달하세요. Ansible 실행 성공은 벤치마크 PASS나 성능 비교 준비 완료를 뜻하지 않습니다.

Helm 예시는 `examples/k8s-status.yml`입니다. inventory가 아니라 `-e @파일`로 `db_lab_k8s`의 context·allowed_contexts·namespace·release를 명시하세요. kubeconfig와 values 파일은 **원격 실습 서버의 절대 경로**입니다. 자동 Secret 값 전달이나 임의 `--set/--force/--post-renderer`는 지원하지 않습니다.

```bash
ansible-playbook control.yml -e @examples/k8s-status.yml
# up을 실행하려면 위 파일의 action: up, db_lab_allow_changes: true를 명시
```

## 4. MVP 실습·비교·복구

```bash
ansible-playbook control.yml -e @examples/study-kafka.yml
ansible-playbook control.yml -e @examples/transaction-stock.yml
ansible-playbook control.yml -e @examples/messages-poison.yml
```

개별 요청은 YAML 변수로 표현합니다.

```yaml
db_lab_target: mvp
db_lab_request:
  action: simulate       # simulate, drills, messages, verify, study
  verb: run              # plan/list/run 등 각 원래 하위 명령
  name: kafka-outage
  options:
    seed: 42
    workload: write-heavy
    seconds: 60
    rate: 3
    workers: 4
    fault_at: 15
    fault_for: 15
    recovery_timeout: 120
db_lab_allow_changes: true
db_lab_allow_faults: true
```

`rate`는 workflow/초이며 DB TPS가 아닙니다. 기존 제한을 유지하고, 임의 명령·옵션 문자열·무제한 로그 follow를 거부합니다. plan은 DB 실기동 가능성을 증명하지 않습니다.

| action | verb/name/options |
|---|---|
| `simulate` | `list`, `plan/run + 기존 13개 시나리오`, `recover` |
| `drills` | `list`, `plan/run + 기존 11개 시나리오`, `prepare`, `recover` |
| `messages` | `list`, `plan/run + 기존 6개 시나리오`, `inspect/cleanup + msg-ID`, `recover` |
| `verify` | `list`, `plan/run + core 등 기존 9개 범위`, `report/export + accept-ID` |
| `study` | `list`, `plan/run + 기존 9개 짝 비교`, `compare` |

MVP 일반 action은 `help/init/up/down/restart/status/doctor/diagnose/inspect-runtime/smoke/test/bind-target/rebuild-search`입니다. `stop/resume/recreate/logs`는 service를 `name`으로 지정합니다. `experiment`는 `normal/redis-spare/bad-db-password/fresh-search`, `sql`은 `orders/outbox/counts/ping`만 허용합니다. MVP reset은 제공하지 않습니다.

`study compare`의 options에는 `baseline_id`, `fault_id`를 사용합니다. `drills run upgrade-restore`/`verify candidate`는 `options.candidate_image`에 이미 선택·pull한 공식 MariaDB 숫자 버전을 요구합니다. `snapshot`은 원격 절대 경로이며 기존 빈 clone 복원 정책을 유지합니다.

### 복구

원인과 marker를 확인한 뒤 종류에 맞게 명시적으로 실행합니다.

```bash
ansible-playbook control.yml \
  -e '{"db_lab_request":{"action":"simulate","verb":"recover"},"db_lab_allow_changes":true}'
```

`drills` 또는 `messages`도 같은 형식입니다. `experiment normal`은 설정 복원, `resume`은 해당 서비스 시작 요청입니다. 이들을 모두 자동 호출해 실패 근거를 지우지 않습니다.

## 5. 안전·동시성·실패

관찰/계획은 별도 동의 없이 가능하지만 원래 CLI가 `.state`/receipt 등 로컬 파일을 기록할 수 있습니다. 엄밀한 무실행은 `--check`를 사용합니다.

| 작업 | 요구 |
|---|---|
| 기동/종료/재시작/init/smoke/core 등 | `db_lab_allow_changes: true` |
| 장애·비교·고급 실습 | 위 값 + `db_lab_allow_faults: true` |
| 기존 DB reset | 위 변경 동의 + `db_lab_allow_destroy: true` + `db_lab_confirmation: DELETE:대상` |
| Helm uninstall | 변경/삭제 동의 + `UNINSTALL:namespace/release` |
| 최초 엔진 bind | 변경 동의 + `BIND:mvp` |

reset 예시는 자동 실행 파일로 제공하지 않습니다. 승인된 reset도 원래 DB 데이터를 실제로 삭제하므로 별도 백업·대상 확인이 필요합니다. bind는 기존 pin을 덮어쓰는 명령이 아닙니다.

play는 `serial: 1`, `any_errors_fatal: true`이고 **변경 작업은 --limit로 한 호스트를 선택해야** 합니다. 진단은 여러 호스트를 순차 처리할 수 있지만 첫 실패/접속 실패에서 중단합니다.[S4] 다중 서버 분산 HA/롤링 업그레이드 구현은 아닙니다.

추가 lock은 같은 원격 프로젝트 경로의 Ansible 호출 사이에서만 동작합니다. 기존 all.sh/하위 lock도 그대로 적용되지만, 다른 checkout·직접 Docker 명령·외부 사용자의 변경까지 전역으로 막지 못합니다.

명령은 셸 문자열이 아닌 argv로 **한 번만** 실행됩니다. 반복 재시도, `ignore_errors`, `poll:0`, 무조건적 rescue/rollback은 넣지 않았습니다. rc2(inconclusive), rc127(blocked), rc130(interrupted), 기타 오류를 성공으로 바꾸지 않습니다. 모듈 결과의 rc와 Ansible 자체 exit code는 서로 다를 수 있습니다.

기본 제한시간은 **각 native 명령 3600초**이며 `db_lab_timeout`으로 30~43200초를 지정합니다. `restart`는 down/up 두 명령 각각의 예산입니다. `verify all`은 시나리오가 많으므로 계획을 확인하고 충분한 예산을 명시해야 합니다. timeout 시 TERM→15초 유예→KILL을 시도하며 유예 중 rc0으로 끝나도 timeout 실패입니다.

Ansible/SSH 자체가 강제 종료되거나 호스트가 꺼지면 자동 복구를 보장하지 않습니다. marker를 삭제하지 않고 기존 recover로 상태를 확인하세요. 외부 watchdog이나 백그라운드 예약 작업은 설치하지 않습니다.

### Ansible changed의 의미

`init`은 실행 전후 `.env` 내용 hash가 같으면 `changed: false`입니다. 관찰/계획은 서비스 변경을 하지 않았다는 의미로 false지만 로컬 로그는 생깁니다. `up/down/restart`와 실습은 **명령을 실행했다는 의미**로 true이며, 반복 실행 때 이미 같다고 컨테이너를 자동 생략하는 선언적 수렴 모듈은 아닙니다. 실패 중에도 일부 작업이 적용됐을 수 있으므로 true가 반환될 수 있습니다. `changed: true`나 rc0만으로 DB 정상/정합성/HA를 인증하지 않습니다.

## 6. 원시 출력과 공유용 보고서 수집

기본 출력에는 실행 계획·rc·시간·로그 경로만 표시하고 native stdout/stderr를 숨깁니다. 원시 출력은 원격 프로젝트의 다음 위치에 제한 크기로 남깁니다.

```text
reports/ansible/control-<ID>/
    plan.json
    result.json
    00.stdout.log
    00.stderr.log
```

디렉터리700/파일600입니다. 스트림당2MiB까지만 저장하고 초과 출력은 배출 후 버립니다. truncated 여부/실제 수신 바이트는 결과에 표시합니다. 오래된 Ansible 로그의 자동 만료·삭제는 없습니다. 계정 관리자와 root는 이 파일을 볼 수 있습니다.

원시 출력이 꼭 필요하면 `db_lab_show_output: true`를 명시하지만 **로그·합성 주문·자격 증명이 노출될 가능성이 있어 CI에는 사용하지 마세요.** 이 값은 범용 비밀정보 제거 기능이 아닙니다.

기존 verify의 실제 accept-ID를 사용하여 공유용 결과 세 파일만 제어 PC로 가져옵니다.

```bash
ansible-playbook -i inventory/hosts.yml collect.yml --limit lab1 \
  -e db_lab_acceptance_id=accept-0123456789ab
```

위 ID는 형식 예시이며 실제 ID로 바꿔야 합니다. 원격 `mvp verify export ID`가 생성한 **summary.json/report.md/junit.xml**만 `artifacts/lab1/accept-ID/`에 수집합니다. 원시 로그·.env·주문 원문·복구 marker·전체 reports를 재귀 복사하지 않습니다. 경로·파일 크기·symlink를 검사하고 Ansible fetch의 checksum 확인도 사용합니다.[S5] candidate export는 기존 정책대로 거부됩니다. export가 성공해도 원래 결과의 failed/blocked가 passed로 바뀌지 않습니다.

## 7. 검증 명령

```bash
# 프로젝트 루트에서: 기존 전체 + 신규 Ansible 정책/프로세스 tests
bash scripts/test-offline.sh

# 실제 ansible-playbook 필요. 무해한 fixture의 로컬 모듈 전송/동의/check 검증
bash scripts/test-ansible.sh

# openssh-server 필요. 임시 loopback sshd에서 실제 SSH + 모듈 전송 검증
bash scripts/test-ansible-ssh.sh

# disposable Debian 12 SSH + 실제 apt/become + 멱등성. rootless Podman 제한 시 Docker 선택
DB_LAB_BOOTSTRAP_TEST_RUNTIME=docker bash scripts/test-ansible-bootstrap-container.sh

# ansible/에서 실제 설치 후 구문 검사
ansible-playbook control.yml --syntax-check
ansible-playbook collect.yml --syntax-check
```

`test-ansible.sh`는 **실제 Ansible + 로컬 fixture**, `test-ansible-ssh.sh`는 **실제 SSH 인증/전송 + 로컬 fixture** 시험입니다. loopback 검증은 별도 원격 호스트의 OS 권한·런타임·DB 준비 상태를 증명하지 않습니다. 전용 원격 실습 서버에서 `--check` → init → doctor → up → verify core → 수집 순으로 별도 인수해야 합니다. 이미 운용 중인 프로젝트에서는 기존 env/state/volumes와 엔진 pin을 보존합니다.

## 공식 계약 참고

[S1] https://docs.ansible.com/ansible/latest/reference_appendices/release_and_maintenance.html

[S2] https://pypi.org/project/ansible-core/2.21.4/

[S3] https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_checkmode.html

[S4] https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_error_handling.html

[S5] https://docs.ansible.com/ansible/latest/collections/ansible/builtin/fetch_module.html

[S6] https://docs.ansible.com/ansible/latest/dev_guide/developing_modules_general.html

공식 문서 확인일 2026-09-19. 문서 확인은 이 프로젝트의 실제 Ansible/SSH/DB 실행 증거가 아닙니다.
