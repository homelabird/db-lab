# DB Lab v10 — 전체 품질 분석과 개선 결과

**작성일: 2026-09-19 · 입력: db-lab-simulation-v9-ansible.zip · 작업: 별도 소스 사본**

입력 ZIP SHA-256: `3406e54009690df1524837e4ccf571b38bfe4d6670fd8fe578812e62996a6912`

## 1. 결론

현재 프로젝트의 강점은 단순 기능 수가 아니라 **원본 DB·이벤트·검색·캐시의 역할 분리**, 잘못된 구현과 보호된 구현의 비교,
복구 후 데이터 내용 대조, 위험한 작업의 동의·소유권 검사다. 공부용으로 유지할 가치가 크다.
반면 가장 큰 품질 공백은 **실제 엔진 인수 증거 부족**, 실행/입력 경계의 일부 검증 누락,
여러 버전으로 쌓인 문서와 제어 코드의 복잡도다. 테스트 개수만으로 이 공백을 해결했다고 판단하면 안 된다.

이번에는 시나리오를 늘리지 않았다. 기존 30개와 기본 6개 컨테이너를 유지하고,
HTTP·검색·로컬 상태 저장·Ansible 기본값을 수정하며 품질 검사와 문서 진입점을 통합했다.
**기존 1,493개는 원본에서 다시 통과했고, 수정본은 신규 73개를 포함한 1,566개 호스트 테스트를 통과했다.**
같은 재현기로 수정 전후 8가지 경계 조건도 대조했다. 실제 Docker·Ansible·Helm이 없는 환경이므로
이 결과는 DB 기동·복제·영속성·SSH 제어 완료 증거가 아니다.

사용자 서버·DB·원격 저장소에는 접속/배포하지 않았다. `.env`, engine pin, 장애 marker, 원본 데이터 볼륨을 삭제하거나 자동 변경하지 않았다.
결과는 전체 수정 ZIP, patch, 현재 보고서/가이드, 재현 및 테스트 증거다.

## 2. 검토 범위와 방법

입력 ZIP의 **467개 파일** 목록·해시·모드를 기준으로 삼았다. 모든 Python/셸/일반 YAML과 문서 링크를 정적으로 검사하고,
여섯 테스트 묶음을 실행했다. 주요 동작 경계인 루트 제어, MVP API/드라이버, 로컬 파일 처리,
Ansible 모듈·playbook, CI·인수시험 경로를 심층 검토했다.

모든 코드 행의 정확성을 형식적으로 증명한 감사는 아니다. 특히 SQL 격리·행 잠금, 이미지별 entrypoint,
Kubernetes scheduling/StorageClass, 실제 네트워크 및 디스크 장애는 실행할 엔진이 없어 확인하지 않았다.
일반 YAML 파싱, Go로 만든 제한된 chart 계약 시험, 실제 Helm 렌더링을 구분했다.

| 영역 | 입력 파일 수 | 확인한 장점 | 남은 품질 위험 |
|---|---:|---|---|
| 루트 파일·공통 scripts/tests | 21 | 명령 위임·실패 전달·별도 health·명시적 reset | 장시간 CLI 분기/일부 설정 파서 중복, 사본 간 전역 잠금 아님 |
| 독립 Elasticsearch | 101 | 비공개 기본값·장애 시나리오·데이터 대조 | 7.17 legacy, 실제 이미지/노드 장애 미검증 |
| 독립 Kafka | 28 | 생성 설정 검사·KRaft ID·소유권/삭제 보호 | provider·이미지 조합, 쿼럼/재연결 실제 검증 부족 |
| 독립 MariaDB HA | 95 | bootstrap 판단·복원 검사·부하 pacing | Galera 전체 중단/복구·실제 데이터 보존 미검증 |
| 독립 Redis | 70 | topology·기본 비공개 정책·복구 범위 명시 | 실제 Sentinel/Cluster 장애 및 비대칭 단절 미검증 |
| MVP | 91 | outbox·명시적 ACK/offset·전체 필드 대조·재현 실습 | 단일 relay, cache-aside 최신성 한계, 일부 경계 오류·CLI 복잡도 |
| Helm | 26 | release selector·Secret·probe·복구 보호 | 실제 Helm/Kubernetes·PVC·이미지 pull 검증 부족 |
| Ansible | 18 | 기존 controller 재사용·동의 분리·check/fetch 제한 | inventory 기본값 가림 수정 필요, 실제 Ansible/SSH 미검증 |
| docs/.github | 17 | 과거 증거 보관·수동 DB CI | 현재 진입점 불명확, 자동 호스트 품질 검사 부재 |

파일 수는 소스뿐 아니라 설정·테스트·역사적 보고서를 포함한다. 문서/설정/시험을 포함한 줄 수를 순수 운영 코드 LOC라고 표시하지 않았다.
원본 전체 목록은 `input-manifest.json`, 영역별 집계는 `area-inventory.json`에 있다.

## 3. 수정한 동작과 근거

P1/P2는 이 학습 프로젝트의 개선 순서다. CVSS 등급, 실제 침해나 운영 사고 판정이 아니다.

| ID | 우선순위 | 발견/개선 | 반영 위치 | 확인 수준 |
|---|---|---|---|---|
| Q01 | P1 | 중복 JSON/Content-Length, 잘못된 Unicode, 알 수 없는 요청 필드 조기 거부 | http_policy.py, api.py, core.py | 실제 loopback HTTP + 명시적 메모리 DB / 순수 입력 시험 |
| Q02 | P1 | 무제한 연결 스레드 대신 32개 수용 상한·초과 응답 | http_policy.py, api.py | 실제 loopback, 슬롯 반환/시작 실패 시험 |
| Q03 | P1 | ES의 부분 실패·timeout을 정상 빈 검색으로 표시하지 않음 | adapters.py | 실제 adapter + 모의 ES 응답 |
| Q04 | P1 | ES 3xx 응답의 다른 endpoint 자동 추적 차단 | adapters.py | 두 실제 loopback HTTP endpoint 간 재현 |
| Q05 | P1 | 상태/보고서/설정 경로 symlink 차단, 상태 JSON 원자적 교체 보강 | all.sh, tools/manage.py | 실제 임시 파일·셸·잠금 경로 시험 |
| Q06 | P1 | Ansible play 기본값의 inventory 가림 제거 | control.yml, collect.yml | 구조 검사·공식 우선순위 계약; 실제 play 실행은 대기 |
| Q07 | P2 | 호스트 테스트 의존성에 Requests 명시 | requirements-checks.txt, test-offline.sh | 의존성 계약 검사·전체 호스트 실행 |
| Q08 | P2 | 소스/호스트/실제 도구/배포 검사를 하나의 진입점으로 구분 | scripts/quality.py, quality.yml | 실제 로컬 검사기 + subprocess / 원격 CI 미실행 |
| Q09 | P2 | 현재 문서 경로·배포 manifest·버전 기록 구분 | README, docs/QUALITY-*, RELEASE-MANIFEST.json | 로컬 링크·파일 해시·배포물 대조 |

### Q01 — 모호한 입력과 정상 입력을 구분

기존 Python JSON 파서는 중복 키의 마지막 값을 사용한다. 이 기본값을 그대로 쓰면 사용자가 quantity를 두 번 보냈을 때
어떤 입력을 처리했는지 불명확하다. Python 공식 문서도 중복 이름/비표준 수의 기본 수용 및 사용자 정의 파싱을 설명한다.[S1]

수정본은 중복 키, NaN/Infinity, `1e999` 같은 float overflow, 잘못된 UTF-8·단독 surrogate를 거부한다.
중복 Host/Content-Length/Idempotency-Key, 지원하지 않는 Transfer-Encoding도 거부하며,
POST/PATCH의 허용 필드를 명시했다. **미정의 필드 거부는 새로 엄격하게 정한 계약**이며, 기존에 이를 모두 금지한다고
문서화했다가 위반한 보안 취약점이라고 과장하지 않는다. 일반 한글 입력·정상 재시도·버전 변경은 회귀 시험으로 보존했다.

### Q02 — 학습용 서버의 수용 한도

기존 ThreadingHTTPServer 사용 자체에는 애플리케이션 수용 상한이 없었다. 새 서버는 요청 스레드를 만들기 전에 슬롯을 확보한다.
기본 최대 32개이며 슬롯이 찬 상태에서 서버가 받아들인 새 연결은 503 `api_capacity_exceeded`, `Retry-After: 1`로 응답한다.
종료/예외 뒤 슬롯을 반환하며 무한 executor queue를 만들지 않는다.

이것은 DB TPS, 모든 진단 작업 수, 모든 DB 연결 수를 제한하는 기능이 아니다. TCP backlog 포화/호스트 자원 부족까지
항상 HTTP 503으로 응답한다고 보장하지 않는다. Python http.server는 공식 문서도 production 서버로 권장하지 않으므로,
여전히 loopback으로 공개하는 학습용 구성이다.[S2] 인증/TLS/운영 API 서버로 확장한 것은 아니다.

### Q03/Q04 — HTTP 성공 상태만으로 검색 성공을 판단하지 않음

ES는 일부 shard 실패나 timeout에서 부분 결과를 반환할 수 있다.[S3] 기존 코드는 hits만 읽어 정상 응답을 만들었다.
이제 `timed_out is False`와 정수형 `_shards.failed == 0`을 요구한다. 불완전한 metadata도 실패로 분류한다.
원본 DB에 주문이 있는데 검색이 일부 실패한 상황을 “주문 없음”으로 숨기지 않기 위한 계약이다.

Requests의 리다이렉트 기본 동작도 명시적으로 끈다.[S4] `Search.request`의 모든 호출에 `allow_redirects=False`를 넣고
3xx를 거부한다. 두 loopback endpoint로 확인한 결과 수정 전에는 두 번째 endpoint를 1회 호출했고, 수정 후에는 0회다.
실제 자격 증명이 외부로 유출된 사건을 재현한 것은 아니다. 사용자가 별도 프록시를 추가했다면 endpoint 설정을 직접 확인해야 한다.

### Q05 — 파일 경로와 상태 쓰기

원본에서 `.state`를 외부 임시 디렉터리로 연결하자 root/MVP의 잠금 파일이 그 대상으로 만들어졌다.
수정본은 설정·상태·주요 보고서 경로 및 관련 파일의 symlink를 검사하고, 잠금에 O_NOFOLLOW를 사용하며,
임시 JSON을 600 권한·배타 생성으로 작성한 뒤 fsync와 replace로 교체한다.
직렬화 실패 때 이전 JSON은 그대로이고 임시 파일은 정리된다.

정상 사용자 경로를 자동으로 이동/삭제하지 않는다. 이는 잘못된 링크·설정 사고를 방지하는 로컬 보호다.
권한 있는 관리자가 검사와 쓰기 사이 경로를 바꾸는 경쟁을 완전히 막는 보안 경계나,
DB fsync/호스트 전원 손실 내구성 검증은 아니다. 다른 사본/직접 Docker 작업의 전역 잠금도 아니다.

### Q06 — Ansible 설정의 실제 우선순위

기존 `control.yml`의 play vars가 `db_lab_request`, `target`, 동의값을 정의하고 있어 inventory 값보다 우선했다.
Ansible 문서상 play vars는 inventory/group_vars/host_vars보다 높다.[S5]
play vars를 제거하고 미설정 인수는 `default(omit)`으로 모듈 기본값을 사용하도록 했다.
`collect.yml`의 출력 디렉터리도 같은 방식으로 변경했다.

기존 inventory의 변경 동의 true도 이제 반영된다. 따라서 적용 전에 `--check`, `--limit`로 확인해야 한다.
실제 Ansible fixture에는 inventory 요청/동의와 정확히 세 파일의 fetch 검사를 추가해 **7개에서 10개**로 늘렸다.
그러나 이 환경에는 ansible-playbook이 없어 실제 10개를 실행한 것으로 집계하지 않았다.

## 4. 수정 전후 독립 재현

같은 `reproduce_quality.py`를 원본과 수정본에 각각 실행했다. HTTP/파일은 실제 로컬 I/O이고 DB는 명시적인 double이다.
정상 실행에 메모리 DB fallback을 추가한 것이 아니다.

| 입력/조건 | v9 관측 | v10 관측 |
|---|---|---|
| 같은 quantity 키 2개 | HTTP 201, 시험 저장소 주문 1개 생성 | HTTP 400, 생성 0개 |
| 오타 `quanity` 추가 | HTTP 201, 생성 1개 | HTTP 400, 생성 0개 |
| Content-Length 헤더 2개 | HTTP 201, 생성 1개 | HTTP 400, 생성 0개 |
| 단독 Unicode surrogate 상품명 | 업무 입력 함수 수용 | 400 입력 오류 |
| ES timed_out=true, failed shard=1 | 성공으로 처리 | 503 불완전 검색 |
| ES 302로 다른 loopback endpoint | 두 번째 endpoint 1회 호출 | 리다이렉트 거부, 호출 0회 |
| MVP .state symlink | 외부 경로에 잠금 생성 | 거부, 외부 파일 미생성 |
| root .state symlink | init 종료0, 외부 잠금 생성 | 종료1, 외부 파일 미생성 |

8건은 전체 보안 감사 건수나 실 DB 장애 횟수가 아니다. 같은 재현기의 관측값이며 `before.json`, `after-final.json`에 있다.

## 5. 전체 테스트와 커버리지

### 실제로 다시 실행한 호스트 테스트

| 묶음 | 원본 v9 | 수정 v10 | 결과 |
|---|---:|---:|---|
| root/Ansible 정책·보호·chart 부분 계약 | 177 | 206 | 통과 |
| MVP | 763 | 807 | 통과 |
| Elasticsearch | 77 | 77 | 통과 |
| Kafka | 94 | 94 | 통과 |
| MariaDB | 198 | 198 | 통과 |
| Redis | 184 | 184 | 통과 |
| **합계** | **1,493** | **1,566** | **실패·skip 없음** |

신규 73개는 HTTP/검색/파일 경계 44개, 루트 경로/품질 검사기/Ansible·CI 구조 29개다.
과거 보고서의 별도 19개 검사는 이번에 실행하지 않았고 합계에도 넣지 않았다.
커버리지 측정의 807개 재실행, 수정 전후 8개 probe, 압축 해제 재검사는 합계에 중복 집계하지 않는다.
정확한 단계별 명령/시간/종료 코드/로그는 `baseline/`, `final-offline/`, `archive-retest/`를 따른다.

### MVP 범위에서 새로 측정한 커버리지

coverage.py를 사용하여 `mvp_app`, `tools`의 실행문과 분기를 측정했다. 전체 저장소 커버리지가 아니다.

| 범위 | 실행문 | 분기 |
|---|---:|---:|
| MVP 앱+도구 전체 | **4,542/5,691 = 79.8%** | **1,390/1,912 = 72.7%** |
| 새 HTTP 경계 모듈 | 98.0% | 100.0% |
| API | 88.7% | 88.0% |
| DB adapters | 80.8% | 74.2% |
| manage.py | 82.4% | 71.9% |
| acceptance.py | 88.9% | 87.7% |
| advanced.py | 59.2% | 53.1% |
| messages.py | 51.4% | 42.9% |

분모는 실행 가능한 문장/분기이며 단순 줄 수가 아니다. 자식 프로세스는 이 coverage 세션에 자동 합산하지 않았으므로
낮은 수치가 곧 그 코드가 어떤 시험에서도 실행된 적 없다는 증명은 아니다. 반대로 높은 수치도 실제 DB의 동작·정확성을 증명하지 않는다.
장애/메시지 CLI의 취소·실패·정리 경계는 다음 실제 도구 시험과 함께 보완할 우선 영역이다.
근거는 `coverage/coverage.json`, 실행 로그와 정확한 명령이다. 임의의 높은 목표치를 맞추기 위해 제외 규칙을 추가하지 않았다.

### 정적·패키지 검사

Python AST, Bash 구문, 일반 YAML/workflow, 중복 YAML 키/테스트 메서드, 로컬 MD 링크를 검사했다.
Helm 템플릿은 일반 YAML 파싱에서 제외한다. 기존 두 DB 내장 checksum도 통과했고 MVP JS는 Node 구문 검사를 통과했다.
최종 분류별 개수는 `static-final/summary.json`에 기록한다. 타입 검사·ShellCheck·취약점 스캔·브라우저 통합 검사를 수행한 것으로 표시하지 않는다.

배포물은 원본 ZIP 전체 목록에서 시작해 기존 보고서도 보존한다. `RELEASE-MANIFEST.json`은 파일 해시/권한 대조용이며 서명이 아니다.
ZIP CRC, 재해제 파일 대조, patch 적용 결과를 `package-integrity.json`에 기록한다.

## 6. 실제 도구/런타임 상태

| 검사 | 이번 실제 결과 | 해석 |
|---|---|---|
| mvp init 2회 | 종료0/0, .env 해시 동일 | 로컬 초기화 보존 |
| mvp doctor | 종료1 | 엔진 부재 |
| mvp verify run core --yes | **blocked / 종료127** | 실제 core 시나리오 **0/1 통과** |
| 실제 Ansible 검사 | **blocked / 종료127** | 실제 playbook 0개 실행; 10개 fixture는 실행 대기 |
| 실제 Helm 검사 | **blocked / 종료127** | 실제 렌더링 미실행 |
| GitHub quality/runtime workflow | 파일·구조 검사만 수행 | 원격 job·artifact 업로드 미실행 |
| SSH/DB/다중 호스트 | 미접속·미실행 | 실제 장애 복구·HA·영속성 인증 없음 |

호스트는 Python 3.13.5, Linux x86_64다. 기존 보고서의 Python -S shim을 복사해 쓰지 않고 현재 환경에서 실행했다.
Docker/Podman/Helm/kubectl/ansible-playbook/ShellCheck, native DB 서버 및 세 DB 드라이버가 없다.
Requests/PyYAML/jsonschema/coverage는 설치된 상태다. 외부 패키지 접속은 DNS 오류로 실패했고 실제 설치 성공을 주장하지 않는다.
앱의 Python 3.12 이미지 빌드와 pin된 드라이버 설치는 별도 인수 대상이다.

## 7. 아직 아쉬운 점과 다음 완료 조건

### 가장 먼저: 실제 core 및 실제 Ansible 인수

가장 큰 미완료는 새 시나리오 부족이 아니라 실제 엔진 결과다. 준비된 로컬 Docker에서 core를 통과한 뒤
transactions/messages/recovery를 순서대로 실행해야 한다. 실제 Ansible fixture 10개 → `--check` → 한 호스트 doctor/core →
실제 fetch 결과 일치도 확인해야 한다. native 결과가 없으면 CLI/CI 코드를 더 만들었다고 완료 처리하지 않는다.

### 유지보수: 긴 제어 함수와 중복 계약

100줄 이상 함수가 control 정책, renderer, simulator, 관리 CLI 등에 남아 있다. 현재는 행 수 경고로 노출하고,
API의 HTTP 경계만 별도 작은 모듈로 분리했다. 전체 기능을 한 번에 재작성하거나 새 프레임워크를 도입하지 않았다.
다음 분리는 command parse/plan, 대상 검증, 실행, 증거/복구를 작은 함수로 나누되 기존 양성·음성 테스트와 실제 runtime 결과를 먼저 확보해야 한다.
특히 CLI/Ansible의 시나리오 명단 중복은 registry 일치 검사를 강화한 뒤 공유 데이터로 옮길 대상이다.

### 재현성·이미지·보안 범위

직접 Python 의존성은 pin되어 있으나 전이 의존성 전체 lock, 이미지 digest/서명, 자동 CVE 검증을 완성한 상태는 아니다.
이번에 사용자가 의도하지 않은 이미지 업그레이드를 수행하지 않았다.
ES 7.17.x는 공식적으로 2026-01-15 지원이 종료된 계열이다.[S6] legacy 프로필을 격리해 유지하고,
새 버전은 별도 볼륨/호환성 시험으로 분리해야 한다. 특정 CVE 악용이나 실제 침해를 확인한 것은 아니다.
Python http.server 기반 API도 공개 서비스로 전환하지 않는다. 비밀번호/인증서 자동 회전은 별도 과제다.

### 데이터·HA와 실습 부하

단일 호스트 MVP는 물리 장애 도메인 독립성을 증명하지 않는다. Redis cache-aside는 선형적 최신 읽기를 보장하지 않으며,
단일 outbox relay를 그대로 여러 개 띄우는 것을 지원하지 않는다. 물리 백업/PITR, in-place 업그레이드·다운그레이드,
다중 worker rebalance와 실제 Galera/Redis HA는 별도 시험이다.
기존 실습의 시간·메모리 한도는 안전 예산이지 현장 용량 인증이 아니다. 모든 실습을 동시에 켜기보다 한 범위씩 실행한다.

### 프로젝트 공개·사용성

역사적 보고서는 보존하되 첫 화면을 현재 가이드로 바꾸었다. 현재 품질/지원 상태가 과거 버전 숫자에 묻히지 않게 했다.
배포물에 최상위 LICENSE 파일은 확인되지 않았다. 공개 배포 시 유지보수자가 프로젝트의 의도한 라이선스와 포함 파일의 출처를
정리해야 하며, 이번 작업이 임의로 라이선스를 부여하거나 법적 적합성을 인증한 것은 아니다.

## 8. 적용 순서와 호환성 주의

1. 새 ZIP을 별도 폴더에 해제하고 release 검사를 수행한다. 기존 설정/상태/데이터를 버리지 않는다.
2. `scripts/quality.py offline`, 준비된 실제 도구의 `tools`를 구분해 실행한다.
3. 보존 중인 messages 자원은 기존 가이드대로 API 교체 전에 정리한다. 전체 앱 소스를 바꿨으므로 API/worker 이미지를 재빌드한다.
4. v10에서 추가 필드·중복 헤더·symlink 경로·ES 리다이렉트가 명시적으로 거부되는 점을 확인한다.
5. Ansible inventory의 요청과 동의값은 이제 정상 적용되므로 `--check`와 `--limit`부터 실행한다.
6. 실제 core를 먼저 통과시키고, 그 증거를 확보한 뒤 더 큰 장애 범위로 진행한다.

구체 명령은 [QUALITY-GUIDE.md](QUALITY-GUIDE.md)에 있다. 일반 down은 계속 볼륨 보존이며 자동 reset은 추가하지 않았다.

## 9. 제작 중 실패와 조치

첫 신규 시험 호출은 작업 디렉터리 오류로 파일 작성/검색이 실패했고 0개 시험이었다. 올바른 루트에서 다시 실행했다.
새 파일 시험 1개는 `parse_env` 호출 인수 누락으로 TypeError가 발생해 fixture를 고쳤다. 코드 기준을 완화하지 않았다.
첫 전체 MVP 실행은 807개 중 6개 모의 ES 시험이 metadata 누락으로 거절됐다.
그 fake 서버를 실제 정상 ES 응답처럼 `timed_out=false`, `_shards.failed=0`을 반환하도록 보완하고 재실행했다.
부분 실패를 허용하도록 production 검사를 다시 느슨하게 바꾸지 않았다. 최초/최종 로그를 별도로 보존한다.

새 CI 작성 중 hidden directory 아래 결과 업로드의 기본 제외 정책을 확인해, 명시한 세 파일만 대상으로
`include-hidden-files: true`를 넣었다.[S7] 원격 업로드를 실제로 관측한 것은 아니다.

## 10. 증거와 공식 참고

- `baseline/summary.json`: 원본 1,493개 실제 재실행.
- `final-offline/summary.json`: 새 공통 진입점으로 전체 1,566개 실행.
- `before.json`, `after-final.json`, `reproduce_quality.py`: 동일 8개 재현.
- `coverage/`: scope/명령/분기·문장 커버리지, 807개 재실행 로그.
- `actual-tools/`, `runtime-checks.json`: 실제 도구/엔진 부재의 blocked 결과.
- `static-final/`, `additional-checks.json`: 구문·문서·기존 체크섬.
- `archive-retest/`, `package-integrity.json`, `source-diff.json`: 배포물 검증.

공식 동작 계약 확인일: 2026-09-19. 문서 확인은 고정 이미지 실기동 증거가 아니다.

[S1] Python 3.12 JSON: https://docs.python.org/3.12/library/json.html

[S2] Python 3.12 http.server: https://docs.python.org/3.12/library/http.server.html

[S3] Elasticsearch 7.17 Search API: https://www.elastic.co/guide/en/elasticsearch/reference/7.17/search-search.html

[S4] Requests redirect/timeout: https://docs.python-requests.org/en/latest/user/quickstart/

[S5] Ansible 변수 우선순위: https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_variables.html

[S6] Elastic EOL: https://www.elastic.co/support/eol

[S7] GitHub upload-artifact: https://github.com/actions/upload-artifact
