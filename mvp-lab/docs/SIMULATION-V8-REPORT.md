# DB 시뮬레이션 v8 구현·검증 보고서

**작성일: 2026-09-19 · 입력 db-lab-simulation-v7.zip · 출력 db-lab-simulation-v8.zip**

## 1. 결과와 변경 경계

기존 30개 시나리오와 기본 6개 컨테이너를 유지하면서, 같은 요청 계획의 정상 실행과 장애 실행을 연결하는 `mvp study`를 추가했습니다. 자동 짝 실행은 9개 시나리오를 지원합니다. 임시 DB의 트랜잭션/메시지 실험이나 요청 의미가 다른 시나리오는 속도 비교 대상에 섞지 않았습니다.

사용자 호스트·운영 DB·원격 GitHub에는 접속하거나 배포하지 않았습니다. 첨부 ZIP을 별도 작업 사본에서 수정했습니다. 일반 API/worker 코드·Compose·이미지 기본값·기존 HA·Helm·CI workflow는 변경하지 않았습니다. 호스트 측 관리기/시뮬레이터와 새 비교 모듈·테스트·문서를 변경했습니다.

**최종 호스트 회귀 1,407개와 독립 재검사 19개를 통과했습니다. 실제 Docker/DB 짝 실험은 미실행이며 CLI는 blocked/127, 통과한 live pair는0/2입니다.** 비교 예시는 실제 loopback HTTP와 모의 DB/엔진으로 생성했고 `test_only`를 유지합니다.

## 2. 구현 내용

| 변경 | 동작 | 해석 제한 |
|---|---|---|
| paired runner | core gate → 새 baseline → fault → 비교. 선행 실패에서 중단 | core의 자체 baseline도 실행되므로 전체는 smoke+부하3회. 원본 합성 주문은 남음 |
| 같은 계획 | seed/workload/rate/workers/시간 예산/operation 목록 일치 | DB 크기·캐시·CPU 부하·실제 branch/실행 순서를 고정하지 않음 |
| 실행 조건 | engine/source/config/image/volume/client fingerprint를 실행 전후 확인, 장애 직전 재검사 | hash는 서명·독립 신원 인증 아님. 수동 Docker나 다른 폴더 작업의 전역 잠금 아님 |
| 요청 측정 | monotonic 시작/끝, phase 시작/끝, workflow ID, client queue/transport/total 분리 | transport도 순수 DB 지연 아님. queue timeout은 transport0으로 넣지 않음 |
| 장애 구간 | 실제 request/applied/restore-request/restore-confirmed로 구간 설정 | transition/boundary는 총계에 남기고 안정 구간 delta에서 제외 |
| 전체 표본 | 계획/수용/누락/완료, 성공409/오류/결과불명, 준비/관측/대조 분리 | 수용량 감소와 오류를 숨긴 성공 latency만으로 개선 판정 안 함 |
| 정합성 | 기존 전체 필드·연속 대조·복원 판정 유지, 요청/완료/최종 상태 기록 재계산 | 같은 결과 로그의 검증이며 DB를 다시 조회하는 독립 감사 아님 |
| offline compare | v8 실행 두 개만 읽어 HTML/Markdown/JSON 생성 | v7에 없는 시계·컨텍스트를 추측해 채우지 않고 재실행 요구 |
| 엄격한 입력 | 크기·경로·symlink·duplicate JSON key·nonfinite 숫자·시간·요약 수치 검사 | 악의적으로 모든 증거를 일관되게 재작성한 경우 진위 인증 불가 |

비교 보고서는 정적 HTML로 JS/CDN/외부 요청·별도 서버 없이 열립니다. 주문 원문·환경 변수·자유 형식 오류는 포함하지 않으며 고정 분류/집계만 내보냅니다. 원본 보고서를 임의로 안전한 공개 자료라고 인증하거나 자동 업로드하지 않습니다.

p50/p95/p99 delta는 같은 작업·결과·안정 구간에서 양쪽 각각5/20/100개 이상일 때만 표시합니다. 이는 표시 기준이며 통계적 유의성이나 충분한 표본 수를 인증하는 공식이 아닙니다. 단일 순차 pair에는 warm-up/순서 효과가 남으므로 A/B 성능 우열·HA·RTO/RPO로 표현하지 않습니다.

## 3. 판정 보강

실제 부하 workflow/HTTP 요청이0인데 준비 주문만 일치하는 실행은 `no_workload_observed`로 실패합니다. 비교기도 빈 부하·미완료 workflow·admission 없이 생성한 요청·요약과 로그의 불일치를 거부합니다. 이미 있는 보고서의 `passed` 문자열이나 종료0을 그대로 믿지 않습니다.

정상 run과 장애 run이 모두 완료해도 계획·source/config/image/volume 등이 다르면 `not_comparable`이며 지연 차이를 표시하지 않습니다. TEST 표지가 있는 입력은 `test_only`입니다. 실제 엔진 표지가 있는 자료도 `not_an_independent_runtime_attestation=true`로 구분합니다.

실행 중 오류는 원래 recover marker를 지우거나 재실행으로 감추지 않습니다. core/paired 단계 상태와 새 run ID를 남깁니다. 정리 명령도 자동 호출하지 않으며 사용자 조사 후 기존 recover를 사용합니다. 장애 시나리오 단계가 실패한 경우 필요한 v8 로그가 있으면 별도 offline compare로 읽을 수 있습니다.

## 4. 검증 결과

| suite | 최종 테스트 수 | 결과/범위 |
|---|---:|---|
| root | 91 | 호스트 제어/제한된 Go chart 계약, 실제 Helm 아님 |
| MVP | 763 | 기존658 + 신규105, mock/HTTP/SQLite 어댑터/파일 검증 |
| Elasticsearch | 77 | 기존 호스트 검사 |
| Kafka | 94 | 기존 호스트 검사 |
| MariaDB | 198 | 기존 호스트 검사 |
| Redis | 184 | 기존 호스트 검사 |
| **전체** | **1,407** | 실패·skip 없음 |
| 독립 기존 V01~V08 등 | **19** | 위 합계와 별도, 기존 검사 코드 미수정 |

신규105개는 분석/입력/표61, 측정/컨텍스트19, 짝 실행기24, 실제 loopback pair1입니다. 서브테스트와 재실행은 중복 합산하지 않습니다. 원본v7의1,302개도 이번에 실제 재실행했습니다. 최초 v8 전체1,406개 이후 브라우저에서 발견한 표 값 반복에 대한 회귀1개를 더해 최종1,407개입니다. 실제 최종 배포물을 새로 해제한 suite 결과를 최종 기준으로 사용합니다.

정적 검사는 Python128개, Bash58개(그중 POSIX sh5 추가), 일반 YAML/workflow25개, JavaScript1개를 통과했습니다. YAML 구문은 Compose/GitHub/Helm 실기동 검사가 아닙니다. 기존 MariaDB SHA256SUMS와 Redis MANIFEST.sha256도 검사했습니다.

### 실제 loopback 시험과 HTML

실제 API/Runner/HTTP 코드에 명시적 MemoryRepo/Cache/Search/Broker 및 FakeLab을 연결하여 정상·Redis 장애를 각각15초 계획으로 실행했습니다. workflow admission, 요청/장애 시계, 최종 대조, 원본 파일 strict load, pair compare, HTML 생성까지 이어집니다. 결과는 `TEST-DOUBLE-DBS-WITH-REAL-LOOPBACK-HTTP` 및 `test_only`입니다. 기록된 지연·복원 후 관측시간을 DB 성능이나 실제 장애 복구 시간으로 사용하지 마세요.

Chromium에서 정적 HTML을 직접 렌더링하여 test_only 표시, script 없음, 외부 요청 없음, 390px 화면의 가로 페이지 넘침 없음을 확인했습니다. 데스크톱1440px와 모바일390px 스크린샷을 검토했습니다. 이것은 static report 검사이지 웹 앱→실DB UI 시험은 아닙니다.

## 5. 실제 CLI 결과

별도 임시 프로젝트 사본에서 실행했습니다. 시험용 env/state/실행 marker는 배포하지 않습니다.

| 명령 | 관측 |
|---|---|
| study list | 종료0, 9개 pair 대상 |
| study plan 동일 seed/옵션 두 번 | 종료0, 출력 동일 |
| 미지원 row-lock pair | 종료2, 계획 거부 |
| run --yes 누락 | 종료2, 동의 누락 거부 |
| init 두 번 | 종료0/0, 기존 env 바이트 유지 |
| doctor | 종료1, 엔진 없음 |
| study run kafka-outage --yes | **종료127 / blocked / 0/2 pair 통과, baseline/fault not_run** |
| env가 없는 사본에서 모의 보고서 compare | 종료2 / test_only, DB 호출 없이 새 HTML 생성 |
| 경로 이탈 ID | 종료1, 읽기 거부 |

호스트 Python3.13.5, `-S`와 명시한 site-packages/PATH shim으로 환경 시작 hook 영향을 분리했습니다. 실제 앱 Python3.12의 이미지 설치/드라이버 실행은 하지 않았습니다. Docker/Podman/Helm/kubectl 및 native DB 서버가 없어 실제 image pull/build·DB 연결·HA·복구·remote CI 실행은 미검증입니다. 시뮬레이터가 서비스 부재를 메모리DB로 대체하는 일반 실행 경로는 없습니다.

## 6. 제작 중 확인하고 수정한 사항

첫 분석 테스트 호출과 일부 파일 작성 명령에서 작업 디렉터리를 잘못 지정한 환경 호출 오류가 있었습니다. 올바른 경로에서 다시 실행했으며 해당 미완료 호출을 통과 수에 세지 않습니다. 기존 HTTP 테스트 출력에 일부 ResourceWarning이 있어 로그 그대로 보존합니다. 테스트 실패나 DB 엔진 오류를 성공으로 바꾼 것이 아닙니다.

첫 headless CLI screenshot 호출은 시간 초과로 이미지가 없었습니다. 이후 Playwright로 같은 정적 HTML을 렌더링했습니다. 390px에서 긴 TEST 문구로 페이지 폭이588px이 되는 문제를 발견해 줄바꿈을 보완했습니다. 시각 점검에서는 표 행 generator가 마지막 값을 반복 표시하는 문제도 발견해 eager tuple 생성으로 수정하고 회귀 검사를 추가했습니다. 원래 JSON 계산값과 표시 오류는 구분하며 최종 HTML/Markdown을 다시 검사했습니다.

## 7. 적용과 잔여 범위

전체 소스로 기존 env/state/volumes를 보존하고 API/worker를 다시 빌드합니다. 먼저 `study plan`에서 순서를 확인하고 `study run kafka-outage --seed 42 --workload write-heavy --yes`로 실행합니다. 새 controller는 core gate를 먼저 수행하므로 기존 runtime-contract가 실패하는 상태에서 다음 장애를 실행하지 않습니다.

새 UI는 결과를 읽는 HTML이며 실시간 차트/고장 버튼/메시지 재처리 콘솔을 구현한 것이 아닙니다. 다중 호스트 HA·다중 worker rebalance·PITR·in-place upgrade·ES legacy 현대화·실제 엔진 인수 공백은 별도입니다. 이번 작업은 실험 비교와 판정의 정확성을 보강한 것입니다.

원본436개 파일은 삭제 없이 보존하며 신규10개로 최종446개입니다. 기존4개(루트/MVP README, manage.py, simulation.py)와 신규10개가 변경 범위입니다. 최종 ZIP CRC·각 파일 해시/권한·v7에 patch 적용한 결과 일치를 확인합니다. 정량 결과와 패키지 SHA-256은 증거의 final-summary.json/archive-integrity.json이 기준입니다.

가이드: `COMPARATIVE-STUDIES.md`. 주요 구현: `tools/measurement.py`, `tools/study_analysis.py`, `tools/study.py`. 직접 참조한 Python3.12 monotonic/JSON 문서는 가이드 끝에 있으며 실제 이미지 실행 증거로 사용하지 않습니다.
