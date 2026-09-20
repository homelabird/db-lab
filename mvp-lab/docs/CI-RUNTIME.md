# v7 — 기동 준비 상태, Kafka 처리 완료 확인, 수동 CI와 공유용 증거

기준일: 2026-09-19. **CI 파일을 추가한 것과 실제 GitHub/Docker에서 통과한 것은 다릅니다.** 이번 제작 환경에는 Docker가 없었으며 아래 명령의 실제 DB 실행은 아직 검증 대기입니다. 기존 30개 시나리오와 기본 6개 컨테이너는 유지합니다.

## 1. 이미 실습 중인 환경에서 적용

전체 소스를 반영하고 기존 `.env`, `.state`, 데이터 볼륨을 보존합니다. `all.sh`만 바꾸지 않습니다. `messages --keep`으로 보존한 자원은 기존 API 컨테이너가 살아 있을 때 먼저 확인·정리합니다. 활성 장애 marker가 있다면 해당 recover 명령으로 조사하고 복원한 뒤 진행합니다.

```bash
bash ./all.sh mvp doctor
bash ./all.sh mvp up
bash ./all.sh mvp inspect-runtime
bash ./all.sh mvp verify run core --yes
```

새 설치는 위 명령 전에 `bash ./all.sh mvp init`을 실행합니다. 이전 버전에서 엔진 pin이 없다면 기존 가이드의 최초 대상 확인·bind 절차를 따르고, 이미 존재하는 pin을 삭제해 우회하지 않습니다.

`mvp up`은 기존처럼 이미지 빌드·기동과 앱 초기화를 수행합니다. 로컬 Docker에서는 초기화 성공 이후에도 실제 컨테이너 준비 상태를 확인합니다. 초기화 한 번이 성공하면 건강 상태를 기다리는 동안 DDL을 반복하지 않습니다. 제한시간 실패 시 컨테이너와 볼륨을 남기며 아래에 결과를 기록합니다.

```text
mvp-lab/reports/startup/startup-<실행ID>.json
```

초기화 timeout은 제한된 시간 안에서 다시 시도하지만, 실패한 데이터베이스를 초기화/삭제하여 통과시키지 않습니다. 명령 예산은 서버 전체 작업·SIGKILL 이후 복구를 보장하는 hard watchdog이 아닙니다.

## 2. 준비 상태와 업무 성공 구분

`inspect-runtime`은 엔진 pin과 Compose 프로젝트·서비스 소유권을 확인하고, 컨테이너 상태·건강 상태·재시작 수·종료 코드·OOM 표지만 읽습니다. 로컬 Docker만 지원하며 실제 DB를 생성·수정하지 않습니다.

| 관측 | 해석 |
|---|---|
| missing | 컨테이너가 발견되지 않음 |
| ambiguous | 같은 서비스에 여러 후보가 있어 단일 대상으로 판정하지 않음 |
| starting / unhealthy | healthcheck가 준비됨을 확인하지 못함 |
| observation_timeout / observation_failed | 상태 관측 실패; DB 장애 원인을 확정한 것 아님 |
| oom_killed=true | Docker가 보고한 OOM 표시; 이것만으로 원인 분석 전체를 끝내지 않음 |
| ready=true | 프로세스/healthcheck 준비 조건. 주문 저장·검색·복구·내구성 증명 아님 |

worker에는 별도 healthcheck가 없으므로 worker의 ready는 프로세스 실행 상태만 뜻합니다. 실제 전체 경로는 `verify run core --yes`로 확인합니다. 기존 Podman/원격 Docker의 일반 초기화 경로는 유지하되 `admin_only_non_local_docker_not_runtime_certified`로 구분합니다. 이 경로에 로컬 Docker 자동 실습/CI 지원을 확대한 것은 아닙니다.

준비되지 않은 컨테이너에 한해 최근 로그에서 고정된 오류 패턴만 분류합니다. 출력은 `permission_denied`, `storage_full`, `authentication_rejected`, `connection_refused`, `configuration_rejected`, `memory_allocation_failed`, `topic_unavailable` 같은 이름뿐입니다. 원문 로그·환경 변수·healthcheck 출력은 보고서에 넣지 않습니다. 패턴은 조사 단서이지 DB가 확정한 원인 판정이 아닙니다.

## 3. Kafka commit 응답 확인

일반 worker와 메시지 실습은 동기 commit 결과가 다음 조건에 맞을 때만 처리 완료로 인정합니다.

- 결과가 정확히 하나의 TopicPartition을 가진 리스트이고 개별 오류가 없음.
- topic과 partition이 처리한 메시지와 같음.
- 반환 offset이 처리한 메시지 offset+1과 정확히 같음.

빈 리스트/None/다른 topic/다른 partition/잘못된 offset은 `kafka_checkpoint_unconfirmed`로 실패합니다. 기존 재시도 경로가 사용되며 임의 offset reset이나 조용한 건너뛰기는 없습니다. DB·Kafka·ES 전체를 하나의 트랜잭션으로 바꾼 것이 아니고 중복 재전달 가능성도 남습니다. 실제 드라이버·브로커에서 이 경로를 인수해야 합니다.

## 4. 기존 실행을 공유용으로 내보내기

```bash
# 출력에 나온 실제 accept-... 값을 사용
bash ./all.sh mvp verify export accept-실제12자리ID
```

출력 위치는 `mvp-lab/reports/share/accept-<12자리ID>/`입니다. `summary.json`, `report.md`, `junit.xml` 세 파일만 생성합니다. 이미 존재하는 출력 디렉터리는 덮어쓰지 않습니다.

허용된 단계 이름·상태·숫자 코드·고정된 조사 안내만 추출합니다. 원본 `.env`, 접속 대상, 주문·payload, 자유 형식 오류, stdout/stderr, 장애 marker는 복사하지 않습니다. 원본 summary 해시를 포함하지만 암호학적 서명이나 독립적인 DB 재검증은 아닙니다. 원본 상세 증거는 로컬에 그대로 둡니다. `candidate` 범위의 외부 이미지 참조는 현재 공유 export에서 받지 않습니다.

이 명령은 파일 생성만 하며 GitHub·웹·이메일로 전송하지 않습니다. 허용된 값만 추출하는 기능을 임의 폴더 전체의 비밀정보 검사기라고 해석하지 않습니다.

## 5. GitHub Actions 수동 CI

프로젝트에 `.github/workflows/mvp-runtime.yml`을 포함했습니다. **사용자의 원격 저장소에 업로드하거나 workflow를 실행한 것은 아닙니다.** 저장소의 기본 브랜치에 파일을 반영한 뒤 GitHub Actions의 `MVP live runtime acceptance`를 선택하고 `Run workflow`에서 suite와 명시적 동의를 선택합니다. 수동 workflow는 기본 브랜치에 있어야 실행 메뉴에서 사용할 수 있습니다.[S3]

| suite | 실제 실행에 포함하는 범위 |
|---|---|
| core | 대상/API/worker 계약 + smoke + baseline |
| transactions | 계약/smoke + 재고·결제·환불 비교 6개 |
| messages | 계약/smoke + 메시지 오류·DLQ·재처리 6개 |
| recovery | 계약/smoke + 논리 백업 복원·deadlock |

기본값은 core입니다. CI에서는 메모리 고갈·네트워크 조작·후보 버전 변경을 자동으로 선택하지 않습니다. 기존 로컬 `verify`의 다른 범위는 유지합니다.

흐름은 다음과 같습니다.

```text
명시적 동의 → 전체 호스트 검사 → 새 hosted Linux runner
 → 새 설정·고유 프로젝트 → 환경 검사 → 실제 이미지 빌드/기동
 → 선택한 verify → 실패 포함 읽기 전용 진단 → 일반 down
 → 허용 필드로 만든 결과만 artifact 업로드
```

push, pull_request, schedule 자동 실행은 없습니다. 저장소 권한은 contents:read이며 checkout 자격 증명을 유지하지 않습니다. 동의하지 않은 실행은 모든 job이 skipped인 성공처럼 보이지 않게 첫 단계에서 거부합니다. runner 시간/이미지 다운로드/저장 용량이 사용되며 사용자 계정의 요금·할당량·권한을 확인해야 합니다.

실패해도 `artifacts/mvp-runtime/`만 artifact 대상으로 사용합니다. 원시 stdout은 `artifacts/private/`에 로컬로 남고 업로드 경로에 포함하지 않습니다. 원시 stderr는 보관하지 않고 바이트 수만 기록하는 기존 execute 계약을 유지합니다. 업로드 보존 기간은 workflow 설정상 7일입니다. checkout/upload-artifact는 major `@v4` 태그이며 전체 SHA 고정·서명·CVE 검증을 완료한 구성은 아닙니다.

## 6. 별도 폐기용 로컬 VM에서 CI runner 직접 실행

**기존 실습 폴더가 아닌 새 checkout/새 압축 해제 디렉터리에서만** 다음을 실행합니다. 먼저 init/up을 실행하지 않습니다.

```bash
python3 scripts/ci-mvp.py --suite core --allow-disposable-runner --yes
```

`.env`, `.state`, `mvp-lab/reports`가 이미 있으면 거부합니다. 원격 endpoint, self-hosted GitHub runner, 기존 공개 산출물도 거부합니다. Linux Docker/Compose 및 디스크 여유 8GiB·Docker 보고 RAM 6GiB 이상을 검사합니다. 이는 현재 여유 RAM이나 실행 성공률 인증이 아닙니다.

이 CI 전용 명령만 명시적 동의 아래 `up --build` 경로를 호출합니다. 기존 `verify` 자체에 자동 pull/build/up을 넣지 않았습니다. 고유 프로젝트 이름을 만들고 같은 label 자원이 없는지도 확인합니다. 빌드 실패로 일부 자원이 생겨도 원본을 추측해 정리하지 않습니다.

기본 성공 시 일반 `down`으로 컨테이너와 네트워크를 종료하고 named volume은 보존합니다. volume 삭제·prune은 하지 않습니다. 로컬 VM을 폐기하지 않는다면 볼륨이 남으므로 이를 알고 사용하세요. 활성 장애 marker가 있으면 down도 기존 보호 규칙에 따라 실패하며 전체 pipeline을 성공으로 바꾸지 않습니다. hosted runner의 수명 종료가 장애 복구의 성공 증거가 되는 것도 아닙니다. 강제 종료 후 cleanup은 보장하지 않습니다.

## 7. 결과 읽기

`artifacts/mvp-runtime/summary.json`은 pipeline 상태와 `live_acceptance_passed`를 분리합니다. DB 실습은 통과했지만 종료에 실패하면 pipeline은 failed가 될 수 있습니다. 입구에서 도구가 없으면 blocked/127이고 이후 단계는 not_run입니다. `acceptance/` 하위에는 verify가 실제 결과를 남긴 경우에만 공유용 결과가 있습니다.

GitHub 결과가 없는 현재 배포를 actual-runtime-passed로 표시하지 않습니다. 직접 실행한 검사와 실제 DB 인수의 범위는 [v7 보고서](SIMULATION-V7-REPORT.md)를 따릅니다.

## 공식 계약 참고

[S1] Docker Compose startup/readiness: https://docs.docker.com/compose/how-tos/startup-order/

[S2] Confluent Python 동기 commit 및 message offset+1: https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html
고정 라이브러리 소스: https://raw.githubusercontent.com/confluentinc/confluent-kafka-python/v2.8.2/src/confluent_kafka/src/Consumer.c

[S3] GitHub 수동 workflow 실행: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow

[S4] 공식 upload-artifact 설정: https://raw.githubusercontent.com/actions/upload-artifact/v4/README.md

문서 확인일 2026-09-19. 공식 계약 조회는 실제 컨테이너·GitHub workflow 실행 증거가 아닙니다.
