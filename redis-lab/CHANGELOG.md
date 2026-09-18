# 2.0.0-operations · 2026-09-18

- 기존 Redis/Sentinel/백업/기본 실습 코드와 설정을 유지.
- 별도 Compose 프로젝트의 redis-perf, ops-runner, 내부 네트워크 프록시 추가.
- bounded 혼합 부하, 단계별 지표, 독립 PING, JSONL 및 오프라인 HTML 보고서.
- 9개 독립 시나리오, CPU quota 검증, Replica backlog 초과/resync 자동화.
- 소유권 검증, 파일 잠금, 런타임 설정/CPU/복제 변경 전 복구 저널.
- 응답 오류를 포함한 pipeline을 끝까지 읽어 다음 응답과 섞이지 않게 처리.
- 지연 중 쓰기 자동 재시도 없음; 전송 시도와 연결 실패, 결과 불확실을 구분.
- 프록시 규칙 자동 만료와 내부 control 인증.
- 이전 성공 보고서 재사용 방지 및 RUNNING/FAIL/BLOCKED/NOT_REPRODUCED 구분.
- 기존 107개 포함 총 170개 오프라인/TCP 검사 통과. 실제 Podman/Redis는 BLOCKED.

---

# 1.1.0-reviewed · 2026-09-17

원본 ZIP을 재검토하여 검증/복구 로직을 수정했습니다. **실제 Redis·Podman 통합 동작을 확인한 릴리스는 아닙니다.**
작성 환경에서 통합 검증 명령을 실행한 결과는 실행기 부재로 `BLOCKED`입니다.

## 재현 후 수정한 검증 오류

1. `verify`가 성공 응답을 받은 쓰기가 하나도 없는 실행을 정상 종료하던 문제.
   이제 요청 0개 또는 acknowledged 쓰기 0개는 비정상 종료하며 JSON에도 `passed: false`를 기록합니다.
   고정 노드 접속 실험에서 장애 이후 쓰기가 전부 거부되면 이 실패 판정이 정상적인 관찰 결과입니다.
2. 거부되었거나 전송되지 않은 요청의 키에 **다른 값이 존재**해도 absent로 분류하던 문제.
   기대값과 다르더라도 키가 존재하면 `rejected_but_present` / `not_sent_but_present`로 탐지합니다.
3. `test`가 사전 준비 검사에서 실패하면 이전의 `passed: true` 보고서가 남던 문제.
   시작 즉시 RUNNING/false로 교체하고, 실패·중단·복구 실패를 모두 별도로 기록합니다.
   주 실패 원인이 복구 오류에 가려지지 않도록 두 오류를 함께 보존합니다.
   `verify`도 미완료 로그나 접속 오류에서 이전 성공 보고서를 남기지 않습니다.

위 세 가지의 원본 실행 재현은 `tests/original-defect-reproduction.json`에 있습니다.
Redis/Podman 호출을 mock으로 대체한 **Python 검증 로직의 재현**이며, Redis 서버 장애 재현이 아닙니다.

## 추가 복구 안전성 수정

- 저장 장애용 marker를 파일 변경 **전에** 기록하고, BGSAVE 완료와 쓰기 성공 **후에** 제거합니다.
  복구 도중 다시 실패하면 재시도할 수 있는 marker를 남깁니다.
- 복원 파일을 sandbox의 임시 경로로 먼저 복사하여 `redis-check-rdb` 검사를 통과한 뒤에만
  sandbox를 중단하고 데이터를 교체합니다. JSON metadata의 필수 필드도 미리 확인합니다.
  이 경로의 명령 순서/차단 동작은 mock 회귀 테스트로 확인했으며 실제 RDB 파서 실행은 미검증입니다.
- 새 config가 최종 경로에 나타나기 전에 환경 fingerprint를 원자적으로 기록합니다.
  설정 파일만 있고 fingerprint가 없는 비정상 상태는 임의로 재사용하지 않고 중단합니다.
  실제 임시 파일과 shell의 중단/재시도 테스트를 포함했습니다. Redis 실행 부분은 stub입니다.
- Sentinel 타이머를 크게 변경했을 때 90초 고정 제한 때문에 시험이 성급히 실패하지 않도록
  장애 관찰/정상화 검사 한도를 설정값에서 계산합니다. 이는 복구 시간 보장이 아닙니다.

## 실제 호스트용 통합 검증

`./lab.sh validate --yes`를 추가했습니다. 모든 단계는 실제 Podman 명령을 사용합니다.
대체 Redis 구현이나 모의 서버로 통과시키지 않습니다. 실행기가 없으면 BLOCKED, 종료 코드 2입니다.
통합 검증은 기본 기동, 자료구조/데이터, 실행 중 클라이언트의 장애 전환, 복제 인증 오류,
quorum 부족, 메모리/RDB 장애, 백업 복원, down/up 이후 데이터·역할 보존을 검사합니다.

`workload --run-id NAME`와 per-run stop 파일을 추가해, 검증기가 자신이 시작한 쓰기 작업만
종료하고 `finish` 레코드가 기록된 후 같은 run ID의 결과를 검사합니다.

로컬 이미지 태그는 `:1.1`입니다. 기존 `:1.0` 클라이언트를 계속 사용하는 일을 피하기 위해
수정판 최초 적용 시에는 `--no-build` 없이 빌드해야 합니다.

## 유지한 설계

Redis 3 + Sentinel 3 + client 1, 선택적 sandbox, 전용 static-IP 네트워크, 별도 writable
named volumes, 인증, host port 미공개, SELinux 비활성화/privileged/prune 미사용을 유지했습니다.
