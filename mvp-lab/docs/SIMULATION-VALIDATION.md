# DB 시뮬레이션 v2 구현·검증 보고서

**작성일: 2026-09-19 · 입력: `db-lab-with-mvp.zip` · 작업: 별도 소스 사본 수정**

## 결론

기존 소형 주문 MVP에 seed 기반 부하와 11개 자동 시나리오, 읽기 전용 데이터 대조, 안전한 오류 분류, 로컬 Docker 대상 고정 및 장애 복구 기록을 추가했습니다. 기존 검증에서 지적한 V01~V08도 코드/계약 수준에서 보완했습니다.

**전체 호스트 회귀 테스트 843개와 독립 재검사 19개가 통과했습니다. 실제 이미지 빌드·DB 기동·잠금·Kafka 재연결·볼륨 보존은 이 환경에서 수행하지 않았습니다.**

사용자 호스트·기존 DB·클러스터에 접속하거나 사용자 원본 ZIP을 수정하지 않았습니다. 이번 결과물은 변경된 전체 프로젝트 패키지입니다. 실제 실행은 로컬 Docker용 CLI로 제공하며, 서비스가 없을 때 메모리 DB로 대체해서 성공 처리하는 경로는 없습니다.

## 1. 구현 범위

기본 6개 컨테이너(MariaDB, Kafka, Elasticsearch, Redis, API, worker)를 유지했습니다. 선택형 Redis spare 외에 새 상시 서비스는 없습니다. 기존 별도 HA 실습 및 Helm 런타임 코드를 변경하지 않았습니다. 루트 `all.sh`의 기존 MVP 위임 경로를 그대로 사용하며 일반 전체 `up/down`에 MVP를 끼워 넣지 않았습니다.

### 시나리오

| 구분 | 시나리오 | 실제 실행 시 사용하는 동작 |
|---|---|---|
| 기준 | baseline | 합성 주문/조회/변경/재시도 계획 실행, 데이터 대조 |
| 의존성 중단 | kafka-outage / es-outage / redis-outage | 소유권을 확인한 단일 컨테이너 stop/start |
| 응답 정지 | db-freeze / worker-freeze | 소유권을 확인한 단일 컨테이너 pause/unpause |
| 교체 | redis-recreate | 동일 이미지 ID·named volume 조건의 컨테이너 재생성 |
| 연결 대상 교체 | redis-switch | 기존 선택형 spare 연결 후 normal 복원 |
| SQL 경합 | row-lock | 실행 전용 합성 주문 한 행의 제한시간 트랜잭션 잠금 |
| 멱등성 | duplicate-retry | 동일 키·동일 payload로 HTTP POST 반복 |
| 동시 수정 | version-race | 동일 expected_version의 변경 2개 동시 요청 |

이 표는 구현 내용입니다. 실제 Docker에서 11개를 모두 실행한 결과가 아닙니다. row-lock은 데드락 실험이 아니고, pause는 패킷 지연이나 서버 전체 장애의 동등한 재현이 아닙니다.

### 부하·관측 계약

워크로드는 mixed/read-heavy/write-heavy/hot-key입니다. seed는 계획의 입력과 순서를 재현하고 실제 시간을 고정하지 않습니다. 최대 400개 workflow, 부하 HTTP 요청 2,400개, 동시 HTTP 8개, 계획 부하 180초, 장애 유지 요청 30초 등 학습용 한계를 둡니다. 밀린 작업은 무한 큐로 쌓지 않고 건너뛰며 건너뛴 슬롯 수를 기록합니다.

요청 지연은 클라이언트 측 HTTP 값이며 준비/부하/관측/대조와 성공/오류/충돌/결과불명을 분리합니다. 관측 트래픽도 서버 부하를 만들므로 벤치마크 도구로 주장하지 않습니다. 복원 이후 수렴 관측 시간은 표본 기반 상한이며 RTO/RPO나 서버 내부 latency가 아닙니다.

### 데이터와 성공 판정

요청별 승인 스냅샷, idempotency key, 변경 시도 버전을 보존합니다. 타임아웃 뒤 확인은 같은 키를 읽기만 하며 재전송으로 새 데이터를 만들어 결과를 숨기지 않습니다. SQL 원본, ES, 이미 존재하는 Redis 값을 공개 필드 8개 전체로 비교합니다. 관측은 cache fill/DEL, ES refresh/rebuild를 하지 않습니다.

승인 기록의 존재/부재와 원본 존재/부재/미확인을 분리합니다. 타임아웃을 바로 데이터 유실이라고 부르지 않습니다. 캐시 MISS는 허용하지만 값이 존재할 때 불일치하면 기록합니다. 두 차례 연속 실행 전용 주문의 대조가 맞고 복원·연결·시나리오 효과가 확인돼야 통과합니다. 효과를 관측하지 못하면 `inconclusive`, 복원/대조 실패는 `failed`입니다.

## 2. 기존 V01~V08 조치

| ID | 변경 | 이번 증거와 잔여 조건 |
|---|---|---|
| V01 | smoke에 SQL 승인 응답과 검색의 전체 필드 비교 | 훼손된 검색 payload를 실제 loopback HTTP 시험에서 거부. 실 ES 검증은 별도 |
| V02 | HTTP status/허용된 ES error type, SQL 오류 분류, event 위치 | mapping400과 unavailable503이 다른 로그. 원문 응답·비밀번호 출력하지 않음 |
| V03 | 엔진 선택값 + 실제 endpoint/daemon ID digest 고정 | context/daemon 변경·잘못된 migration 거부를 mock 기반 검증. 실 엔진 권한/신원 인증을 대체하지 않음 |
| V04 | 임의 SQL 진단 제거, orders/outbox/counts/ping 고정 조회 | 공백 변형 FOR UPDATE 포함 임의 SQL이 DB 클라이언트로 전달되지 않음 |
| V05 | 공통 test-offline.sh에 MVP 포함 | 실제 스크립트 호출 경로 검사와 여섯 suite 재실행 |
| V06 | MariaDB SHA256SUMS에서 배포에 없는 로그 항목 정리 | 최종 존재 파일의 해시 검증. 기존 MariaDB 서비스 코드는 변경 없음 |
| V07 | rebuild attempted/indexed/skipped_older_version/errors 분리 | SQL v1/ES v7 조건에서 indexed0, skipped1. 버전 보호 유지 |
| V08 | partition offset_state/retention_gap_offsets와 offset_warning | committed0, low100, high100에서 lag0이어도 gap100 경고. 유실 주문 수로 표현하지 않음 |

독립 재검사 19개 중 V01/V07/V08은 공개 결과 계약이 바뀐 부분만 시험 어댑터를 갱신했습니다. V01은 명시적 RuntimeError 거부, V07은 구분된 건수 dict, V08은 새 경고 필드명을 확인합니다. 테스트를 삭제하거나 기준을 완화한 것이 아닙니다. 원래 검사 코드도 증거 묶음에 보존합니다.

## 3. 이번에 실행한 검사

| 검사 | 결과 | 범위 |
|---|---:|---|
| root | 91 통과 | 라우팅/보호/제한된 chart template 계약 |
| MVP | 199 통과 | 기존 109 + 신규 90. mock/계약/HTTP 검사 |
| Elasticsearch | 77 통과 | 기존 호스트 검사 |
| Kafka | 94 통과 | 기존 호스트 검사 |
| MariaDB | 198 통과 | 기존 호스트 검사 |
| Redis | 184 통과 | 기존 호스트 검사 |
| **호스트 회귀 합계** | **843 통과** | 실패·skip 0 |
| 독립 V01~V08 및 기존 동작 재검사 | **19 통과** | 실 DB 없는 별도 인수 기준 |
| Python AST | 83 통과 | Python 구문 |
| Bash -n | 54 통과 | 셸 구문 |
| 일반 YAML | 24 통과 | 파싱. Helm 템플릿 11개 제외 |
| UI JavaScript | 1 통과 | Node 구문 검사. 실제 브라우저/DB 통합 검사는 아님 |

최종 전체 suite는 `full-validation/summary.json`과 각 로그가 기준입니다. 초기 109/156/195개 및 재실행은 최종 843개에 중복 집계하지 않았습니다.

### 신규 시뮬레이터 HTTP 시험의 의미

테스트 안에서 실제 loopback HTTP 서버와 실제 API/업무/시뮬레이터 코드를 실행했습니다. DB·Kafka·Docker는 명시적인 double입니다. 15초 계획의 Kafka 중단·복원 흐름에서 요청·관측·최종 대조·보고서 생성이 이어지는 것을 확인했습니다.

그 예시는 `loopback-test-double-example/`에 보관하며 `evidence_kind=TEST-DOUBLE-DBS-WITH-REAL-LOOPBACK-HTTP`로 표시합니다. **그 파일에 있는 ms/초 값은 MariaDB·Kafka·Redis·ES 성능 수치 또는 실제 장애 복구 시간으로 사용하지 마세요.**

Docker 명령은 전용 테스트에서 stub/mock으로 처리했습니다. 이미 중단/정지된 대상의 소유권 오인, 다른 container ID 복원, 원격 target 실행, 잘못된 복구 payload, 부분 실패 뒤 marker 보존, 다른 이미지/볼륨을 안전한 교체로 간주하는 오류를 검사했습니다.

## 4. 실행 명령 사전 검사와 한계

임시 사본에서 다음 명령을 실제 호출했습니다. 사용자 서비스에는 적용하지 않았습니다.

| 명령 | 종료 코드 | 해석 |
|---|---:|---|
| mvp simulate list | 0 | 11개 시나리오 표시 |
| mvp simulate plan kafka-outage --seed 42 | 0 | 동일 seed 재호출 결과 동일 |
| mvp init 두 번 | 0 / 0 | 기존 .env 유지 |
| mvp doctor | 1 | Docker/Podman 없음 |
| mvp simulate run baseline --yes | 1 | 엔진 없음. 가짜 실기동 성공으로 처리하지 않음 |
| mvp simulate run row-lock (--yes 없음) | 2 | 명시적 동의 누락 거부 |

이 환경에 Docker·Podman·Helm·kubectl이 없습니다. 설치나 외부 호스트 연결은 하지 않았습니다. 호스트 Python은 `full-validation/summary.json`에 기록된 버전이며 컨테이너의 Python3.12 설치 호환성은 미검증입니다.

**미실행:** 이미지 pull/build, 실제 SQL session 설정·행 잠금·동시 트랜잭션·1205 반환, 실제 Kafka 재연결/offset, ES refresh/버전 보호, Redis 재시작/AOF, Docker pause/recreate/spare 전환, 볼륨 보존, 고정 버전 드라이버 연결, 실 브라우저→실 DB, Helm/Kubernetes, HA·RTO/RPO·취약점 스캔.

### 검사 도중 바로잡은 사항

최초 전체 실행 호출은 도구의 짧은 실행 제한으로 중단되었습니다. 이후 전체 실행기를 끝까지 실행한 최종 결과를 집계했습니다. 첫 독립 재검사의 V01 어댑터는 기대 오류 문자열을 잘못 적어 1건 ERROR가 발생했고, 실제 명시적 거부 문자열에 맞춰 수정 후 19개 모두 통과했습니다. 초기 로그/코드와 최종 로그를 구분해 보존했습니다. 이는 서비스 코드에서 거부가 실패한 것이 아니라 검사 어댑터의 문자열 오류입니다.

## 5. 적용 순서

전체 ZIP을 별도 폴더에 풀고 기존 `.env`/`.state`/볼륨을 보존합니다. 기존 설치는 `docker context show`와 `docker info`로 대상 확인 후 `mvp bind-target --yes`, `mvp up`, `mvp smoke` 순서로 시작합니다. 신규 설치는 `mvp init`, `mvp doctor`, `mvp up`으로 초기 pin을 만듭니다.

첫 자동 실습은 `simulate run baseline --seed 42 --yes`, 그 다음 동일 옵션으로 `kafka-outage`입니다. 실 데이터가 없는 전용 로컬 스택에서 한 번에 한 시나리오만 실행하세요. `--seconds`는 부하 구간이지 준비/복구를 포함한 전체 명령 최대 시간이 아닙니다.

시나리오가 남긴 복구 marker가 있으면 `simulate recover --yes`를 사용합니다. 실제 엔진/컨테이너 ID가 달라지면 복구도 중단됩니다. 운영자 확인 없이 pin이나 marker를 삭제하여 보호 장치를 우회하는 절차는 제공하지 않습니다.

## 6. 구현하지 않은 범위

네트워크 지연/손실, 디스크 full/I/O 장애, OOM, 실제 deadlock 생성, 메이저 버전 업그레이드와 다운그레이드, 백업 복원, 복제 HA/다중 호스트, poison message/DLQ 재처리, 재고·결제 정합성 모델은 이번 범위가 아닙니다. 컨트롤러가 SIGKILL되거나 호스트가 꺼졌을 때 pause를 자동 해제하는 외부 watchdog도 없습니다. 일반 종료에서는 복원을 시도하고, 강제 종료 뒤에는 marker 기반 수동 복구가 필요합니다.

코드가 많아졌다는 이유로 모든 장애를 정교하게 재현했다고 주장하지 않습니다. 이번 개선은 **같은 요청을 주면서 DB마다 실패 양상이 왜 다른지, 승인과 실제 저장이 어떻게 다른지, 복구가 끝났다는 판단을 어떤 증거로 내리는지**를 배우는 데 집중합니다.

## 7. 파일 안내

프로젝트의 `mvp-lab/docs/SIMULATION-GUIDE.md`에 명령·안전 범위·판정 해석이 있습니다. 주요 구현은 `tools/simulation.py`, `tools/sim_engine.py`, `mvp_app/inspection.py`, `mvp_app/lock_drill.py`, `mvp_app/observability.py`입니다.

증거 묶음의 `final-summary.json`, `full-validation/`, `extended-results.json`, `static-cli/`, `archive-integrity.json`, `source-diff.json`, `tools/`, `loopback-test-double-example/`를 함께 확인하세요. 산출물별 실제 해시와 파일 수는 패키징 완료 후 생성한 `archive-integrity.json`이 기준입니다.

공식 동작 계약은 가이드의 Docker pause, MariaDB FOR UPDATE, Confluent Python API, Elasticsearch7.17 Index API 링크를 참고했습니다. 문서 조회는 고정 이미지 실행 증거가 아닙니다.
