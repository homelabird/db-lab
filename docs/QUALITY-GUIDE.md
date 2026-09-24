# v10 — 프로젝트 품질 검사와 적용 순서

기준일: 2026-09-19. 시나리오를 더 늘리지 않고 입력·검색·로컬 파일·Ansible 설정·검증 진입점을 정리한 버전입니다.
**호스트 테스트 통과, 실제 Ansible 실행, 실제 DB 인수시험은 서로 다른 결과입니다.**
전체 분석과 이번에 측정한 결과는 [품질 보고서](QUALITY-REPORT.md)를 참고하세요.

## 1. 처음 사용하는 사람의 경로

전체 ZIP을 별도 폴더에 해제합니다. 기존 `.env`, `.state`, engine pin, 데이터 볼륨을 보존합니다.
원래 사용하던 동일 계정으로 실행하고, 문제를 숨기기 위해 pin/marker/볼륨을 지우지 않습니다.
MVP는 데이터·컨테이너가 분리된 6개 스택이며, 기본 `all.sh up/down/restart` 배치에는 네 HA 실습과 함께 포함됩니다. 합산 자원 사용량을 확인하고, 일부만 실행할 때는 대상 이름을 지정하세요.

```bash
# 프로젝트 루트. Python 3.12/3.13과 Bash, Go가 있는 별도 검사 환경
python3 -m venv .venv-quality
. .venv-quality/bin/activate
python -m pip install -r scripts/requirements-checks.txt

# DB 변경 없는 소스 검사
python scripts/quality.py static

# 여섯 호스트 테스트 묶음. 테스트 안의 명시적 double/SQLite 어댑터를 사용
python scripts/quality.py offline
```

`static`은 Python AST·Bash 구문·일반 YAML·중복 YAML 키·덮어쓴 test 메서드·로컬 문서 링크를 검사합니다.
타입 검사, 보안 취약점 스캔, Compose 스키마 검사, 실제 Helm 렌더링과 같지 않습니다.
100줄 이상 함수는 개선 후보로 표시할 뿐 자동으로 품질 불합격 점수를 매기지 않습니다.

`offline`은 기존 root/MVP/Elasticsearch/Kafka/MariaDB/Redis 테스트를 모두 실행합니다.
이 단계에서 스택을 기동하거나 원격 SSH를 실행하지 않습니다. 임시 파일과 loopback HTTP 소켓은 사용합니다.
Go는 기존 제한된 chart 계약 검사에 필요하며, 이것도 실제 Helm 실행은 아닙니다.
테스트 의존성은 `scripts/requirements-checks.txt`에 모았고 Requests도 명시했습니다.

## 2. 실제 도구 검사와 DB 인수시험

```bash
# Ansible 자체를 설치한 제어기에서. Helm은 별도로 준비된 바이너리를 사용합니다.
python -m pip install -r ansible/requirements.txt
python scripts/quality.py tools

# 소스/호스트/실제 Ansible fixture/실제 Helm 검사를 한 번에
python scripts/quality.py all
```

`tools`는 `scripts/test-ansible.sh`와 `scripts/test-helm.sh`를 실제 호출합니다.
Ansible 검사는 무해한 로컬 제어기 fixture이며, Helm 검사는 lint/render입니다.
SSH 원격 서버, 컨테이너 엔진, DB 쿼럼·영속성까지 검증하지 않습니다.
도구가 없으면 `blocked`, 종료 127입니다. 모의 모듈이나 Go 부분 렌더러로 실제 검사를 대체하지 않습니다.
테스트가 하나도 없거나 실패하면 성공으로 계산하지 않으며, 호스트 skip이 있으면 완전 통과로 표시하지 않습니다.

실제 DB 인수는 준비된 **전용 로컬 Docker**에서 따로 실행합니다.

```bash
bash ./all.sh mvp init       # 기존 설정 보존
bash ./all.sh mvp doctor
bash ./all.sh mvp up         # v10 API/worker를 다시 빌드해야 함
bash ./all.sh mvp inspect-runtime
bash ./all.sh mvp verify run core --yes
```

이 경로부터는 이미지 다운로드/빌드와 합성 주문 생성이 포함됩니다. 실패하면 기록을 확인합니다.
네트워크·OOM·데드락을 포함한 더 큰 범위는 core를 통과한 뒤 명시적으로 선택합니다.
[통합 인수 가이드](../mvp-lab/docs/RUNTIME-ACCEPTANCE.md)와 [Ansible 가이드](../ansible/README.md)를 따르세요.

## 3. v9에서 바뀐 계약

### HTTP 입력

POST `/api/orders`는 `item`, `quantity`, `unit_price`만 받습니다. PATCH는 `status`, `expected_version`만 받습니다.
오타 필드를 무시하던 동작은 이제 400입니다. 같은 JSON 키의 반복, NaN/Infinity/float overflow,
잘못된 UTF-8·단독 surrogate, 16단계를 넘는 JSON 중첩, 중복 Host/Idempotency-Key/Content-Length를 거부합니다.
Transfer-Encoding 방식은 계속 미지원이며 명확하게 거부합니다. 본문은 16KiB, query 필드는 20개까지입니다.

기본 서버는 동시에 처리하는 연결 스레드를 32개로 제한합니다. 슬롯이 찬 상태에서 수락한 연결에는 503 `api_capacity_exceeded`와
`Retry-After: 1`을 반환합니다. 이는 애플리케이션 수용 한도이며 DB 장애와 구분해야 합니다.
진단 요청 내부의 별도 작업이나 모든 DB 연결·메모리 사용을 32개로 제한하는 것은 아닙니다.
Python http.server 기반의 로컬 교육용 서버라는 범위는 그대로입니다. 인터넷 공개·인증/TLS는 지원하지 않습니다.

### 검색

ES가 HTTP 200을 반환해도 `timed_out=true` 또는 실패 shard가 있으면 503 `elasticsearch_search_incomplete`입니다.
검색 메타데이터가 누락된 비정상 응답도 성공으로 간주하지 않습니다.
ES의 3xx 응답을 따라가지 않고 `elasticsearch_redirect_refused`로 거절합니다.
역방향 프록시를 따로 추가한 사용자는 리다이렉트 대신 올바른 endpoint 설정을 확인해야 합니다.
원본 주문·인덱스를 지우거나 검색을 강제로 재구축하는 자동 복구는 추가하지 않았습니다.

### 로컬 파일과 Ansible

루트/MVP의 상태·보고서·설정 경로에 symlink가 있으면 관련 명령을 거부합니다.
파일을 지우거나 링크 대상을 자동 이동하지 않습니다. 의도한 링크였다면 기존 경로·소유권·데이터를 먼저 검토하세요.
로컬 관리자나 파일을 동시에 바꾸는 공격자를 완전히 격리하는 보안 장치는 아닙니다.
MVP 상태 JSON은 임시 파일을 600 권한으로 만들고 직렬화/파일 flush 후 교체합니다.
이는 DB 내구성이나 호스트 전원 손실 복구 보장이 아닙니다.

Ansible의 play-level 기본값이 inventory의 `db_lab_request`, `target`, 동의값 등을 덮어쓰지 않도록 했습니다.
미설정 값은 모듈 기본값을 사용하고 inventory 및 `-e`에 명시한 값은 반영합니다.
**기존 inventory의 `db_lab_allow_changes: true`도 이제 의도대로 적용됩니다. 먼저 `--check`와 `--limit ONE_HOST`로 확인하세요.**
수집 위치 `db_lab_collect_root`도 inventory에서 지정할 수 있습니다.

## 4. 결과 파일과 배포물 확인

모든 품질 명령은 새로운 `.quality/quality-<ID>/`에 `summary.json`, `report.md`, `junit.xml`을 기록합니다.
`offline/tools`는 단계별 로그도 남깁니다. 기존 증거는 덮어쓰지 않습니다.
특정 새 경로는 `--output /path/to/new-directory`로 지정합니다.

```bash
# 받은 ZIP이 이후 바뀌었는지 확인. 새 배포물에서 수행합니다.
python scripts/quality.py release
```

`RELEASE-MANIFEST.json`은 SHA-256과 파일 모드입니다. 기존 역사적 보고서도 목록에 들어 있으며 함께 검사합니다.
일반 실행이 만든 `.env`, `.state`, `reports`, `.quality`, 가상환경은 새로운 배포 파일로 취급하지 않습니다.
manifest에 원래 들어 있던 보고서는 해당 경로가 reports 안이어도 계속 검증합니다.
소스를 수정했다면 release 검사는 실패하는 것이 정상입니다. 서명·출처 진위·이미지 공급망 검증이 아닙니다.
Windows 압축 도구가 실행 비트를 보존하지 않은 경우 mode 오류를 조사하고 Linux 환경에서 재해제하세요.

## 5. CI 구분

- `.github/workflows/quality.yml`: push/PR/수동 실행에서 소스·호스트 테스트(Python 3.12/3.13)와 실제 Ansible 로컬 fixture 검사.
  DB 기동, SSH 대상, 장애 주입, secrets 사용은 없습니다. 최초 실행의 runner/네트워크/패키지 가용성은 실제 CI에서 확인해야 합니다.
- `.github/workflows/mvp-runtime.yml`: 기존처럼 명시적 동의가 필요한 수동 DB 인수시험. 자동 push/PR 기동으로 바꾸지 않았습니다.

workflow 파일을 추가한 것은 원격 저장소에서 실행 완료했다는 뜻이 아닙니다.

## 6. 유지보수 규칙

기능 변경에는 양성/음성 테스트, 실제 런타임 인수 기준, 소스/이미지 재빌드 필요 여부를 함께 기록합니다.
시험용 double은 tests에만 두고, 정상 실행에서 엔진 부재를 메모리 DB로 바꾸지 않습니다.
실패한 초기 결과와 수정 후 결과는 구분합니다. 과거 버전 테스트 숫자를 현재 숫자에 더하지 않습니다.
스키마/이미지 버전을 바꾸기 전 새 볼륨·별도 복원 환경에서 호환성을 검사합니다. 자동 downgrade/reset은 하지 않습니다.
MD 가이드의 첫 경로는 이 문서이고, 각 버전 보고서는 해당 시점의 역사적 증거입니다.
