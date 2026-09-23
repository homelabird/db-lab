> **v3 추가:** 네트워크 2개 + 별도 DB 실습 5개는 [ADVANCED-DRILLS.md](ADVANCED-DRILLS.md)를 참조하세요. 아래의 v2 범위와 과거 검증 수치는 당시 기록입니다.

# DB 연동 시뮬레이션 v2 — 장애를 넣고, 원인을 구분하고, 같은 데이터를 다시 대조하기

기준일: 2026-09-19. 학습용 작은 주문 시스템에 대한 구현 가이드입니다.
**이번 배포의 코드·모의 검증 완료와 실제 Docker/DB 검증 완료는 다릅니다. 실제 엔진 인수 시험은 남아 있습니다.**

## 1. 무엇이 달라졌는가

MariaDB 원본 → outbox → Kafka → Elasticsearch 검색, Redis 상세 조회 캐시라는 기존 구조는 유지합니다. 기본 컨테이너도 6개 그대로입니다. `redis-switch`에서만 기존 선택형 Redis spare를 사용합니다. 새 모니터링 서버, Kafka Connect, Kubernetes 또는 대규모 부하 플랫폼은 추가하지 않았습니다.

이제 `simulate`가 다음 순서를 실행합니다.

1. 현재 Docker 대상·프로젝트·실제 API 컨테이너의 loopback 포트·준비 상태를 검사합니다.
2. 이번 실행 전용 합성 주문 6개를 만들고 정상 상태의 원본·검색·캐시를 대조합니다.
3. seed로 정한 요청 계획을 **정상 → 장애 → 복구** 구간 동안 보냅니다. 데이터는 삭제하지 않습니다.
4. 장애 종류별로 실제 적용 상태와 증상을 관찰합니다. 작업이 중단되면 자신이 남긴 복구 기록으로 되돌립니다.
5. 응답으로 승인받은 주문과 타임아웃 등으로 결과가 불명확한 요청을 따로 추적하고, 실행 전용 주문을 다시 비교합니다.
6. 결과 JSON·Markdown과 요청·장애·진단 시각을 기록합니다. 기준에 못 미치면 실패 또는 미확인으로 종료합니다.

**동일 seed는 요청 종류·계획 순서·수량·가격을 반복합니다. UUID, 실제 지연, 스레드 실행 순서, DB 스케줄링까지 같게 만드는 것은 아닙니다.**

## 2. 첫 실행

새 디렉터리에 전체 프로젝트를 풀어 사용합니다. 루트 `all.sh`만 교체하는 방식이 아닙니다.
Linux 또는 WSL2의 Bash/Python, 로컬 Docker Engine과 Docker Compose v2를 대상으로 합니다. Docker Desktop의 Linux 컨테이너도 대상이지만 이 배포에서 실기동 확인하지 않았습니다.
**자동 장애 실습은 로컬 Unix socket/npipe로 연결된 Docker만 허용합니다. 원격 Docker와 Podman 자동 실습은 차단합니다.** 기존 개별 실습의 provider 지원을 의미 없이 확대하지 않습니다.

```bash
# 프로젝트 루트, 신규 배포
bash ./all.sh mvp init
bash ./all.sh mvp doctor
bash ./all.sh mvp up
bash ./all.sh mvp smoke

# 제공되는 시나리오와 실행 계획: DB 변경 없음
bash ./all.sh mvp simulate list
bash ./all.sh mvp simulate plan kafka-outage --seed 42

# 정상 기준 먼저
bash ./all.sh mvp simulate run baseline --seed 42 --yes

# 같은 요청 계획으로 Kafka 중단 비교
bash ./all.sh mvp simulate run kafka-outage --seed 42 --yes
```

`list/plan`은 Docker 없이도 읽을 수 있습니다. 루트 래퍼의 실행 영수증 등 로컬 기록은 생길 수 있으므로 “파일 변경도 전혀 없음”을 뜻하지는 않습니다.
`run --yes`는 **합성 주문 쓰기와 선택한 장애 조작에 대한 동의**입니다. 합성 주문은 실습 후 남습니다. 준비되지 않은 스택을 시뮬레이터가 자동 기동하지 않습니다.

### 이전 MVP에서 업그레이드할 때

이전 `.env`와 `.state`를 보존합니다. 같은 project 이름은 같은 엔진 자원을 가리키므로 다른 복사본을 동시에 사용하지 마세요. 기존 DB 볼륨을 지우거나 비밀번호/KRaft ID를 새로 만들지 않습니다.

```bash
# 먼저 사람이 의도한 엔진인지 확인합니다.
docker context show
docker info

# 이전 버전에는 엔진 pin이 없으므로, 확인한 현재 대상을 최초 1회 고정합니다.
bash ./all.sh mvp bind-target --yes

# 새 API/worker 코드와 STUDY_PROJECT 환경 변수를 반영합니다.
bash ./all.sh mvp up
bash ./all.sh mvp smoke
```

`bind-target`은 최초 확인을 대신해 주지 않습니다. 기존 pin이 있으면 다른 endpoint/daemon으로 덮어쓰지 않습니다. 이후 실제 daemon/endpoint 또는 엔진 선택 환경이 달라지면 변경 명령을 거부합니다. 정당한 호스트 이전은 자동 재바인딩 대상이 아니며, 기존 데이터와 실제 대상을 별도로 확인해야 합니다.

## 3. 시나리오 11개

| 시나리오 | 변경 | 기대 관찰과 판정 |
|---|---|---|
| `baseline` | 장애 없음 | 요청 정상 처리, 승인 주문과 원본·검색의 전체 필드 일치 |
| `kafka-outage` | Kafka 컨테이너 stop 후 같은 ID start | 장애 적용 직후 canary 주문을 실제 HTTP로 저장·검색. SQL 저장/검색 미노출과 Kafka 연결 실패/outbox 증가를 함께 관측하고 복원 뒤 수렴 |
| `es-outage` | ES stop/start | 검색 오류와 consumer 지연. outbox=0만으로 성공 판정하지 않음 |
| `redis-outage` | Redis stop/start | 캐시 연결 오류, SQL 우회, 원본·검색은 보존 |
| `db-freeze` | MariaDB pause/unpause | 프로세스를 종료하지 않고 응답을 정지. 타임아웃과 일부 캐시 조회의 차이 |
| `worker-freeze` | worker pause/unpause | 장애 적용 직후 canary 주문을 실제 HTTP로 저장·검색. DB와 Kafka는 연결되지만 outbox 또는 consumer lag가 증가하며 검색은 stale인 상태를 확인하고 재개 뒤 수렴 |
| `redis-recreate` | Redis 컨테이너 재생성 | 이미지 ID·named volume이 같은지 검사. 컨테이너 교체와 데이터 초기화를 구분 |
| `redis-switch` | API/worker를 별도 Redis로 연결 후 normal 복원 | 다른 캐시의 HIT/MISS와 잔존 캐시를 비교. 기존/spare 볼륨 삭제 안 함 |
| `row-lock` | 이번 실행 전용 주문 행 하나에 제한시간 `FOR UPDATE` | SQL 프로세스 장애가 아닌 잠금 대기. `mariadb_lock_wait_timeout`을 실제 응답에서 관측해야 효과 확인 |
| `duplicate-retry` | 같은 idempotency key로 POST 3회 | 같은 주문 ID로 반환되어야 함. 요청 재시도와 주문 중복을 구분 |
| `version-race` | 같은 expected_version으로 paid/cancelled 동시 요청 | 정상 조건에서 하나 200, 하나 409. 둘 다 200이면 계약 위반 |

위 표는 **구현된 실험과 목표 관찰**입니다. 실제 DB에서 이미 관측했다는 결과표가 아닙니다. 기존 `bad-db-password`, `fresh-search`, `rebuild-search` 수동 실험도 유지합니다.

```bash
# SQL 연결 장애와 다른 잠금 대기
bash ./all.sh mvp simulate run row-lock --seed 42 --yes

# 연결이 즉시 끊기는 경우와 다르게, 응답이 멈추는 경우
bash ./all.sh mvp simulate run db-freeze --workload read-heavy --seed 42 --yes

# 낙관적 버전 충돌과 중복 요청
bash ./all.sh mvp simulate run version-race --seed 42 --yes
bash ./all.sh mvp simulate run duplicate-retry --seed 42 --yes

# 같은 볼륨 컨테이너 교체 vs 별도 캐시로 연결 교체
bash ./all.sh mvp simulate run redis-recreate --seed 42 --yes
bash ./all.sh mvp simulate run redis-switch --seed 42 --yes
```

`stop`은 정상 종료 요청을 포함하며 강제 전원 손실의 대체가 아닙니다. `pause`는 프로세스 정지이지 네트워크 패킷 손실·선별 지연을 구현한 것이 아닙니다. Redis spare는 처음 만들 때만 비어 있고 재사용하면 기존 캐시가 있을 수 있습니다. 컨테이너 재생성은 다른 엔진/버전으로의 마이그레이션이나 빈 디스크 교체가 아닙니다.

### row-lock의 정확한 범위

API 컨테이너에서 전용 helper가 **이번 run ID로 만든 주문 한 행만** 확인하고 트랜잭션 잠금을 획득합니다. 최대 보유시간은 30초이며, 종료 시 rollback/close합니다. helper는 주문을 UPDATE하거나 삭제하지 않습니다. 이 버전의 앱 SQL 연결에는 세션별 `innodb_lock_wait_timeout=2`를 적용했습니다. 글로벌 DB 변수나 다른 연결 설정은 바꾸지 않습니다.

이는 단일 행 잠금 대기 실습입니다. **데드락을 자동 생성하는 실험은 아닙니다.** 1213 코드의 진단 분류는 추가했지만 데드락 발생을 검증했다고 보지 않습니다. 실제 행 잠금 효과는 InnoDB 트랜잭션/격리 동작을 포함해 현장 확인이 필요합니다. [S2]

## 4. 부하를 바꾸는 방법

```bash
bash ./all.sh mvp simulate run kafka-outage \
  --seed 42 --workload write-heavy \
  --seconds 60 --rate 3 --workers 4 \
  --fault-at 15 --fault-for 15 --recovery-timeout 120 --yes
```

| 옵션 | 의미 / 제한 |
|---|---|
| `--workload` | `mixed`, `read-heavy`, `write-heavy`, `hot-key`; hot-key는 기존 6개 fixture 중 하나로 읽기/변경 집중 |
| `--seconds` | 계획 부하 구간 15–180초. 준비·복구·최종 대조 시간은 별도 |
| `--rate` | 계획된 workflow 발행 목표 0.2–10회/초. **DB TPS나 HTTP RPS가 아님** |
| `--workers` | 동시 HTTP 요청 상한 1–8. `version-race`는 최소 2 |
| `--fault-at`, `--fault-for` | 부하 시작 뒤 장애 요청 시각, 적용 확인 뒤 유지 시간(최대 30초) |
| `--recovery-timeout` | 마지막 관측·대조의 제한 5–300초 |

실행당 계획 workflow 최대 400개, 부하 HTTP 요청 최대 2,400개입니다. 최초 6개 준비 주문·진단·대조 요청은 이 부하 카운터와 별도로 기록됩니다. 하나의 workflow는 조회 후 변경, POST 재시도 등으로 여러 HTTP 요청을 발생시킬 수 있습니다. 실제 요청 수와 수용·건너뛴 workflow 수를 따로 확인하세요.

일감이 밀리면 무한 큐나 따라잡기 폭주 대신 계획 슬롯을 건너뜁니다. 따라서 요청 제한은 과부하 도구의 완전한 open-loop 부하 보장을 의미하지 않습니다. 건너뛴 작업은 지연 표본에 존재하지 않으므로 높은 부하의 성능 비교는 표본 누락까지 고려해야 합니다. 진단·대조도 DB 읽기 부하를 추가합니다.

`hot-key` 업데이트는 주문의 정상 상태 전이(created→paid→shipped)를 따르므로 terminal 상태 이후에는 조회로 바뀝니다. 무한 결제/취소 반복이나 가짜로 version만 증가시키는 모델은 아닙니다.

`kafka-outage`와 `worker-freeze`는 무작위 부하 생성 주문에 기대지 않고, 장애가 확인된 직후 별도 실행 전용 canary 주문을 만듭니다. HTTP 쓰기 승인, 즉시 검색 결과, 진단 시점의 dependency/backlog를 함께 남깁니다. Kafka 정지와 worker 정지를 구분하려고 전자는 Kafka 연결 불가와 SQL outbox 증가를, 후자는 DB/Kafka 연결 유지와 outbox 또는 consumer lag 증가를 요구합니다. 복구 후 canary도 다른 실행 주문처럼 SQL/검색/캐시 대조를 통과해야 합니다. 이는 짧은 고정 workload 실습이지 실제 사용자 traffic이나 장기 backlog drain 성능 보증은 아닙니다.

## 5. 성공 기준과 보고서 읽기

결과는 `mvp-lab/reports/simulations/sim-<실행ID>/`에 저장됩니다.

| 파일 | 용도 |
|---|---|
| `report.md`, `summary.json` | 판정, 최종 주문별 상태, 구간별 지연, 충돌·계약 위반 |
| `plan.json` | seed와 계획 입력·순서 |
| `requests.jsonl` | 개별 요청의 상태/실패 종류/지연/request ID/구간 |
| `timeline.jsonl` | 장애 요청·적용·복원, outbox/lag 등 진단 |
| `audits.jsonl`, `intents.json` | 주문별 비교와 승인/불명확 요청 기록 |
| `runtime-snapshot.json` | 선택한 컨테이너 ID·이미지 ID·볼륨·상태. 환경 변수 원문 제외 |
| `source-manifest.json`, `client-runtime.json` | 실행 소스 해시와 호스트 Python 정보 |

`runtime-snapshot.json`은 기동된 컨테이너의 정보입니다. 이미지 태그의 원격 최신성·서명·취약점 검증 증거는 아닙니다. 준비 단계에서 실패하면 일부 파일은 없습니다.

### 판정 4종

**passed / 0:** 지정한 장애 동작과 효과가 관측되고, 복원 후 실행 전용 주문들이 2회 연속 전체 대조를 통과하며 의존성이 연결됩니다. 정상/중복/충돌 시나리오는 부하 HTTP 오류도 허용하지 않습니다. 기대한 409는 오류와 분리합니다.

**failed / 1:** 전체 필드 불일치, 승인 주문 확인 실패, 잘못된 동시 수정, 복원 실패 또는 제한시간 내 관측 실패 등이 남습니다.

**inconclusive / 2:** 최종 일치는 확인했지만 목표 장애 효과를 충분히 관측하지 못했습니다. 예를 들어 row-lock 실험에서 잠금 대기 코드가 한 번도 나타나지 않으면 통과로 꾸미지 않습니다.

**aborted / 130:** 사용자 중단. 가능한 범위에서 자신이 적용한 장애를 복원합니다. SIGKILL/호스트 종료 후 복원을 보장하는 watchdog은 아닙니다.

### 데이터 판정

SQL 원본·ES·이미 존재하는 캐시의 ID, 상품, 수량, 단가, 합계, 상태, 버전, 생성시각 **전체 8개 필드**를 대조합니다. 관측 전에/뒤에 SQL을 읽어 변경 중임을 감지하지만 여러 DB를 하나의 원자적 스냅샷으로 읽는 것은 아닙니다.

- `acknowledged_present`: 응답으로 승인된 주문을 원본에서 확인했습니다.
- `acknowledged_missing`: 승인 기록은 있는데 원본에서 확인되지 않습니다. 바로 “영구 유실”이라고 단정하지 않고 실패 근거를 남깁니다.
- `present_without_ack`: 응답을 받지 못했지만 같은 키의 주문이 실제로 저장돼 있습니다.
- `unacknowledged_absent`: 승인되지 않았고 원본에도 없습니다. 승인 데이터 유실과 별개입니다.
- `unresolved`: 연결 실패/관측 제한으로 확인하지 못했습니다.

타임아웃 뒤 POST를 다시 보내 확인하는 대신 **키로 읽기만 하는 조회**를 사용합니다. 검사기가 새 주문을 만들어 불명확한 결과를 숨기지 않습니다. 캐시 MISS는 실패가 아니지만 남아 있는 캐시 값이 과거 버전이면 기록합니다. 최종 대조는 캐시 TTL/정상 경로에 의한 자연 복구를 기다리며 강제로 DEL/SET·ES refresh·rebuild하지 않습니다.

UI의 **“세 저장소 대조 (읽기 전용)”** 버튼도 같은 원칙입니다. 기존 `fresh=1` 상세 조회는 SQL을 읽고 캐시를 채울 수 있으므로, 그것을 무수정 관측으로 오인하지 마세요.

### 시간·처리량 해석

p50/p95/p99는 **클라이언트 HTTP 관측값**이며 client slot 대기도 포함합니다. 성공·HTTP 오류·버전 충돌·클라이언트 결과불명 요청을 나누어 기록하고, 준비/부하/관측/대조를 분리합니다. 작은 표본의 p99는 안정된 운영 통계가 아닙니다.

`observed_convergence_seconds_after_restore`는 복원 명령 이후 마지막 대조에서 수렴을 확인한 시간 상한입니다. **RTO/RPO 인증이나 실제 DB 고유 지연이 아닙니다.** 수렴 중에는 두 차례 읽기 표본의 간격이 포함됩니다. outbox/lag 진단은 해당 MVP 전체 값이고, 최종 정합성 비교는 이번 실행 전용 주문만 대상으로 합니다. 다른 브라우저/CLI 부하를 동시에 발생시키지 않는 독립 실습을 권장합니다.

## 6. 트러블슈팅 진행 순서

1. `timeline.jsonl`에서 `fault_requested`, `fault_applied`, `fault_restore` 시각을 확인합니다. 요청 시각과 실제 적용 확인 시각은 다릅니다.
2. `requests.jsonl`의 해당 구간을 읽습니다. 503의 DB 오류, 409 버전 충돌, HTTP 결과불명(상태 0)을 혼동하지 않습니다.
3. `request_id`/`study_run`으로 API 로그를, `event_id`·partition·offset으로 worker 로그를 연결합니다.
4. SQL outbox와 Kafka lag는 전달 단계의 지표로 읽고, 주문별 원본/검색/캐시 대조에서 최종 처리 여부를 확인합니다.
5. 원인 해결 후 같은 주문을 확인합니다. 무조건 재색인·캐시 삭제·볼륨 삭제로 관측 근거를 없애지 않습니다.

```bash
bash ./all.sh mvp logs api
bash ./all.sh mvp logs worker
bash ./all.sh mvp diagnose
bash ./all.sh mvp sql orders
bash ./all.sh mvp sql outbox
bash ./all.sh mvp sql counts
```

SQL 진단 명령은 `orders|outbox|counts|ping`으로 제한했습니다. 이전 가이드의 임의 SELECT 문자열 전달 예제는 더 이상 지원하지 않습니다.

HTTP 오류 로그에는 HTTP status와 허용된 ES error type, SQL 오류에는 1045/1205/1213/연결 오류 분류를 남깁니다. 전체 응답 원문·접속 URL·비밀번호를 로그에 붙이지 않습니다. 검색 재구축은 attempted/indexed/skipped_older_version/errors를 분리합니다. Kafka는 committed가 low보다 낮거나 high보다 큰지 별도 경고합니다. offset 간격 수를 주문 유실 건수로 해석하지 않습니다. [S3, S4]

## 7. 종료·복구와 안전 경계

```bash
# 중단된 자동 실습이 복구 기록을 남겼을 때만
bash ./all.sh mvp simulate recover --yes

# 원래 수동 실험은 별도 명령입니다.
bash ./all.sh mvp experiment normal --yes
```

장애 조작 전에 `.state/simulation-active.json`에 소유 대상·작업을 저장합니다. `recover`는 이 파일과 engine pin, project label, container ID를 확인합니다. 자신이 멈춘 컨테이너를 다른 컨테이너로 오인해 시작하지 않고, 잘못된 대상에서는 복구도 거부합니다. 복원 실패 시 기록을 남기며 성공으로 처리하지 않습니다.

같은 로컬 컨트롤러의 MVP 잠금을 보유하지만 다른 디렉터리 사본이나 직접 `docker`를 실행하는 사람까지 잠그지는 못합니다. 실행 중 수동으로 컨테이너/이미지/모드를 변경하면 복구가 차단되거나 부분 적용이 남을 수 있습니다. Redis spare 시작 후 컨트롤러가 강제 종료되면 spare가 남아 있을 수 있으므로 상태를 확인하세요. spare 볼륨은 정상 복원 뒤에도 보존합니다.

컨트롤러가 동작하는 동안 pause/stop의 복원을 시도하지만 호스트가 꺼지거나 SIGKILL되면 자동 만료를 보장하지 않습니다. **최대 30초는 계획된 유지 시간이고, 외부 프로세스 없이 보장되는 장애 TTL은 아닙니다.** row-lock helper만 자체 제한시간 후 rollback합니다. 정상 컨트롤러 복원 시에도 DB 준비 완료는 다음 정합성/연결 검사로 따로 확인합니다.

이 실습은 보관 가치가 없는 합성 데이터를 사용하는 전용 로컬 MVP에만 적용합니다. 원래 HA 실습·Helm 파일과 사용자 운영 DB를 자동 연결하거나 변경하지 않습니다.

## 8. 이번에 하지 않은 것

임의 패킷 지연·손실/대역폭 제한, 디스크 full·I/O 오류, OOM kill, DB 메이저 버전 업그레이드, 과거 백업 복원, 다중 호스트/복제 HA, 자동 deadlock 생성, poison event/DLQ 재처리, 실제 결제·재고 업무 모델은 구현하지 않았습니다. 이 범위를 섞어 “모든 DB 장애 시뮬레이션”이라고 부르지 않습니다.

컨테이너 런타임이 없는 제작 환경에서는 실제 DB를 시작하지 않았습니다. 테스트 double을 쓴 결과는 `evidence_kind=TEST-DOUBLE...`로 표시합니다. API/worker 실사용 코드는 실제 드라이버를 그대로 사용하며, 정상 실행에서 메모리 DB로 대체하는 옵션은 제공하지 않습니다.

## 참고한 공식 계약

[S1] Docker pause: 프로세스 정지와 Linux freezer cgroup. https://docs.docker.com/reference/cli/docker/container/pause/

[S2] MariaDB FOR UPDATE: InnoDB 트랜잭션과 행 잠금. https://mariadb.com/docs/server/reference/sql-statements/data-manipulation/selecting-data/for-update

[S3] Confluent Python API: watermark/committed offset. 현재 문서를 참고했지만 고정 이미지/라이브러리 실행 검증은 별개입니다. https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html

[S4] Elasticsearch 7.17 Index API: external version과 이전 버전 충돌. https://www.elastic.co/guide/en/elasticsearch/reference/7.17/docs-index_.html
