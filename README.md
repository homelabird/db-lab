# DB Lab — v12 MVP 시작·진단 후속 수정

[시나리오별 dev/staging/prod 대응 쿼리](docs/SCENARIO-RESPONSE-QUERIES.md) · [이번 수정·실행 가이드](docs/FOLLOWUP-REPAIR-2026-09-21.md) · [v11 Redis/Kafka 수정](docs/STARTUP-REPAIR-2026-09-21.md) · [기존 품질 검사 명령](docs/QUALITY-GUIDE.md) · [Ansible](ansible/README.md)

[로컬 운영 자동화 목표와 단계별 로드맵](docs/LOCAL-OPS-AUTOMATION-ROADMAP.md)

> **MVP와 HA 실습은 서로 다른 데이터·컨테이너 구성입니다.** `bash all.sh up`은 네 HA 실습과 독립형 6개 컨테이너 MVP를 순차 기동하고, `bash all.sh down`은 역순으로 종료합니다. MVP만 별도로 관리하려면 `bash all.sh mvp up/down`을 사용하세요.
> 컨테이너 엔진 지원과 실기동 검증은 실습별로 다릅니다. Elasticsearch 9의 최신 Podman 검증은 [ES9 README](elasticsearch-9/README.md)에, Kafka/Redis의 범위는 [수정·검증 기록](docs/STARTUP-REPAIR-2026-09-21.md)에 있습니다. 한 실습의 결과를 전체 프로젝트의 지원 보증으로 간주하지 마세요.
> v12는 MVP의 HTTP 대상 확인, API/worker 준비 상태, 초기화 실패 증적, 진단 종료 코드와 실제 Helm CI 경로를 보강했습니다. v11의 Redis/Kafka 수정은 유지합니다.
> 실제 수정 근거와 실행한 검사/실행하지 못한 검사는 위의 **이번 수정·실행 가이드**에서 확인하세요.
> 오프라인 테스트는 실제 클러스터 기동·장애 복구를 증명하지 않습니다. 프로젝트별 문서에서 검증 날짜/provider를 확인하고, 전체 조합의 잔여 인수 항목은 [런타임 인수 가이드](docs/RUNTIME-ACCEPTANCE.md)를 따르세요.

Elasticsearch, Kafka, MariaDB HA, Redis 실습을 모아 놓은 프로젝트입니다.
프로젝트 루트의 `all.sh`에서 각 실습의 기존 `lab.sh`를 호출하거나,
별도의 Kubernetes 통합 Helm chart를 관리할 수 있습니다.

## DB들이 함께 동작하는 작은 MVP

주문 저장(MariaDB) → 이벤트 전달(Kafka) → 검색(Elasticsearch), 상세 캐시(Redis)를 연결한
공부용 시스템을 `mvp-lab/`에 추가했습니다. **기존 HA 클러스터에 자동 연결하지 않는 별도 6개 컨테이너 구성**이며,
기존 HA 실습의 DB 데이터/설정은 변경하지 않습니다. `all.sh up/down/restart`의 기본 lifecycle 배치에도 MVP를 포함합니다.

```bash
./all.sh mvp init
./all.sh mvp doctor
./all.sh mvp up
./all.sh mvp smoke
# 브라우저: http://127.0.0.1:18090
./all.sh mvp stop kafka --yes
./all.sh mvp diagnose
./all.sh mvp resume kafka
```

단순 주문·검색 화면, 장애 시 부분 동작, 다른 Redis로 교체, 잘못된 DB 암호, 새 검색 인덱스 전환과 재구축을 제공합니다.
설치 조건·명령·실습 순서·한계는 [MVP 가이드](mvp-lab/README.md),
이번 추가 작업의 검증 범위는 [MVP 검증 기록](mvp-lab/docs/VALIDATION.md)을 확인하세요.

## 초기 개선 작업과 현재 적용 경계

초기 2026-09-18 진단 리포트에 따라 실행 설정·부하 제어·안전 기본값·Helm 토폴로지를 수정했습니다.
변경 상태와 미검증 항목은 [개선 작업 보고서](docs/REMEDIATION-REPORT.md), 실제 기동·복구 인수 시험은
[런타임 검증 절차](docs/RUNTIME-ACCEPTANCE.md)를 기준으로 확인하세요. 기존 `docs/*VALIDATION*`와
하위 프로젝트의 오래된 보고서는 해당 시점의 기록이며 이번 수정본의 실기동 증거가 아닙니다.

**이번 버전은 `all.sh` 하나만 교체하면 안 됩니다.** 루트 `scripts/`, 하위 변경 파일, Helm chart를 함께 적용하세요.
기존 `.env`·DB 볼륨은 별도 보존하고 새 코드 사본에서 비교하세요. DB 데이터 마이그레이션이나 자동 삭제를 수행하지 않습니다. Kafka 기동 시 선택한 토폴로지 설정을 저장하며, Redis의 구형 생성 설정에는 누락된 cluster bus 리스너 항목만 보완합니다.

## 루트에서 통합 관리

여기서 **root는 프로젝트 최상위 디렉터리**를 뜻합니다. 관리자 계정으로
전환하는 기능이 아니며, `all.sh`는 `sudo`를 호출하지 않습니다.
원래 실습을 실행하던 동일한 사용자 계정으로 실행하세요.

```bash
chmod +x all.sh
./all.sh help
./all.sh list

# 환경 파일 준비 → 호스트 사전 검사 → 전체 실행
./all.sh init
./all.sh preflight
./all.sh doctor
./all.sh up

# 상태 조회 / 종료 / 재시작
./all.sh status
./all.sh down
./all.sh restart
```

`bash ./all.sh ...`로도 실행할 수 있습니다. 인자가 없으면 도움말만 출력하며,
자동으로 DB를 시작하거나 데이터를 지우지 않습니다. 현재 작업 디렉터리와
관계없이 `all.sh`가 위치한 프로젝트를 찾습니다. Bash 4.4 이상이 필요하며,
Python·컨테이너 런타임 등 실제 요구사항은 각 실습의 README를 따릅니다.

### 공통 명령

| 명령 | 동작 |
| --- | --- |
| `list` | 프로젝트와 진입점 목록 |
| `init` | 각 실습의 환경 파일 초기화 |
| `preflight [--json]` | 엔진·설정·계획 포트 충돌·호스트 조건 검사 |
| `health [--json]` | 애플리케이션/토폴로지 판정, 비정상 시 실패 코드 |
| `doctor`, `check` | 각 실습의 기존 사전 검사 |
| `up`, `start` | 환경 초기화 후 순차 시작 |
| `status`, `ps` | 각 실습의 기존 상태 명령 |
| `down`, `stop` | 선택 순서의 역순으로 종료, DB 볼륨 보존 |
| `restart` | 프로젝트별 `down → init → up`, DB 볼륨 보존 |
| `test`, `offline-tests` | 선택한 실습의 기존 호스트 전용 테스트 |
| `self-test` | `all.sh` 자체의 모의 회귀 테스트 |
| `reset <대상...> --yes` | 지정한 실습의 데이터까지 삭제 |
| `logs <프로젝트> [인자...]` | 한 프로젝트의 기존 로그 명령에 전달 |

공통 `up/down/restart`의 기본 대상은 `elasticsearch → kafka → mariadb → redis → mvp`이며, 종료는 역순입니다. `init/status/doctor/health/preflight/test`는 네 HA 실습을 기본 대상으로 유지합니다. MVP는 이 명령들에 `mvp` 대상으로 지정할 수 있습니다. 공통 `reset all`은 HA 실습 네 곳만 대상으로 하며 MVP 데이터를 삭제하지 않습니다.
`elasticsearch-9`(`es9`)는 기본 배치에서 **제외**된 별도 9.x 랩이라 항상 이름을 명시해야 합니다.
legacy 7.x 랩(9200/9000/5601)과 포트가 달라(9201/5602) 두 랩을 동시에 기동할 수 있습니다.
`down`은 선택한 대상의 역순으로 종료하고 볼륨을 보존합니다. `restart`도 선택한 순서에 따라 한 프로젝트씩 종료한 뒤 시작합니다.
직접 대상을 나열하면 입력한 순서를 사용하며 중복 별칭은 한 번만 처리합니다.
`all`은 다른 프로젝트 이름과 함께 쓰지 않습니다.

```bash
./all.sh up kafka redis
./all.sh status es mariadb
./all.sh down kafka redis
./all.sh --dry-run up
./all.sh --fail-fast up
./all.sh reset redis --yes
```

`--dry-run`은 **실행할 명령을 표시만 하는 루트 옵션**입니다. 하위 스크립트를
실행하거나 환경 파일을 만들지 않습니다. 파일 존재 여부와 인자는 검사하지만,
실제 컨테이너 설정이나 클러스터 연결을 검증하는 기능은 아닙니다.
기본적으로 한 프로젝트가 실패해도 나머지는 계속 처리하고 결과를 요약합니다.
`--fail-fast`는 첫 실패에서 중단하며, Ctrl+C 또는 종료 신호로 중단된 명령 뒤에는
다음 프로젝트를 시작하지 않습니다. 이미 실행한 프로젝트의 변경을 자동으로
롤백하지는 않습니다.

공통 `up/restart`는 종합 preflight가 실패하면 어떤 프로젝트도 시작·종료하지 않습니다.
기존 프로세스가 점유한 포트는 소유권을 단정할 수 없으므로 경고만 출력합니다. 사전 검사는
전체 메모리/디스크 용량 충족이나 이미지 pull을 보장하지 않습니다.

루트 변경 작업에는 `flock` 잠금이 적용되고 `reports/all-*.json`에 명령별 결과가 남습니다.
기본 `.state/`/보고서 권한은 비공개이며 환경 변수, 암호, SQL 인수를 기록하지 않습니다.
루트 잠금은 다른 사본의 컨트롤러나 직접 실행한 하위 `lab.sh`까지 통제하지 않습니다.
`health`는 DB 조회 기반 점검입니다. 일부 하위 컨트롤러가 로컬 `.state`/생성 설정을 갱신할 수는 있지만
데이터 쓰기·장애 주입·볼륨 삭제를 시험하지 않습니다. `status`/배치 `OK`와 구분하세요.

공통 `up`은 환경 초기화가 실패하면 해당 프로젝트를 시작하지 않습니다.
`restart`도 종료 실패 후 시작하지 않습니다. 프로젝트별 DB 기동/복구 판단은
기존 컨트롤러에 맡깁니다.

`status`는 네 프로젝트의 **기존 상태 명령을 그대로 실행**합니다. 따라서 아직
시작하지 않은 실습은 오류를 반환하거나 자체 준비 대기 시간만큼 기다릴 수 있습니다.
요약의 `OK`는 명령 종료 코드가 0이라는 뜻이며, 모든 노드가 건강하다는 보장은
아닙니다. 표시된 DB 상태도 확인하세요.

### 환경 파일과 런타임

Elasticsearch와 Kafka는 `.env`가 없을 때에만 `.env.example`에서 복사하며
새 파일의 권한을 `0600`으로 설정합니다. 기존 파일은 덮어쓰지 않습니다.
MariaDB와 Redis는 **기존 `lab.sh init`**을 이용해 비밀번호 생성과 환경 검증을
수행합니다. 공통 `up`도 이 초기화 절차를 먼저 수행합니다.

각 `lab.sh`는 자신의 디렉터리에서 별도 프로세스로 실행합니다. 서로의 `.env`를
루트 셸에 로드하거나 합치지 않습니다. 컨테이너 엔진 선택도 기존 구현을 따르며,
이 스크립트가 모든 실습을 Docker로 변환하거나 패키지를 자동 설치하지 않습니다.
환경 변경 전에는 각 프로젝트 README와 기존 데이터를 확인하세요.

### Elasticsearch 공용 코어와 9.x 랩

legacy 7.x(`elasticsearch/`)와 9.x(`elasticsearch-9/`) 랩은 `lib/es-lab/`의 공용 코어
(`common.sh` + `lablib_core.py` + `datagen/realistic.py`)를 공유합니다. 랩별 `scripts/common.sh`는
코어 로더이며, 노드·네트워크·스냅샷·진단 공통 로직은 코어에, 버전별 헬퍼는 각 랩
`scripts/lablib.py`에 있습니다. **시드 데이터 생성기**(`datagen/realistic.py`)는 양 랩이 공유하므로
같은 seed·설정이면 동일 문서를 만듭니다. ES9의 범위는 코어 스택 + 스냅샷 + 시드 + 쿼리 검증이며
장애 시나리오/샤드 드릴은 legacy 전용입니다. 사용법·설계·한계는
[elasticsearch-9/README.md](elasticsearch-9/README.md)를 확인하세요.

```bash
# ES9 랩 (opt-in — 기본 배치에 포함되지 않음)
./all.sh es9 doctor
./all.sh es9 up
./all.sh es9 seed --size-mb 5
./all.sh es9 verify
./all.sh es9 status
./all.sh es9 snapshot
./all.sh es9 down
```

### 개별 프로젝트 기능 그대로 사용

`./all.sh <프로젝트> <기존 명령> [인자...]` 형식은 네 실습의 전체 기능을 그대로
사용하는 통로입니다. 프로젝트 이름만 주면 해당 실습의 `--help`를 표시합니다.
인자 안의 공백이나 SQL 문자열도 배열로 전달하며 `eval`을 사용하지 않습니다.

| 프로젝트 | 사용 가능한 이름 |
| --- | --- |
| Elasticsearch 7.x | `elasticsearch`, `es`, `elastic` |
| Elasticsearch 9.x (opt-in, Cerebro 없음·Kibana만) | `es9`, `elasticsearch9`, `elasticsearch-9` |
| Kafka | `kafka`, `kafka-lab` |
| MariaDB HA | `mariadb`, `maria`, `mariadb-ha-lab` |
| Redis | `redis`, `redis-lab` |

```bash
./all.sh es scenario list
./all.sh es seed --size-mb 5
./all.sh kafka up --nodes 3
./all.sh kafka seed --kind payments --count 1000
./all.sh kafka health
./all.sh mariadb sql galera1 'SELECT 1;'
./all.sh redis up --no-build
./all.sh redis seed --users 1000
./all.sh logs kafka kafka1 --follow
./all.sh logs redis redis-1 --follow
./all.sh logs es --tail 100 kibana
./all.sh mariadb --help
```

공통 명령에는 프로젝트별 옵션을 붙이지 않습니다. 예를 들어 `up kafka --nodes 3`
대신 **`kafka up --nodes 3`**을 사용합니다. 로그 추적은 한 프로젝트씩 실행합니다.
프로젝트 고유 명령의 상대 파일 경로는 해당 실습 디렉터리 기준입니다.
다른 위치의 백업 파일 등은 절대 경로로 지정하는 편이 안전합니다.

### 데이터 삭제는 별도 확인

```bash
# 볼륨을 보존하는 일반 종료
./all.sh down

# 주의: Redis 실습 데이터 삭제
./all.sh reset redis --yes

# 주의: 네 실습의 데이터 모두 삭제
./all.sh reset all --yes
```

공통 `reset`은 **대상 지정과 `--yes`가 모두 있어야** 실행됩니다. `reset --yes`만
쓰거나, `down`에 `--purge`/`-v`를 붙이면 실행하지 않고 사용법 오류를 반환합니다.
루트에서는 삭제를 다음과 같이 명시적으로 연결합니다.

| 프로젝트 | 공통 `reset`이 호출하는 기존 명령 |
| --- | --- |
| Elasticsearch | `lab.sh down --purge --yes` |
| Kafka | `lab.sh reset --yes` |
| MariaDB HA | `lab.sh reset --confirm-delete-lab-data` |
| Redis | `lab.sh reset --yes` |

**프로젝트 이름이 먼저 오면 의미를 바꾸지 않습니다.** 예를 들어
`./all.sh es reset`은 Elasticsearch의 기존 설정 복구 명령이지 볼륨 삭제가 아닙니다.
또한 `./all.sh redis start redis-1`은 개별 노드 시작입니다.
이 경로의 삭제/장애 주입 명령은 각 실습의 기존 확인 플래그와 안전장치를 따릅니다.

### 종료 코드와 테스트

배치 작업은 모두 성공하면 `0`, 하나라도 실패하면 `1`을 반환합니다.
인자 오류는 `2`이며, 개별 프로젝트/Helm 명령은 원래 종료 코드를 전달합니다.
중단은 `130`, 종료 신호는 `143`으로 처리합니다. 배치 요약은 표준 오류에 출력합니다.

```bash
bash -n all.sh
./all.sh self-test       # 루트 라우팅/오류/삭제 방지 테스트
./all.sh test            # 네 실습의 기존 호스트 전용 테스트
./all.sh test kafka redis
```

`self-test`는 Python 표준 라이브러리만 사용하며, 임시 디렉터리의 모의 `lab.sh`와
모의 Helm을 호출합니다. `test`가 호출하는 각 테스트의 추가 의존성과 보고서 생성
동작은 해당 프로젝트 테스트 스크립트를 따릅니다. 두 명령 모두 실제 DB 기동 성공을
증명하는 통합 테스트는 아닙니다. 이번 변경의 검증 내역은
[docs/REMEDIATION-REPORT.md](docs/REMEDIATION-REPORT.md)에 기록합니다.
전체 신규 보호 장치와 Go-template 부분 계약 검사는 `bash scripts/test-offline.sh`,
실제 Helm 검사는 `bash scripts/test-helm.sh`로 분리했습니다. 후자는 Helm이 없으면 실패하며 모의 도구로 대체하지 않습니다.

### 반복 벤치마크 비교

공통 runner로 선택한 기존 lab의 bounded workload를 실행할 수도 있습니다.

```bash
./all.sh benchmark redis --seconds 30 --rate 300 --workers 8
./all.sh benchmark es7 --seconds 30 --rate 100 --batch 100
./all.sh benchmark kafka --seconds 30 --rate 1000 --payload 256
./all.sh benchmark mariadb --seconds 30 --workers 4
./all.sh benchmark es9 --seconds 30 --repeat 3
./all.sh --dry-run benchmark es9 --seconds 10 --rate 5 --batch 5
```

`--repeat 3`은 같은 설정을 순차 실행하고 새 PASS report만 모아 comparison JSON을
저장합니다. 성능 비교 준비가 안 된 경우에도 blockers와 통계는 남기고 종료 코드 2를
반환합니다. 반복 비교는 최소 30초 workload만 허용합니다.

대상 lab은 미리 실행·준비되어 있어야 합니다. runner는 선택된 workload만 실행하고 lab을
기동·초기화·정리하지 않습니다. 각 DB의 rate/worker 의미와 기본값은 다르며, 비교기는
서로 다른 workload 설정을 거부합니다. 선택한 DB에 적용되지 않는 옵션은 오류로 처리합니다.

MariaDB `./lab.sh load`와 Kafka `./lab.sh seed`는 PASS 실행마다 versioned JSON을 각각
`mariadb-ha-lab/reports/benchmarks/`, `kafka-lab/reports/benchmarks/`에 저장합니다.
동일한 DB 설정과 workload의 반복 실행은 공통 비교기로 요약할 수 있습니다.

```bash
python3 scripts/benchmarks.py mariadb-ha-lab/reports/benchmarks/RUN-1.json mariadb-ha-lab/reports/benchmarks/RUN-2.json
python3 scripts/benchmarks.py kafka-lab/reports/benchmarks/RUN-1.json kafka-lab/reports/benchmarks/RUN-2.json kafka-lab/reports/benchmarks/RUN-3.json
```

서로 다른 DB/workload/status의 보고서는 거부합니다. 최소 2회 통계를 내고 3회 미만은
분산 해석이 약하다고 표시합니다. container engine/version, host fingerprint/CPU/RAM,
container limits와 실행 중
container image ID(가능한 경우 registry digest 포함)가 같으면 `environment_confirmed: true`가
됩니다. `performance_comparison_ready`에는 세 번 이상 반복, 각 workload 30초 이상, 전체 구간
host 관측, 데이터셋 postcondition도 필요합니다. run별 host 압력 차이는 별도로 공개합니다.
비교 준비가 되어도 승자, capacity 또는 production SLO를 판정하지 않습니다. 같은 경로의 보고서를
한꺼번에 넣지 말고 실행 묶음을 직접 선택하세요. Redis `./ops.sh results` export에는 실행별
`benchmark.json`이 포함되며 같은 비교기에 전달할 수 있습니다. 기존 Redis 전용 `compare`도
상세 report 호환 경로로 유지합니다.

## Kubernetes 통합 Helm 관리

일반 `up/down`에는 Kubernetes를 포함하지 않습니다. 새 기본 profile은 **Redis 3 + Sentinel 3**이며
전체 DB 배포는 별도 선택입니다. 고정 암호는 제거했고 Kubernetes Secret을 참조합니다.

```bash
export DB_LAB_CONTEXT=kind-db-lab
export DB_LAB_NAMESPACE=db-lab
./all.sh k8s init
./all.sh k8s preflight
./all.sh k8s up
./all.sh k8s status
```

명시한 context가 `DB_LAB_ALLOWED_CONTEXTS` 목록에 있어야 변경 작업을 허용합니다.
기본 허용 목록은 `kind-db-lab,k3d-db-lab,minikube`이며, 이름만 검사하므로 실제 연결 대상이 실습용인지
사용자가 확인해야 합니다. namespace `default/kube-system/kube-public/kube-node-lease`의 변경은 거부합니다.
`DB_LAB_KUBECONFIG`를 지정할 수 있으며 생략하면 기존 KUBECONFIG 규칙을 따릅니다.

기존 release values를 유지하면서 명시한 변경을 적용합니다. Helm 3.14+가 필요하고
`up/preflight`에는 values 관련 옵션만 전달할 수 있습니다. ES/MariaDB 최초 bootstrap과 전체 복구는 서로 다른 절차입니다.
**0.1.x chart의 in-place upgrade는 지원하지 않습니다.** 새 namespace/release에서 검증 후 DB 백업/복원으로 이전하세요.

설치·bootstrap 플래그 제거·Sentinel 접속·Secret·PVC·마이그레이션 전체 절차는
[helmchart/README.md](helmchart/README.md)를 따르세요. 실제 DB 기동·PVC 보존·장애 전환은 이번 환경에서 검증하지 않았습니다.


## MVP 시뮬레이션 v2

기존 6개 컨테이너 구조에 seed 기반 부하, 11개 시나리오, 읽기 전용 원본/검색/캐시 대조, 제한된 로컬 Docker 장애 주입과 복구 기록을 추가했습니다. [실행·업그레이드·해석 가이드](mvp-lab/docs/SIMULATION-GUIDE.md)를 따르세요. 실제 DB/엔진 시험은 별도이며 단위 테스트의 통과가 HA 검증은 아닙니다.

```bash
bash ./all.sh mvp simulate list
bash ./all.sh mvp simulate plan kafka-outage --seed 42
# 정상 동작하는 전용 로컬 MVP가 준비된 뒤
bash ./all.sh mvp simulate run baseline --seed 42 --yes
bash ./all.sh mvp simulate run kafka-outage --seed 42 --yes
```

## DB 시뮬레이션 v3: 선택형 네트워크·복원·자원 실습

기본 6개 컨테이너는 유지합니다. `mvp simulate`에 DB 송신 지연/패킷 손실을 추가했고,
`mvp drills`에 별도 DB로의 논리 복원·후보 버전 import 검증·Redis tmpfs full/cgroup OOM·합성 행 deadlock을 추가했습니다.
실제 DB 실행 검증은 호스트 테스트와 분리합니다. 기존 볼륨을 지우거나 in-place 업그레이드하지 않습니다.

```bash
bash ./all.sh mvp drills list
bash ./all.sh mvp simulate plan db-network-loss --seed 42
```

[범위·명령·복구·실기동 한계](mvp-lab/docs/ADVANCED-DRILLS.md)를 먼저 확인하세요.
실제 준비된 전용 스택의 순차 인수 명령은 `bash scripts/test-runtime-mvp.sh --yes`입니다.


## 메시지 정합성 실습 v4

기존 6개 컨테이너에서 별도 토픽·그룹·인덱스로 poison event, mapping rejection, projection/DLQ ACK-commit gap, 역순/중복 재생, 동일 버전 내용 충돌을 비교합니다. 정상 worker를 자동 DLQ 모드로 바꾸거나 원본 offset을 되감지 않습니다.

```bash
bash ./all.sh mvp messages list
bash ./all.sh mvp messages plan poison-schema
bash ./all.sh mvp messages run poison-schema --yes
# 준비된 로컬 Docker에서만 실행하며 합성 쓰기와 실습 자원 정리를 포함합니다.
bash ./scripts/test-runtime-messages.sh --yes
```

[실행·정리·검증 경계](mvp-lab/docs/MESSAGE-DRILLS.md), [통합 사고 대응 훈련](mvp-lab/docs/INCIDENT-EXERCISE.md), [v4 구현 보고서](mvp-lab/docs/SIMULATION-V4-REPORT.md)를 확인하세요. 기존 `.env`/`.state`/볼륨을 보존하고 전체 소스로 API/worker를 다시 빌드합니다. `--keep` 자원은 API 교체 전에 먼저 정리해야 합니다. 실제 Kafka/DB 인수 시험은 호스트 단위 검사와 별개입니다.

## 트랜잭션 정합성 실습 v5

원본 MVP와 분리된 임시 MariaDB에서 재고 동시 차감, 중복 결제, 여섯 단계 rollback, commit 결과 불명확, 요청 키/내용 충돌, 동시 환불을 비교합니다. **잘못된 구현의 목표 증상을 드러내고 보호된 구현의 주문·재고·지갑·원장·이벤트를 모두 대조**해야 실습이 통과합니다. 금액은 단일 DB의 합성 지갑이며 실제 PG/분산 결제가 아닙니다. 원본 테이블·일반 API/worker는 변경하지 않습니다.

```bash
bash ./all.sh mvp drills plan stock-race --clients 4 --seed 42
# 준비된 로컬 Docker MVP에서만 임시 DB/client를 생성하고 정리합니다.
bash ./all.sh mvp drills run stock-race --clients 4 --seed 42 --yes
bash ./scripts/test-runtime-transactions.sh --yes
```

[여섯 실습과 판정·복구 가이드](mvp-lab/docs/TRANSACTION-DRILLS.md), [v5 구현·검증 보고서](mvp-lab/docs/SIMULATION-V5-REPORT.md)를 참고하세요. `drills`는 11개, 기존 `simulate` 13개와 `messages` 6개는 유지합니다. 제작 환경에서 실제 Docker/MariaDB 인수 시험은 수행하지 못했으며 SQLite 시험 어댑터의 결과를 MariaDB 잠금·내구성 검증으로 표시하지 않습니다.

## v6: 실행 대상·드라이버·스키마를 먼저 확인하는 인수시험

기존 시나리오 30개를 유지하고 `mvp verify`를 추가했습니다. 네 가지 검증/진단 결함을 수정했으며 이미지와 소스 일치, API/worker 각각의 실제 드라이버·읽기 전용 DB 계약, 새 실행 증거와 복구/정리까지 묶어 확인합니다.

```bash
bash ./all.sh mvp verify plan core
# 보존 자원 정리와 명시적 mvp up(재빌드) 후 준비된 실습 환경에서:
bash ./all.sh mvp verify run core --yes
```

`all`은 후보 버전 시험을 제외한29개, `candidate`는 직접 선택한 이미지에 대한 별도 시험입니다. 도구가 없으면 blocked/not_run이지 PASS가 아닙니다. 이 배포 제작 환경에서는 Docker가 없어 실제 엔진 인수는 미실행입니다. [실행·오류·복구 가이드](mvp-lab/docs/RUNTIME-ACCEPTANCE.md)를 확인하세요.

## Ansible 제어 (v9-ansible)

로컬/SSH 실습 서버의 기존 `all.sh`를 사용하는 Ansible 제어와 공유 결과 수집을 추가했습니다.
`ansible/README.md`에 설치·inventory·명령·동의·check mode·복구·검증 범위가 있습니다.
기본 설정은 localhost의 MVP 상태 조회입니다. 컨테이너 런타임 설치/소스 자동 덮어쓰기나
운영 데이터 자동 초기화는 하지 않습니다. 변경/장애/reset에는 별도 동의가 필요합니다.

```bash
# 제어 PC, Python3.12~3.14 virtualenv에서
python -m pip install -r ansible/requirements.txt
cd ansible
ansible-playbook control.yml --check -e @examples/mvp-up.yml
ansible-playbook control.yml -e @examples/mvp-up.yml
```

선택형 `deploy.yml`과 `bootstrap.yml`은 각각 새 source release 배치와 Ubuntu 24.04/Debian 12 package 설치를 지원합니다. `ansible/README.md`의 단일 호스트·check mode·정확한 설치 동의 예시를 확인하세요. 별도 원격 OS/DB 실기동 검증 범위도 함께 구분합니다.

전체 프로젝트와 기존 `.env/.state/volumes`를 보존하세요. `bash scripts/test-ansible.sh`는 실제 Ansible 로컬 fixture, `bash scripts/test-ansible-ssh.sh`는 임시 loopback `sshd`의 실제 SSH 인증과 모듈 전송을 확인합니다. 두 검사는 별도 원격 서버의 OS/runtime 배치나 실제 DB 기동·복구를 증명하지 않습니다.
