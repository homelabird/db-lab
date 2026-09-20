# DB 시뮬레이션 v7 — 기동 신뢰성·checkpoint·수동 CI·증거 공유

**작성일 2026-09-19 · 입력 db-lab-simulation-v6.zip · 출력 db-lab-simulation-v7.zip**

## 1. 결과 요약

이번에는 시나리오를 늘리지 않고 기존 30개/기본 6개 컨테이너를 유지했습니다. 실제 실행 경로를 연결하기 위해 수동 GitHub Actions workflow와 새 폐기용 checkout 전용 CI runner를 추가하고, 기동 준비 상태 및 Kafka 처리 완료 응답 검사를 강화했습니다. 원본 ZIP과 사용자 호스트·DB·원격 저장소는 변경하지 않았습니다.

**전체 호스트 테스트 1,302개와 별도 독립 재검사 19개가 통과했습니다. 실제 Docker/DB/GitHub Actions 실행은 완료하지 못했습니다.** 실제 CLI 호출에서 Docker가 없어 CI pipeline과 verify core가 blocked/127로 끝났고 실제 통과한 core 시나리오는 0/1입니다. 그 차단 결과 자체를 공유 export에서도 보존하는지 확인했습니다.

## 2. 실제 변경

| 영역 | 변경 | 검증/한계 |
|---|---|---|
| 기동 | admin init 성공 뒤 로컬 Docker의 DB/API health와 worker 실행 상태 대기 | 실제 함수+가짜 시계/명령/inspect, 초기화 timeout·준비 지연·실패 잔존 검사. 실제 DB 기동 아님 |
| 관측 | inspect-runtime, 고정된 컨테이너 상태·OOM/종료/재시작 수·오류 신호 | 가짜 Docker 결과에서 소유권·민감 출력 차단. read-only이며 원인 확정/업무 성공 아님 |
| Kafka | 동기 commit 응답의 정확한 topic/partition/offset+1 확인 | 기존 v6와 v7에 같은 double을 넣어 빈/None/다른topic/미래offset의 수용→거부 확인 |
| 증거 | verify export, 허용 필드만 JSON/Markdown/JUnit으로 복사 | 순수 파일 시험+실제 blocked 결과 export. 독립 DB 재검증/자동 업로드 아님 |
| CI | 수동 workflow + fresh runner의 init/doctor/build/up/verify/down | engine/subprocess double, YAML 구조, 실제 Docker 부재 차단. 원격 GitHub 미실행 |

### 기동 계약

기존 wait_initialized는 admin init 종료0이면 바로 반환했습니다. v7 로컬 Docker 경로는 초기화 성공과 컨테이너 준비를 따로 확인합니다. 초기화에 성공한 뒤에는 상태 대기를 위해 초기화를 반복하지 않습니다. deadline 초과는 실패로 기록하고 생성된 컨테이너/볼륨은 보존합니다. worker의 별도 healthcheck가 없어 worker 준비는 프로세스 실행만 뜻하며 실제 경로는 verify/smoke에서 판정합니다.

Podman/원격 Docker의 기존 일반 admin 초기화 경로는 호환성을 위해 유지하면서 admin_only_non_local_docker_not_runtime_certified로 표시합니다. 새 진단/자동 실습/CI는 로컬 Docker만 허용합니다. 일반 초기화의 유지가 provider 실제 검증을 뜻하지 않습니다.

### Kafka 계약

이전 acknowledge는 명시적 partition 오류가 없으면 빈 응답·None·다른 topic·너무 큰 offset도 반환을 허용했습니다. v7은 정확히 한 개의 matching TopicPartition과 message.offset()+1만 인정하고 나머지는 kafka_checkpoint_unconfirmed로 실패합니다. 안전한 로그 코드만 추가하고 기존 재전달 경로를 유지합니다. 실제 Kafka가 잘못된 응답을 반환한 사고를 관측한 것이 아니라, 모의 응답을 처리하는 검증의 빈틈을 확인한 것입니다.

### 공유 증거

원본 summary의 실행 ID·계획 단계·결과를 검사한 뒤 고정 필드만 복사합니다. 단계 failed가 남은 상태를 전체 passed로 export하지 않습니다. 원본 해시는 포함하지만 서명·감사 독립성은 없습니다. 소스 코드 해시/런타임 증거 자체를 새로 측정하지 않고 기존 실행 기록을 투영합니다. candidate 공개 export는 아직 지원하지 않습니다.

CI 공개 산출물은 artifacts/mvp-runtime만이고 raw stdout은 artifacts/private에 남깁니다. stderr는 기존 execute의 바이트 수만 보존하는 계약을 유지합니다. 공유물에 .env/연결URL/원문주문/자유형식오류/복구marker를 넣지 않습니다. 이것은 arbitrary 파일 전체를 안전하다고 보증하는 민감정보 스캐너가 아닙니다.

## 3. 수동 CI 경계

workflow_dispatch만 허용하고 push/PR/schedule 실행은 없습니다. suite 선택은 core/transactions/messages/recovery만 허용하며 권한은 contents:read, checkout 자격 증명 비보존, 기본 core와 명시적 동의를 사용합니다. 동의 누락은 첫 job에서 실패합니다. 호스트 전체 검사 뒤 별도 hosted Ubuntu runner에서 실제 이미지 빌드와 해당 verify를 요청하는 코드입니다.

새 checkout 안의 mvp-lab/.env/.state/reports가 있거나 self-hosted/원격 engine이면 거부합니다. 디스크 여유 8GiB와 Docker 보고 Linux/RAM6GiB를 검사하지만 여유 메모리나 플랫폼 호환성 인증이 아닙니다. 별도 고유 프로젝트와 기존 동일label 자원 부재를 확인합니다. 파일 시스템/원격의 독립 강한 소유권 인증은 아닙니다.

up이 일부 자원을 만들다 실패할 수도 있으므로 이후 읽기 전용 상태를 모으고 일반 down을 시도합니다. 활성 fault marker가 있으면 정리도 기존 보호장치에 따라 실패합니다. marker를 지우거나 volume/prune으로 통과시키지 않습니다. shutdown 실패는 전체 pipeline passed를 허용하지 않습니다. 로컬 disposable runner에서는 일반 down 뒤 named volume이 남고, VM을 계속 쓰면 잔존 자원을 운영자가 확인해야 합니다.

GitHub 원격 저장소에 workflow를 추가하거나 실행한 것은 아닙니다. actions의 @v4 태그와 DB 이미지 태그·전이 의존성을 모두 SHA/digest 고정한 것은 아닙니다. 실제 호스팅 비용·계정 권한·runner 할당량도 확인하지 않았습니다.

## 4. 이번에 실행한 검사

| 묶음 | 통과 | 비고 |
|---|---:|---|
| root | 91 | 기존 제한된 Go template 계약; 실제 Helm 아님 |
| MVP | 658 | v6 575 + 신규83 |
| Elasticsearch | 77 | 기존 호스트 검사 |
| Kafka | 94 | 기존 호스트 검사 |
| MariaDB | 198 | 기존 호스트/SQLite 관련 검사 |
| Redis | 184 | 기존 호스트 검사 |
| **전체** | **1,302** | 실패·skip 없음 |
| 독립 V01~V08 및 기존 흐름 | **19** | 별도 집계, 기존 검사 코드 유지 |

신규83개는 startup/초기화28, checkpoint13, 공유증거18, CI/워크플로24입니다. Docker·Kafka 응답은 명시적 double, 파일/JUnit 생성은 실제 로컬 I/O입니다. 기존 loopback HTTP와 SQLite SQL·스케줄 어댑터 범위도 그대로 유지합니다. 테스트 수와 subtest/재실행은 합산하지 않습니다.

정적 검사: Python120개, Bash58개(그중POSIX sh5 추가검사), 일반 YAML 및 workflow25개, JS1개. Helm 템플릿은 일반 YAML에서 제외했습니다. Workflow는 YAML·구조 계약 검사이지 actionlint/실제 GitHub 검증이 아닙니다. MariaDB SHA256SUMS·Redis MANIFEST.sha256도 통과했습니다.

### 실제 CLI 관측

| 호출 | 관측 |
|---|---|
| ci runner 동의 없음 | 종료2, 실행 거부 |
| 새 checkout의 ci runner 동의 있음 | **종료127 / blocked / live_acceptance_passed=false** |
| init 두 번 | 종료0/0, 기존 env 바이트 일치 |
| 목록/verify plan core/all | 종료0; 시나리오 범위 유지 |
| doctor / inspect-runtime | 종료1, Docker 없음 |
| verify run core --yes | **종료127 / blocked / core 시나리오0/1** |
| 그 blocked 실행의 verify export | 종료0, export도 blocked; 실제 DB PASS로 변환 안 됨 |

새 실행 사본에서 호출했으며 생성된 env/engine pin/장애 marker는 배포 ZIP에 넣지 않습니다. CI 차단 공개 예시와 verify 차단 공개 예시는 실제 CLI 결과이고, 통과 예시가 아닙니다.

### 초기 실패와 수정 기록

첫 전체 v7 실행(신규80개 단계)의 MVP는 655개 중 메시지 시나리오12개에서 오류가 발생했습니다. 가짜 Reader.commit이 error만 반환하고 실제 TopicPartition의 topic/partition/offset을 반환하지 않아 새 엄격 기준에 거절된 것입니다. 가짜 응답을 실제 계약 형태로 수정한 뒤 메시지16개 및 최종658개가 통과했습니다. 일반 worker의 검증 기준은 완화하지 않았습니다. 초기 실패 로그를 보존합니다.

이후 workflow 동의 누락 거부1개와 원격/Podman의 기존 초기화 범위 보존2개를 추가해 신규83개/전체1302개가 됐습니다. 앞선 부분 검사80·81개, 메시지 재실행16개, 최종 ZIP 재실행은 전체 합계에 더하지 않습니다.

패키징 첫 대조에서는 기준 사본에 재현 시험이 만든 bytecode 6개가 섞여 파일 삭제로 오인할 수 있었습니다. 원본 ZIP의 파일 목록을 기준으로 대조하도록 수정했습니다. 그 직후의 압축 해제 시험 최초 호출은 아직 ZIP이 없어 실행되지 않았으며 별도 로그로 보존합니다. 최종 패키지는 원본 ZIP 내용과 비교하고 새로 실행한 archive-retest만 집계합니다.

## 5. 환경과 미검증

호스트 Python3.13.5에서 -S와 명시한 site-packages 및 python3 shim으로 시작 hook 영향을 배제했습니다. 실제 앱 Python3.12 이미지 빌드/설치 호환성은 확인하지 못했습니다. Docker/Podman/Helm/kubectl/native DB 서버가 없고 외부 패키지 저장소 DNS도 실패했습니다. 다른 호스트에 임의로 연결하거나 프로젝트를 원격 배포하지 않았습니다.

실제 컨테이너 기동/healthcheck, confluent-kafka2.8.2 commit 반환, 실제 DB 연동·잠금·복구, 이미지 entrypoint와 아키텍처, GitHub runner의 네트워크/용량/권한/timeout, 실제 artifact 업로드·수명 종료는 미검증입니다. CI 코드가 있다고 이 공백을 해결한 결과로 쓰지 않습니다.

다중 호스트 HA·physical backup/PITR·in-place upgrade·메시지 재처리 웹UI·ES legacy 현대화도 이번에 완료하지 않았습니다. 현재 가장 중요한 잔여 인수는 로컬 또는 명시적 hosted runner에서 core를 실제로 통과시키고 단계별 증거를 확인하는 것입니다.

## 6. 산출물

기존 소스 파일은 삭제 없이 보존합니다. 기존 네 DB별 실습과 Helm 서비스 코드는 바꾸지 않습니다. 주요 변경은 worker acknowledge, manage 초기화/진단, acceptance export와 새 tools/startup.py, tools/evidence_export.py, scripts/ci-mvp.py, .github/workflows/mvp-runtime.yml입니다. 세부 변경 파일·해시·권한은 증거의 archive-integrity.json/source-diff.json을 기준으로 합니다.

실행 가이드: CI-RUNTIME.md. 증거 묶음에는 baseline, 초기 full 실패, final-full, 독립19개, commit 전후 재현, 실제 CLI 차단 예시, 정적/체크섬 검사, archive-retest와 패치 일치 검사가 포함됩니다. 문서 출처는 가이드 [S1]~[S4]입니다. 문서 조회는 실제 이미지/DB/CI 실행 증거가 아닙니다.
