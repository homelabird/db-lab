# DB 시뮬레이션 v4 구현·검증 보고서

**작성일: 2026-09-19 · 입력: db-lab-simulation-v3.zip · 출력: db-lab-simulation-v4.zip**
입력 SHA-256: `e4986fa23cdfb6553eca0f657ecb469cbcdd5f760eb2705b925087005cbf939d`

## 결론

학습용 6개 컨테이너 MVP에 메시지 오류·격리·재처리·중복·역순·동일 버전 내용 충돌을 다루는 `messages` 실습 6개를 추가했습니다. 기존 `simulate` 13개, `drills` 5개는 유지합니다. 정상 토픽/그룹의 offset을 되감거나 일반 worker를 자동 DLQ 정책으로 변경하지 않았습니다.

**전체 호스트 회귀 1,029개와 별도 독립 재검사 19개가 통과했습니다. Docker/Podman/Helm/kubectl이 없어 실제 DB·브로커 기동과 장애 복구는 수행하지 못했습니다.** 결과 파일은 모의 의존성, 실제 loopback HTTP, 실제 엔진 미실행을 구분합니다. 사용자 호스트에 배포하거나 원본 ZIP을 변경한 것이 아니라 별도 프로젝트 사본을 수정한 작업입니다.

## 1. 추가 기능

| 시나리오 | 구현과 확인 조건 |
|---|---|
| poison-schema | 잘못된 schema_version에서 source offset 불변·후속 정상 문서 미반영 관측 → ACK 뒤 격리·현재 SQL 교정 |
| mapping-reject | strict mapping의 알 수 없는 필드 오류를 관측하고 인프라 오류와 구분 |
| projection-commit-gap | ES 반영 뒤 commit 전 예외 → 같은 offset 재전달 → 동일 중복 구분 |
| dlq-commit-gap | DLQ ACK 뒤 commit 전 예외 → 같은 격리 ID 2건 기록 → 교정 발행 1건 |
| replay-ordering | SQL 버전 1→2→3, 이벤트 2/1/3/2/3/1 → 구버전·중복 분리·최신 전체 내용 대조 |
| version-collision | 같은 ID/version·다른 내용에 대해 덮어쓰기 차단 및 원본 기반 교정 |

실행마다 새로운 events/DLQ 토픽·그룹·ES 인덱스·Redis key prefix를 사용합니다. 정상 SQL에 합성 주문 3개를 만들기 때문에 정상 outbox에도 정상 주문 이벤트가 생깁니다. **주입한 잘못된 메시지는 별도 토픽에만 전송**됩니다. 기본 웹 검색이 정상이어도 실습이 적용되지 않았다는 뜻은 아닙니다.

`commit-gap`은 명시적 Python 예외입니다. 실제 프로세스 kill, broker crash, consumer rebalance, 네트워크 단절은 발생시키지 않습니다. manual partition assign으로 재전달과 수동 checkpoint 순서를 확인하도록 했습니다. SQL/ES/Kafka 전체의 exactly-once 또는 원자적 트랜잭션을 구현한 것이 아닙니다.

## 2. 기존 애플리케이션에서 함께 고친 부분

`Search.index`를 external_gte에서 external로 바꿨습니다. 동일 버전 충돌 후 realtime GET으로 내용까지 확인하여 동일 재시도, 구버전, 내용 충돌을 나눕니다. 같은 버전인데 데이터가 다르면 `event_version_payload_conflict`로 실패하며 기존 문서를 덮어쓰지 않습니다. ES metadata와 source version이 불일치하는 경우도 통과시키지 않습니다. 외부 버전의 의미는 Elastic 7.17 공식 API를 확인했습니다.[S1]

이 변경은 일반 worker와 재색인에도 적용됩니다. **기존에 동일 버전 충돌 데이터가 있으면 이후 일반 worker가 멈출 수 있습니다.** 조용히 덮어쓰던 동작보다 오류가 드러나는 방향의 계약 변경이며 자동 DLQ/볼륨 초기화로 해결하지 않습니다. 재색인은 duplicates 카운터를 추가해 실제 반영·구버전 무시와 분리했습니다.

## 3. 실행·정리 보호

로컬 Linux Docker+Compose v2, engine pin, 실제 API 컨테이너 ID와 정상 MVP 환경을 확인합니다. 토픽 생성 전에 의도를 기록하고 응답을 받은 즉시 topic UUID를 저장합니다. ES index UUID와 owner metadata도 기록합니다. 삭제 전에 모든 자원의 동일성을 확인하고 삭제 직전에 다시 확인합니다. 원본 topic/index/volume을 이름 패턴만으로 삭제하는 경로는 없습니다.

기본 성공 시 실습 토픽과 인덱스만 정리합니다. `--keep`은 최대 8개 완료 실행을 보존하며 `inspect`와 명시적 `cleanup`으로 관리합니다. SQL fixture/outbox는 남습니다. scoped cache는 30초 TTL이며 그룹 metadata는 broker 만료 대상입니다. 보고서와 원문 DLQ는 합성 데이터라도 민감하게 취급하도록 700/600 권한으로 생성합니다.

실패·중단 시 marker를 남기고 다른 변경 작업을 막습니다. `messages recover --yes`는 소유 자원을 정리하는 명령이며 원본 DB 데이터 복구가 아닙니다. API 안의 flock과 deadline을 공유하여 아직 실행 중인 helper와 cleanup이 충돌하지 않도록 합니다.

**실행 결과를 모를 때는 실패 쪽으로 닫습니다.** 자원 생성은 성공했으나 UUID 응답을 받지 못한 경우, API가 교체된 경우, 이름은 같고 자원 ID가 다른 경우 자동 정리도 거부할 수 있습니다. 사람이 metadata와 증거를 조사해야 하며 pin/marker를 지워서 우회하도록 안내하지 않습니다. 검사와 삭제 사이 관리자가 변경하는 경쟁을 완전히 없애는 원자적 서버측 보호는 아닙니다.

## 4. 검증 결과

| 묶음 | 실행 수 | 결과 |
|---|---:|---|
| root | 91 | 통과 |
| MVP | 385 | 통과: v3의 266 + 신규 메시지 119 |
| Elasticsearch | 77 | 통과 |
| Kafka | 94 | 통과 |
| MariaDB | 198 | 통과 |
| Redis | 184 | 통과 |
| **전체 호스트 회귀** | **1,029** | 실패·skip 없음 |
| 독립 기존 V01~V08 및 흐름 재검사 | 19 | 통과; 위 합계와 별도 |

신규 테스트는 4개 파일에 있습니다. 여섯 시나리오와 음성 조건을 memory double 및 실제 requests/loopback HTTP + 모의 ES로 검사한 16개, 정책/입력/ACK·offset/정리 보호 검사 103개입니다. HTTP는 실제 소켓이지만 SQL·Kafka·Redis·ES 엔진의 내구성 증거가 아닙니다. `test-double-examples/`의 6개 예시는 같은 시험 경로를 다시 실행한 자료이며 테스트 수에 더하지 않았습니다.

정적 검사: Python AST **100개**, Bash **56개**, 일반 YAML **24개**, UI JavaScript **1개** 통과. Helm 템플릿은 일반 YAML 검사에서 제외했으며 실제 Helm lint/template로 계산하지 않았습니다. MariaDB SHA256SUMS와 Redis MANIFEST.sha256도 검사했습니다.

### 명령 인수 검사

| 명령/검사 | 결과 |
|---|---|
| messages list / plan, 같은 plan 재실행 | 성공·출력 동일 |
| run --yes 누락 / 존재하지 않는 시나리오 | 각각 종료2, 올바른 거부 |
| init 두 번 | 성공·기존 .env 내용 보존 |
| doctor / messages run --yes | 종료1, 실제 엔진 부재 |
| scripts/test-runtime-messages.sh --yes | **종료127, Docker 없음** |
| scripts/test-helm.sh | **종료127, Helm 없음** |

이 검사들은 임시 사본에서 수행했으며 배포 ZIP에 시험용 .env, engine pin, 실제 실행 marker를 넣지 않습니다.

### 검증 환경·초기 실행 기록

호스트 Python 3.13.5에서 검사했습니다. 앱 이미지 Python3.12의 설치·실행은 별도입니다. 환경의 Python startup hook 영향을 배제하려 `-S`와 설치된 site-packages의 PYTHONPATH, python3 PATH shim을 사용했습니다. 프로젝트 기능 코드를 mock fallback으로 바꿔 실행하지 않았으며 각 테스트에 명시된 double 경로만 사용했습니다.

최초 root suite와 한 묶음 실행은 도구 실행 제한으로 중간 종료되었습니다. 미완료 로그를 보존하고 각각 다시 실행해 최종 결과를 집계했습니다. 최초 새 테스트 호출 1회는 잘못된 작업 디렉터리로 mvp_app import가 실패했고 올바른 디렉터리에서 재실행했습니다. 이러한 미완료·환경 호출 오류를 통과 수에 넣지 않았습니다.

독립 V07 검사는 공개 rebuild 결과에 새 `duplicates: 0` 필드가 생겨 기대 dict만 갱신했습니다. 구버전 indexed=0/skipped=1 요구는 유지했습니다. 원래 검사 코드와 변경 후 코드를 모두 보존했습니다. 기존 애플리케이션 테스트의 external_gte 기대값·409 응답 fixture·rebuild dict는 새 보호 계약에 맞춰 바꾸고, 별도 119개로 신규 경계를 검사했습니다.

## 5. 적용과 운영 변화

```bash
# 전체 소스 사용, 기존 .env/.state/볼륨 보존
bash ./all.sh mvp doctor
bash ./all.sh mvp up
bash ./all.sh mvp smoke
bash ./all.sh mvp messages plan poison-schema
bash ./all.sh mvp messages run poison-schema --yes
```

이전 messages의 retained 자원은 API 재생성 전에 정리합니다. 생성 당시 API ID가 달라진 자원의 자동 승계는 제공하지 않습니다. `--keep` 없는 기본 실행은 최종 상태를 보고서에 남기고 실습 자원을 정리하므로 `inspect`하려면 `--keep`을 사용합니다. 첫 실습은 실패 증거를 보존하는 절차까지 포함해 전용 합성 데이터 스택에서 수행합니다.

## 6. 미검증·미구현 범위

실제 이미지 pull/build, confluent-kafka2.8.2의 broker topic UUID·수동 commit·delivery ACK, MariaDB transaction, ES mapping/version/refresh, Redis TTL, 실제 cleanup·SIGKILL 이후 회복은 실행하지 못했습니다. 코드/계약 검사를 실제 HA·RTO/RPO·성능·저장 내구성 검사로 해석하지 않습니다.

기능상 기본 파이프라인의 운영용 DLQ 도입, 재처리 웹 UI, 다중 partition·다중 worker·rebalance, 장기 DLQ 보존과 ACL/암호화, 분산 transaction, 실제 장비·다중 호스트 HA, 물리 백업/PITR, in-place 메이저 업그레이드, ES legacy 현대화는 별도 작업입니다. 기존 v3의 실제 DB 검증 공백이 이번 메시지 기능으로 해소된 것은 아닙니다.

## 7. 자료와 출처

프로젝트 가이드: `mvp-lab/docs/MESSAGE-DRILLS.md`. 주요 구현은 message_safety.py, message_runtime.py, message_drill.py, tools/messages.py이며 실환경 검사 진입점은 scripts/test-runtime-messages.sh입니다.

증거 ZIP의 `full/summary.json`, `extended-results.json`, `static-cli-results.json`, `archive-integrity.json`, `source-diff.json`과 각 로그가 실제 집계·패키지 근거입니다. `test-double-examples/`는 **실제 DB가 아닌** 명시적 예시입니다. 최종 ZIP 재검사 결과를 테스트 합계에 중복 집계하지 않습니다.

[S1] https://www.elastic.co/guide/en/elasticsearch/reference/7.17/docs-index_.html

[S2] https://docs.confluent.io/kafka-clients/python/current/overview.html

[S3] https://raw.githubusercontent.com/confluentinc/confluent-kafka-python/v2.8.2/src/confluent_kafka/admin/_topic.py

공식 계약 확인일: 2026-09-19. 문서 조회는 고정 이미지의 실행 증거가 아닙니다.
