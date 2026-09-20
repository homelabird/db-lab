# DB 시뮬레이션 v8 — 정상·장애 짝 실험과 증거 비교

작성일: 2026-09-19. 기존 30개 실습/기본 6개 컨테이너는 유지합니다. 새 장애 종류가 아니라 **같은 입력 계획의 정상 실행과 장애 실행을 비교**하는 계층입니다. 제작 환경에서는 실제 Docker/DB를 실행하지 못했습니다. 모의 DB 예시는 `test_only`이며 성능 수치가 아닙니다.

## 1. 첫 실행

전체 프로젝트를 반영하고 기존 `.env`, `.state`, named volume을 보존하세요. `all.sh`만 교체하지 않습니다. `messages --keep` 보존 자원은 API 재생성 전에 기존 명령으로 확인·정리합니다. 엔진 pin이나 복구 marker를 삭제해 우회하지 마세요.

```bash
# 이미 준비한 전용 실습 환경은 init이 기존 설정을 보존합니다.
bash ./all.sh mvp init
bash ./all.sh mvp doctor
bash ./all.sh mvp up

# 목록·계획은 DB를 변경하지 않습니다.
bash ./all.sh mvp study list
bash ./all.sh mvp study plan kafka-outage --seed 42 --workload write-heavy

# 합성 주문 쓰기와 Kafka 중단·복원을 포함합니다.
bash ./all.sh mvp study run kafka-outage --seed 42 --workload write-heavy --yes
```

`study run`은 **core 인수검사 → 비교용 새 baseline → 선택한 장애 실행 → 비교 보고서** 순서입니다. core에는 smoke와 자체 baseline이 있으므로, 전체로는 smoke와 부하 실행 세 번이 포함됩니다. core의 baseline은 옵션이 다를 수 있어 비교용으로 재사용하지 않습니다. 어느 선행 단계라도 실패·미확인이면 다음 장애를 적용하지 않습니다. 모든 합성 주문은 남습니다.

준비된 로컬 Linux Docker+Compose v2만 자동 실습 대상입니다. runner는 pull/build/up/reset을 자동 실행하지 않습니다. 별도 상시 서비스·대시보드 서버·운영 DB 연결을 추가하지 않았습니다.

## 2. 지원 범위와 요청 계획

짝 비교 대상은 `kafka-outage`, `es-outage`, `redis-outage`, `db-freeze`, `worker-freeze`, `redis-recreate`, `redis-switch`, `db-network-delay`, `db-network-loss` **9개**입니다.

`row-lock`, `version-race`, `duplicate-retry`는 scenario에 따라 요청 의미/계획이 달라 같은 baseline과 직접 비교하지 않습니다. 기존 개별 `simulate`/`drills`/`messages` 명령으로 계속 실행합니다. 별도 임시 DB의 트랜잭션/메시지 시나리오를 기본 파이프라인 HTTP 속도와 섞지 않습니다.

```bash
bash ./all.sh mvp study run redis-outage \
  --seed 42 --workload read-heavy \
  --seconds 60 --rate 3 --workers 4 \
  --fault-at 15 --fault-for 15 --recovery-timeout 120 --yes
```

옵션의 단위는 기존 Plan과 같습니다. `rate`는 초당 계획 workflow 목표이지 HTTP RPS나 DB TPS가 아닙니다. 한 workflow가 여러 요청을 보낼 수 있습니다. 계획 기간 15–180초, 동시 HTTP 1–8개, workflow 최대 400개, 부하 HTTP 최대 2,400개입니다. `--step-timeout`은 자식 단계별 120–1,200초(기본650)이며 전체 실습/서버 작업이 반드시 그 시간에 종료된다는 보장은 아닙니다.

네트워크 시나리오는 먼저 `bash ./all.sh mvp drills prepare --yes`로 helper를 준비합니다. 패킷 손실 난수까지 workload seed로 고정하지 않습니다.

## 3. 무엇을 같은 조건으로 확인하나

각 실행 전후에 엔진 binding, 앱/도구 소스 파일, Compose/Containerfile/직접 의존성 파일, 설정 전체의 해시, 실제 이미지 ID와 DB 볼륨, 클라이언트 Python/플랫폼을 기록합니다. 평문 비밀번호를 비교 보고서에 복사하지 않습니다. 컨테이너 ID만 바뀌는 재생성은 허용하지만 이미지·볼륨이 달라지면 다른 조건입니다.

정상 실행을 통과한 뒤 **장애를 적용하기 직전에도 현재 조건을 다시 조회**합니다. 정상 실행과 조건이 달라졌다면 장애 단계 전에 거부합니다. 장애 실행 도중/이후 변동도 실패·비교 차단으로 남깁니다.

이것은 조건의 일부가 동일함을 확인하는 장치입니다. CPU 사용률·디스크 경합·메모리 여유·DB 크기·캐시 warm 상태를 동일하게 만들지 않습니다. 정상 실행이 먼저 데이터를 만든다는 순서 효과도 있습니다. 원본 초기화나 캐시 삭제로 억지로 조건을 맞추지 않습니다. 두 디렉터리의 컨트롤러나 수동 Docker 조작까지 전역 잠그지는 못합니다. 해시는 서명/독립적인 엔진 신원·실행 인증이 아닙니다.

## 4. 결과 파일과 판정

```text
mvp-lab/reports/studies/study-<실행ID>/
  plan.json
  summary.json
  report.md
  report.html
  comparison/                # 두 실행까지 완료한 경우
    comparison.json
    report.md
    report.html
```

`report.html`은 로컬 파일로 여는 정적 HTML입니다. JavaScript·CDN·외부 요청·서버가 없으며, 화면에서 DB를 끄거나 재처리하는 기능도 없습니다. 보고서에는 요청 원문이나 주문 원문을 넣지 않습니다. 파일 권한은700/600이며 원본 실행 기록은 별도로 보존합니다.

runner 상태는 passed/failed/blocked/inconclusive/aborted입니다. 통과한 **짝 실행 수는 0~2**이고 core는 별도 단계입니다. 선행 실패 뒤는 not_run입니다. Docker가 없으면 blocked/127이지 호스트 모의 테스트로 대체한 PASS가 아닙니다. 자식의 종료코드0만 보지 않고 새 실행 ID·계획·컨텍스트·로그 수치·복원·데이터 대조를 검사합니다.

비교기 상태는 다음과 구분됩니다.

| 상태 | 의미 |
|---|---|
| complete | 제공된 두 기록이 live 표지/조건/실험 통과 기준을 만족. 독립적인 DB 재실행 인증은 아님 |
| test_only | 제공된 기록에 모의 의존성 표시가 있음. 실제 DB 결과가 아님 |
| not_comparable | 계획·소스·설정·이미지 등이 다르거나 구간을 확정할 수 없음. 지연 차이 계산 억제 |
| fault_not_recovered | 비교 조건은 맞지만 장애 실행 자체가 통과하지 못함 |

runner의 단계가 실패하면 자동으로 재시도하거나 데이터 복구를 수행하지 않습니다. 실패한 simulation ID가 남아 있으면 아래 offline 비교 명령으로 읽을 수 있지만, 계획 시작 이전의 실패처럼 필요한 파일/시각이 없으면 비교 자체를 거부합니다.

## 5. 보고서의 핵심 지표

**전체 요청을 보존합니다.** 계획·수용·건너뜀·완료 workflow 수, HTTP 성공·409 충돌·오류·결과불명(status0), 준비·관측·대조 요청 수를 따로 표시합니다. 성공 표본의 지연만 줄고 실제 수용량이 줄거나 오류가 증가한 것을 개선으로 부르지 않습니다. 아무 workflow도 실행하지 못한 정상 실험은 더 이상 통과하지 않습니다.

**같은 작업과 구간끼리 비교합니다.** 실제 장애 요청/적용확인/복원요청/복원확인의 시각으로 before / apply_transition / fault / restore_transition / after를 나눕니다. 정상 실행에도 같은 상대 구간을 적용합니다. 요청이 경계를 걸치면 `:boundary`로 남기며 오류 총계에는 포함하지만 단일 구간 지연 차이에서는 제외합니다. 장애가 부하 기간 바깥까지 걸리면 구간을 만들어내지 않고 비교 차단합니다.

**대기와 통신을 분리합니다.** 전체 지연, 클라이언트 동시성 슬롯 대기, 통신·직렬화 경로 시간을 나눕니다. 슬롯에서 시간 초과한 요청은 transport 미시도이며 통신 지연0 표본으로 넣지 않습니다. transport는 서버 SQL 처리시간이 아닙니다. 각 중앙값은 별도로 계산하므로 대기p50 + 통신p50이 전체p50과 같을 필요는 없습니다. 단조 시계의 차이로 시간을 재며 서로 다른 실행의 절대 monotonic값은 비교하지 않습니다.[S1]

**표본 부족은 0이 아닙니다.** 동일 작업·결과·안정 구간에서 양쪽 각각 p50은5개, p95는20개, p99는100개 이상일 때만 차이를 표시합니다. 미달이면 `—`/null입니다. 이것은 작은 표본 과해석을 줄이는 표시 정책이지 통계적 유의성·신뢰구간·성능 우열 판정이 아닙니다. 낮은 기본 부하에서는 p95/p99가 자주 비는 것이 정상입니다.

**복구는 속도와 따로 봅니다.** 기존 전체 필드 주문 대조·연속 일치·장애 해제 판정을 유지하고 관측한 outbox/lag 최대, 최종 주문 상태, 복원 후 대조 관측 시간을 표시합니다. 조회 실패한 lag는0이 아니라 미확인입니다. 표본 최대는 전체 최대가 아니며 offset 간격은 유실 주문 수가 아닙니다. RTO/RPO 인증을 추가한 것이 아닙니다.

## 6. 이미 생성한 두 결과를 읽기만 비교

v8의 measurement_schema2·컨텍스트가 있는 실행만 받습니다. 아래 변수에는 실제 결과의 `sim-...` 값을 지정합니다.

```bash
bash ./all.sh mvp study compare "$BASELINE_RUN_ID" "$FAULT_RUN_ID"
```

DB/HTTP/Docker를 호출하지 않고 로컬 파일을 읽어 `reports/comparisons/compare-.../`에 새 결과를 만듭니다. `.env`도 필요하지 않습니다. 두 simulation은 같은 입력 옵션과 계획을 사용해야 합니다. 예전 v7 로그에 없는 시계나 소스 정보를 추측해서 채우지 않으며 재실행을 요구합니다.

JSON 중복 키·NaN/Infinity·뒤집힌 시각·미수용 workflow의 요청·위조된 표본 수/요약·미완료 workload의 PASS를 거부합니다. 심볼릭 링크·경로 이탈·입력크기 상한도 검사합니다. Python 기본 JSON의 일부 완화 동작을 쓰지 않는 별도 엄격 파서를 사용합니다.[S2] 이것은 악의적인 작성자가 모든 증거 파일을 일관되게 새로 만든 경우까지 진위를 인증하는 서명이 아닙니다.

## 7. 실패 시 읽는 순서

`study/summary.json`의 실패 단계와 자식 실행 ID → 자식 simulation의 timeline → 해당 구간 요청/오류 → 원본·검색·캐시 대조 순서로 확인합니다. core에서 막히면 runtime-contract와 startup 진단부터 봅니다. 새 비교기는 스택을 고치지 않습니다.

네트워크/stop/pause 장애 marker가 남았다면 기존 `bash ./all.sh mvp simulate recover --yes`로 소유권을 확인하고 복구합니다. 다른 계열의 marker는 해당 `drills/messages recover`를 사용합니다. 호스트 종료·SIGKILL 이후 자동 회복을 새로 보장하지 않습니다.

## 8. 검증 범위

호스트 단위/경계 시험과 실제 loopback HTTP + 명시적 MemoryRepo/MemoryCache/MemorySearch/MemoryBroker 시험을 수행했습니다. 실제 Runner로 정상과 Redis 장애를 순서대로 실행해 strict load → 비교 → HTML 생성까지 검사하지만 DB·엔진은 모의 객체입니다. 예시에는 `TEST-DOUBLE-DBS-WITH-REAL-LOOPBACK-HTTP`, 비교에는 `test_only`가 남습니다. 시간 수치를 Redis/MariaDB/Kafka/ES 성능으로 읽지 마세요.

실제 Docker/DB 인수는 별도입니다. 제작 환경의 차단 결과와 최종 전체 검사 수는 `SIMULATION-V8-REPORT.md` 및 증거 묶음을 확인하세요. 기존 HA·Helm·CI 원격 실행·물리 복구·엔진 현대화의 검증 공백도 이 비교 기능으로 해결됐다고 주장하지 않습니다.

[S1] Python 3.12 time.monotonic: https://docs.python.org/3.12/library/time.html#time.monotonic

[S2] Python 3.12 json의 parse_constant/object_pairs_hook와 입력 크기 주의: https://docs.python.org/3.12/library/json.html

문서는 API 의미 확인용이며 대상 이미지에서의 실제 실행 증거가 아닙니다.
