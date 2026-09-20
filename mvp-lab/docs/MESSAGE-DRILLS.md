# DB 시뮬레이션 v4 — 잘못된 메시지, 격리, 재처리, 버전 충돌

작성일: 2026-09-19. v3의 작은 주문 MVP에 추가하는 선택형 실습입니다.
**구현·호스트 검사 통과와 실제 Docker/DB 실행 성공은 다릅니다. 이 배포 제작 환경에서는 실제 브로커와 DB를 시작하지 않았습니다.**

## 1. 무엇을 배우는가

DB가 모두 살아 있어도 잘못된 메시지 하나가 뒤의 정상 주문 검색을 막을 수 있습니다. 이번 실습은 장애가 서버가 아닌 **데이터·스키마·처리 순서**에 있을 때를 다룹니다. 정상 처리, 실패 위치 관측, 격리, 원본 기반 재처리, 최종 데이터 비교를 한 실행의 기록으로 연결합니다.

기본 컨테이너는 MariaDB·Kafka·Elasticsearch·Redis·API·worker의 6개 그대로입니다. helper는 API 컨테이너의 별도 프로세스로 실행됩니다. 새 웹 콘솔·상시 DLQ worker·Kafka Connect·Schema Registry를 추가하지 않았습니다. 기존 `simulate` 13개와 `drills` 5개는 유지하고 `messages`에 6개를 추가했습니다.

```text
이번 실습의 합성 주문 3개 ── MariaDB 원본
          │                       │
          │ 시험용 이벤트          └─ 현재 원본을 읽어 교정 이벤트 생성
          ▼                                        │
실행별 events 토픽 ── 엄격한 처리 ── 실행별 검색 인덱스 ◀──┘
       │                │
       │                └─ poison에서 중단 / 다음 정상 이벤트가 막힌 상태 관측
       ▼
허용된 영구 오류만 실행별 DLQ에 기록
       │
       └─ DLQ 전송 ACK 확인 후 원본 메시지 offset commit
```

기존 `mvp.orders.v1`의 소비 위치를 되감거나 정상 worker의 오류 정책을 바꾸지 않습니다. 실습 주문을 SQL에 만들면 **일반 outbox도 생성되어 정상 파이프라인에는 정상 주문이 전달**됩니다. 잘못된 이벤트는 별도 실습 토픽으로만 보냅니다. 따라서 일반 웹 검색이 정상인 동시에 실습 인덱스만 막히는 것이 가능합니다. 웹 화면을 고장 내는 실험으로 해석하지 마세요.

## 2. 실행 전 준비

전체 ZIP을 사용하고 기존 `.env`, `.state`, 데이터 볼륨을 보존합니다. 이전에 남겨 둔 `messages --keep` 자원은 API 컨테이너를 재생성하기 **전에** 정리하세요. 정리 도구는 생성 당시 API 컨테이너 ID까지 고정하므로 재생성 후 다른 ID를 자동으로 신뢰하지 않습니다.

로컬 Docker Linux 컨테이너와 Compose v2, 정상 모드 MVP가 필요합니다. 원격 엔진·Podman·spare/잘못된 비밀번호 모드에서 자동 주입하지 않습니다. 기존 engine pin과 프로젝트 이름이 일치해야 하며 설정을 삭제해서 보호 장치를 우회하지 않습니다.

```bash
# 프로젝트 루트. 기존 init은 .env를 보존합니다.
bash ./all.sh mvp init
bash ./all.sh mvp doctor
bash ./all.sh mvp up
bash ./all.sh mvp smoke

# 아래 두 명령은 DB 없이도 목록·실행 계획을 확인할 수 있습니다.
bash ./all.sh mvp messages list
bash ./all.sh mvp messages plan poison-schema

# 첫 실습: 완료 후 실습 토픽·인덱스만 자동 정리하고 기록과 주문은 보존
bash ./all.sh mvp messages run poison-schema --yes
```

`--yes`는 합성 주문 쓰기, 실행별 토픽/인덱스 생성, 성공 시 해당 실습 자원 정리에 대한 동의입니다. 기존 DB 초기화나 원본 토픽 삭제에 대한 동의가 아닙니다. `up`은 새 API/worker 코드를 빌드합니다. 현재 버전의 라이브러리와 실제 이미지의 호환성은 현장에서 먼저 `smoke`로 확인해야 합니다.

## 3. 여섯 가지 실험과 합격 조건

| 시나리오 | 자동으로 재현할 조건 | 코드가 요구하는 관측 |
|---|---|---|
| `poison-schema` | 지원하지 않는 `schema_version=99`를 정상 이벤트 사이에 배치 | offset 1에서 차단, 다음 주문 검색 미반영 → DLQ ACK → 후속 주문 처리 → SQL 원본으로 교정·수렴 |
| `mapping-reject` | ES strict mapping에 없는 필드를 가진 이벤트 | 실제 projection 경로의 mapping 400으로 차단 → 그 원인을 격리 → 현재 SQL의 정상 필드로 교정 |
| `projection-commit-gap` | ES 반영 뒤 Kafka commit 전에 명시적 예외 | 문서는 있지만 checkpoint는 전진하지 않음 → reader 재생성 후 같은 offset 재전달 → 동일 내용 중복으로 분류 |
| `dlq-commit-gap` | DLQ ACK 뒤 원본 commit 전에 명시적 예외 | DLQ는 읽히지만 checkpoint 불변 → 같은 원본 재전달로 동일 DLQ ID가 2회 존재 → 하나의 교정 이벤트로 재처리 |
| `replay-ordering` | SQL 원본을 1→2→3 버전으로 바꾸고 메시지는 2,1,3,2,3,1 순서로 전송 | 구버전 무시 3회, 동일 중복 1회, 최종 version 3과 전체 내용 일치 |
| `version-collision` | 같은 주문 ID·같은 version인데 상품명이 다른 이벤트 | 원래 문서를 덮어쓰지 않고 내용 충돌로 차단·격리 → SQL 기준 교정 후 일치 |

이는 **구현한 합격 기준이지 이번 환경에서 관측한 실 DB 결과표가 아닙니다.**

```bash
bash ./all.sh mvp messages run mapping-reject --yes
bash ./all.sh mvp messages run projection-commit-gap --yes
bash ./all.sh mvp messages run dlq-commit-gap --yes
bash ./all.sh mvp messages run replay-ordering --yes
bash ./all.sh mvp messages run version-collision --yes
```

한 번에 하나씩 수행하세요. 준비된 스택에서 여섯 실험을 모두 확인하는 별도 인수 스크립트도 있습니다.

```bash
bash ./scripts/test-runtime-messages.sh --yes
```

이 스크립트는 `smoke` 후 6개를 순서대로 실행하고 실패 시 중단합니다. 자동 `up`, 볼륨 삭제, 정상 offset reset으로 실패를 감추지 않습니다. 여섯 실습만으로 합성 주문 18개가 생기며 앞의 smoke가 만든 주문은 별도입니다.

## 4. 격리와 재처리가 정확히 의미하는 것

DLQ(dead-letter queue)는 이 실행 전용 Kafka 토픽입니다. 원본 이벤트 바이트·키, 원본 topic UUID/partition/offset, 오류 종류, SHA-256을 기록합니다. 같은 원본 위치와 내용은 같은 `dlq_id`가 됩니다. 동일 ID의 두 DLQ 레코드가 있어도 전달이 두 번 발생한 증거를 지우지 않고, 교정 발행만 하나로 줄입니다. 해시는 신원 인증이나 위변조 방지 서명이 아닙니다.

**격리 대상으로 인정하는 것은 제한적입니다.** 이벤트 JSON/스키마 검증 오류, 허용된 ES mapping 오류, 동일 버전·다른 내용 충돌만 해당합니다. ES 401/429/503, 연결 실패, 알 수 없는 400/409를 무조건 DLQ로 보내지 않습니다. 재시도 횟수가 많다는 이유만으로 정상 이벤트를 건너뛰지 않습니다.

전송 순서는 `DLQ publish → delivery ACK 확인 → source commit`입니다. ACK 실패 또는 commit 실패에서 원본을 완료했다고 처리하지 않습니다. Kafka의 외부 저장소 반영과 offset commit을 하나의 원자적 트랜잭션으로 묶은 구조는 아니므로 중복은 가능합니다. [S2]

교정은 악성/잘못된 payload를 임의로 수정하는 범용 변환기가 아닙니다. 이번 실행의 `order_id → idempotency_key` 허용 목록을 확인하고, **SQL의 현재 주문 상태를 읽어** 새로운 이벤트를 만듭니다. 범위 밖 주문, 바뀐 해시, 다른 실행의 DLQ, 존재하지 않는 원본은 거부합니다. 과거 결제 이력을 복원하거나 삭제된 주문을 부활시키는 기능이 아닙니다.

`commit-gap`은 지정 위치에서 의도적으로 예외를 던지고 reader를 닫아 재시작하는 실습입니다. **프로세스 SIGKILL, broker crash, 네트워크 단절, consumer group rebalance를 발생시키지 않습니다.** 수동 partition assign을 사용하고 정상 그룹의 멤버로 참여하지 않습니다. 실제 라이브러리 ACK·수동 commit 계약은 실기동으로 따로 확인해야 합니다. [S2]

## 5. 동일 버전의 내용 충돌 보호 — 일반 worker에도 적용

기존 `Search.index`는 `external_gte`를 사용했습니다. 이 모드는 버전이 같아도 저장을 허용하므로, 동일 버전이면 내용도 같다는 가정이 깨질 때 보호가 부족합니다. v4는 `external`로 바꾸고 conflict 응답 뒤 realtime GET으로 저장된 버전과 내용을 검사합니다. [S1]

| 들어온 이벤트와 기존 문서 | 처리 |
|---|---|
| 더 높은 버전 | 새 내용 반영 |
| 같은 버전·같은 내용 | `duplicate_ignored` |
| 더 낮은 버전 | `older_version_ignored` |
| 같은 버전·다른 내용 | `event_version_payload_conflict`로 실패; 덮어쓰지 않음 |
| `_source.version`과 ES `_version` 불일치 등 | 불일치를 숨기지 않고 실패 |

일반 worker도 이 저장 보호를 공유하지만 **자동 DLQ 정책은 여전히 없습니다.** 기본 파이프라인에 기존 충돌 문서가 있으면 이전처럼 덮어써서 감추지 않고 진행을 멈출 수 있습니다. 그런 경우 원본과 문서를 조사해야 하며 볼륨 삭제나 버전 임의 증가를 자동 처방하지 않습니다.

재색인 결과는 `attempted`, `indexed`, `duplicates`, `skipped_older_version`, `errors`로 나눕니다. 같은 데이터의 재시도와 새 반영 건수를 섞지 않습니다. 동시 관리자 변경 사이의 원자적 검사·삭제까지 보장하는 기능은 아닙니다.

## 6. 기록을 남겨서 관찰하기

```bash
# 끝난 뒤에도 실습 자원을 보존
bash ./all.sh mvp messages run dlq-commit-gap --keep --yes

# 출력된 실제 msg-... 값을 RUN에 넣습니다. 다음은 실행 ID 형식 설명용 자리표시자입니다.
RUN='msg-실제출력된24자리16진수'
bash ./all.sh mvp messages inspect "$RUN"
bash ./all.sh mvp messages cleanup "$RUN" --yes
```

`inspect`는 완료된 실습의 현재 checkpoint, DLQ 레코드, 실습 검색 결과를 보여 줍니다. 진행 도중의 poison 상태는 `events.json`의 단계 기록에서 읽습니다. 대화형 단계별 일시정지 콘솔은 아닙니다. 이미 정리했거나 보존 기간이 지난 토픽을 새로 만들거나 offset을 초기화해 검사하지 않습니다.

결과 위치는 `mvp-lab/reports/messages/msg-<실행ID>/`입니다.

| 파일 | 읽을 내용 |
|---|---|
| `report.md` | 단계별 요약과 판정 |
| `summary.json` | 시나리오, 최종 2회 대조, checkpoint, fixture, 정리 결과 |
| `events.json` | 수신 offset, 실패 원인, ACK 경계, 격리/교정 영수증, 최종 대조 |
| `ledger.json` | engine/API ID, scope, topic UUID, index UUID, 단계·정리 기록 |
| `source-containers.json` | 선택된 원본 컨테이너와 이미지·볼륨 상태 |
| `source-manifest.json` | 실행 시 Python 소스 해시 |

`head_of_line_block_observed` → `dlq_ack` 관측 → `repaired_from_current_sql` → `final_audit` 순서를 확인합니다. Kafka commit은 **다음에 처리할 위치**이므로 offset 1에서 막히고 committed=1이면 그 메시지는 아직 미완료입니다. [S2]

최종 대조는 이 실행의 주문 세 개에 대해 SQL 원본, 실습 ES 문서, 실제 검색 가시성, 남아 있는 scoped cache를 비교합니다. 두 차례 연속 일치해야 합니다. cache MISS는 허용합니다. 강제 ES refresh나 검사용 cache fill로 성공을 만들지 않습니다. 처리 단계의 정상 cache 무효화는 실행 전용 키에만 적용합니다. 세 DB를 하나의 원자적 스냅샷으로 읽는 것은 아닙니다.

실습 결과가 passed여도 전체 서비스·원본 검색 인덱스 전체의 정합성 보증은 아닙니다. SQL 쓰기는 동시에 일반 outbox에 반영되지만 본 실습의 최종 checkpoint는 실습 그룹만 대상으로 합니다.

## 7. 정리·중단·용량 경계

기본 성공 시 전용 events/DLQ 토픽과 검색 인덱스를 정리합니다. SQL 주문, 일반 outbox, 기본 이벤트 토픽·그룹, 원본 인덱스, DB 볼륨은 지우지 않습니다. scoped Redis 키는 30초 TTL로 사라지며 consumer group metadata는 삭제하지 않고 broker 만료에 맡깁니다. `--keep`은 최대 8개 완료 실행만 허용합니다. 보고서는 자동 삭제하지 않습니다.

실패하거나 중단되면 실패 증거와 `.state/message-active.json`을 남기고 다른 변경 명령을 막습니다.

```bash
bash ./all.sh mvp messages recover --yes
```

이 명령의 의미는 **그 실행의 자원 정리**입니다. 기본 서비스 재시작이나 정상 파이프라인 데이터 복구를 대신하지 않습니다. helper가 아직 실행 중이면 컨테이너 내 flock을 확인해 정리를 거부합니다.

매 요청에 engine pin과 프로젝트·생성 당시 API ID를 확인합니다. 삭제 전에는 모든 기록된 topic UUID와 index UUID/owner metadata를 확인하고, 각각 삭제 직전에 다시 확인합니다. 이름만 같거나 ID가 바뀌면 정리하지 않습니다. 확인과 삭제 사이에 관리자가 변경하는 경쟁을 완전히 없애는 서버측 원자적 보호는 아닙니다. [S3]

**생성 요청이 성공했으나 응답이 끊겨 UUID를 저장하지 못한 자원은 자동으로 채택·삭제하지 않습니다.** 이 경우 recover도 거부할 수 있습니다. 실제 엔진의 메타데이터·실행 기록으로 소유권을 별도로 조사해야 합니다. pin/ledger를 지워서 자동으로 통과시키지 마세요. API 컨테이너 교체 역시 자동 승계 대상이 아닙니다.

요청당 이벤트 원문 32KiB, run publish 100개, DLQ 검사 32개, 토픽별 retention 24시간/1MiB(세그먼트 단위 적용)로 제한합니다. retention 설정은 순간 디스크 사용량의 엄격한 상한이 아닙니다. helper는 SIGALRM 150초와 드라이버 타임아웃, 호스트 RPC 175초 제한을 사용합니다. 네이티브 호출·호스트 종료까지 포함하는 외부 watchdog이나 절대 실행시간 보장은 아닙니다. SIGKILL 후 helper가 살아 있으면 복구 잠금이 남을 수 있습니다.

DLQ 원문과 합성 주문은 결과 파일에 포함될 수 있습니다. 디렉터리/파일은 700/600으로 생성하지만 암호화·운영 Secret 관리 시스템은 아닙니다. 합성 데이터만 넣으세요.

## 8. 검증 수준과 남은 범위

배포 시 호스트 회귀 1,029개, 독립 검증 19개가 통과했습니다. 신규 메시지 시험 119개 중 16개는 6개 시나리오와 실패 조건을 메모리 double 및 실제 loopback HTTP+모의 ES로 검사합니다. 나머지는 정책·parser·offset/ACK 호출·실행 대상·정리 보호 계약을 검사합니다. 실제 HTTP 소켓 사용이 실제 ES/Kafka 사용을 의미하지 않습니다.

실서비스 모드에는 메모리 fallback이 없습니다. 컨테이너 런타임이 없으면 실행을 거부합니다. 실제 이미지 build/pull, pinned confluent-kafka2.8.2와 broker의 topic UUID/수동 commit, Redis TTL, ES mapping/refresh, MariaDB 트랜잭션은 실환경 인수 시험이 남아 있습니다. broker crash·rebalance·멀티 partition·다중 worker 소유권·DLQ 장기 보관·운영용 수동 재처리 UI·분산 transaction은 구현 범위 밖입니다.

## 공식 계약 참고

- [S1] Elasticsearch 7.17 Index API: `external`/`external_gte`, version conflict, refresh. https://www.elastic.co/guide/en/elasticsearch/reference/7.17/docs-index_.html
- [S2] Confluent Python client: delivery callback, flush, manual offset commit, at-least-once. https://docs.confluent.io/kafka-clients/python/current/overview.html
- [S3] 고정 라이브러리 2.8.2 TopicDescription/topic_id: https://raw.githubusercontent.com/confluentinc/confluent-kafka-python/v2.8.2/src/confluent_kafka/admin/_topic.py

확인일 2026-09-19. 공식 API 문서 확인은 고정 이미지·브로커 실기동 시험과 다릅니다.
