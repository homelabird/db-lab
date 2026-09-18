> **수정 전 검증 기록입니다.** 현재 수정본의 결과는 [FIXES-VALIDATION.md](FIXES-VALIDATION.md)와 `reports/validation/host-validation.json`을 확인하세요.

# 검증 내역과 남아 있는 확인 사항

기준일: 2026-09-17. 새 시나리오 확장까지 재실행한 총 139개 검사와 한계는 [SCENARIOS-VALIDATION.md](SCENARIOS-VALIDATION.md)를 보세요. 아래 82개는 기본 프로젝트 최초 검증 내역입니다. **정적 검사 통과와 실제 DB 실행 성공을 구분합니다.**

## 이 제작 환경에서 완료한 검사

| 항목 | 실제 수행한 범위 | 결과 |
|---|---|---|
| Python 단위/정적 테스트 | `python3 -m unittest discover -s tests -v` | 82개 통과 |
| 복구 노드 선택 | 빈 볼륨, 유일한 safe flag, 높은 seqno, 불명확한 위치, UUID 불일치, 부분 초기화 | 안전 조건·거부 분기 통과 |
| 파괴적 명령 방어 | running/paused 볼륨 접근 거부, 살아 있으나 unready인 클러스터 자동 bootstrap 거부, 불건전한 donor가 있을 때 rebuild 거부 | mock 기반 제어 흐름 테스트 통과 |
| 상태 판정 | SQL 연결만 성공한 경우 제외, Primary/Synced/connected/ready 조건, donor/joiner/non-Primary 제외 | 단위 테스트 통과 |
| 환경 설정 | 랜덤 비밀번호·권한 600, 기존 .env 보존, 포트·프로젝트명·입력 제한 | 임시 디렉터리 테스트 통과 |
| Compose | YAML 파싱, 3~5개 server_id, 독립 볼륨, restore 네트워크 분리, localhost 포트, privileged/host-network/socket bind 부재 | 구조 검사 통과 |
| 데이터 생성 | tiny/small/standard/large SQL 생성, Sequence 범위 연속성, FK ID 범위 치환, 배치 경계, 가격 조건 보존, helper table 의존 제거 | 생성 결과 검사 통과 |
| Shell/Python 문법 | 신규 shell의 `bash -n`, Python compileall, CLI 도움말 | 통과 |
| 웹 대시보드 | 실제 Chromium에서 로컬 HTML 실행, **명시적인 합성 API fixture**로 정상 3노드·저하 1노드, 수동 새로고침, JSON 펼침, 1440px/390px 레이아웃 | 통과, JS 오류 없음 |
| 첨부 원본 보존 | 압축 원본의 파일 28개를 `standalone-original/`와 바이트 비교 | 모두 동일 |

테스트 로그는 `docs/static-tests.log`, 원본 체크섬은 `docs/original-files.sha256`에 있습니다. 테스트의 합성 상태 값은 테스트 코드에만 있으며, 실제 서비스의 상태 API에는 가짜 DB 결과나 성공 fallback을 넣지 않았습니다.

standard 데이터의 **계획된 생성 행 수는 2,040,068행**입니다. 이는 생성 규칙으로 산출한 수이며, 여기서 실제 DB에 적재해 측정한 값이 아닙니다. `verify`는 사용자 호스트에서 적재 후 실제 COUNT(*)와 비교합니다.

## 이 환경에서 실행하지 못한 검사

제작 환경에 Podman, Docker, MariaDB 서버 및 HAProxy 실행 파일이 없습니다. 따라서 다음 작업은 **미실행**입니다.

- 실제 컨테이너 이미지 pull/build, apt 의존성 설치, HAProxy 실행 파일의 설정 문법 검사.
- MariaDB의 전체 스키마/SQL 실행, 공식 entrypoint와 초기화 래퍼의 실제 프로세스 순서.
- Galera 3노드 가입, mariadb-backup SST 및 IST 선택, rootless Podman 네트워크/DNS 동작.
- 실제 프록시 장애 전환, 인증/권한, 동시 송금, 인증 충돌·BF abort 발생.
- 실제 quorum 상실·회복, 전체 crash 후 복구, 데이터베이스 백업·복원.
- SELinux 호스트에서의 실제 정책 검사, 자원 사용량/성능/지연 측정.

`images/proxy/Dockerfile`에 넣은 `haproxy -c`는 **사용자가 이미지를 빌드할 때** 수행됩니다. 위 Compose 검사는 실제 `podman-compose config`/`docker compose config` 실행을 대체하지 않습니다. 웹 UI 테스트도 실제 Galera 상태 수집 검증이 아닙니다.

## 사용자 호스트에서의 확인 순서

```bash
./lab.sh init
./lab.sh doctor
./lab.sh up
./lab.sh seed --size tiny
./lab.sh verify
./lab.sh routes
```

처음에는 tiny로 초기화·복제·권한·프록시 경로를 확인하고, 성공하면 원하는 데이터 크기로 다시 적재합니다.

```bash
./lab.sh seed --size standard --replace --confirm-replace
```

전체 실습용 자동 수용 테스트도 넣었습니다. **기존 commerce_lab 합성 데이터와 별도 restore 데이터를 교체하고, 이 랩의 노드 강제 종료·재구축을 실행**하므로 일회용 랩에서만 사용하세요. 자동 초기화나 백그라운드 실행은 하지 않습니다.

```bash
RUN_DISRUPTIVE_TESTS=1 bash tests/acceptance.sh
```

이 스크립트는 실제 실행 로그를 `reports/acceptance.log`에 남깁니다. 성공 시에도 quorum 상실, 전체 노드 crash 복구, IST 선택, flow control은 `WORKBOOK.md`에서 별도로 관찰해야 합니다. 실패 시 강제로 복구/삭제하지 않고 진단을 위해 현재 상태를 남깁니다.

## 로컬 테스트 재실행

```bash
# DB나 컨테이너 없이 실행. PyYAML이 없으면 Compose 구조 테스트 9개만 skip.
python3 -m unittest discover -s tests -v

# 선택: Python playwright와 Chromium이 준비된 개발 환경에서 UI만 검사
python3 tests/dashboard_smoke.py

# 원본 보존 체크섬 검사 (프로젝트 루트에서)
sha256sum -c docs/original-files.sha256
```

실제 운영 검증, 보안 감사, 성능 벤치마크를 완료한 제품으로 간주하지 마세요. 한 호스트에 모인 노드와 단일 프록시이므로 물리 서버 장애에 대한 HA 검증용도 아닙니다.

단일 호스트에서 프로세스/컨테이너 재시작 정책까지 적용하려면 `compose.production.yaml`을
`compose.yaml` 위에 overlay로 지정합니다. 이 overlay는 재시작 정책과 proxy/dashboard
healthcheck만 제공하며, TLS·외부 secret 관리·원격 백업/PITR·다중 호스트 분산은 운영 환경에서
별도로 강제해야 합니다.
