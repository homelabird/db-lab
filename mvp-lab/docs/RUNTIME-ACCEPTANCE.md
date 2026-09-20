# DB 시뮬레이션 v6 — 실제 실행 대상부터 확인하는 통합 인수시험

기준: 2026-09-19. 기존 시나리오 30개와 기본 컨테이너 6개를 유지합니다.
**이 배포를 작성한 환경에서는 Docker가 없어 실제 DB 인수시험이 차단됐습니다. 코드/모의 검사 통과를 실제 DB 통과로 읽지 마세요.**

## 1. 이번 변경의 목적

테스트 코드가 맞아도 실행 중인 API가 오래된 이미지이거나, worker가 다른 버전이면 비교 결과를 신뢰하기 어렵습니다. `mvp verify`는 실행 대상을 확인하고 실제 컨테이너 안에서 드라이버/스키마를 검사한 다음 기존 실습을 순서대로 실행합니다. 이전 실행의 PASS나 예시 보고서를 재사용하지 않습니다.

검사기가 요청과 다른 주문을 성공으로 받아들이던 문제, 같은 ID의 다른 재시도 내용을 허용하던 문제, 공개 바인딩을 함께 가진 API를 허용하던 문제, Kafka checkpoint 조회 오류를 무시하고 lag를 계산하던 문제도 수정했습니다. 네 가지 모두 명시적인 모의 응답/엔진을 사용해 수정 전 수용, 수정 후 거부를 확인했습니다. 실제 주문 손상/공개 노출/메시지 유실을 관측한 것이 아닙니다.

## 2. 적용과 첫 실행

전체 ZIP을 사용합니다. `all.sh` 단독 교체는 지원하지 않습니다. 기존 `.env`, `.state`, named volume을 보존하고 같은 스택을 다른 사본에서 동시에 관리하지 마세요. 과거 engine pin을 지우거나 암호/KRaft ID를 새로 생성하지 않습니다.

이전 `messages --keep` 자원은 **API 재생성 전에** 해당 실행의 `messages inspect`와 `messages cleanup ... --yes`로 확인·정리합니다. 남아 있는 fault marker는 해당 종류의 recover 명령으로 먼저 조사합니다. API ID가 바뀐 보존 자원을 무조건 승계하지 않습니다.

프로젝트 루트에서:

```bash
bash ./all.sh mvp init          # 기존 설정 보존
bash ./all.sh mvp doctor
bash ./all.sh mvp up            # 사용자가 명시적으로 앱 이미지 빌드/기동

# 이 두 명령은 Docker와 설정 파일 없이도 계획을 확인할 수 있습니다.
bash ./all.sh mvp verify list
bash ./all.sh mvp verify plan core

# 실제 환경에서 쓰기/검사에 동의하고 기본 경로 실행
bash ./all.sh mvp verify run core --yes
```

`verify` 자신은 up/pull/build를 하지 않습니다. 기존 v5 이미지는 새 build manifest가 없으므로 거부될 수 있습니다. 원인을 확인하고 소스를 포함해 정상 빌드해야 하며 manifest를 수동으로 꾸며 통과시키지 마세요.

지원 자동 인수 대상은 기존 pin으로 고정된 **로컬 Docker + Compose v2 + Linux 컨테이너**입니다. 원격 Docker/Podman, Windows 컨테이너, 공유 운영 데이터베이스 대상으로 확대하지 않았습니다. 이미지 Python은 현재 Containerfile의 3.12와 맞아야 합니다.

## 3. 실행 순서

### A. 컨테이너와 저장 경로 확인

엔진 pin, 정상 모드, 복구 marker 부재, 여섯 컨테이너의 Running/준비 상태를 확인합니다. API는 설정된 `127.0.0.1` 포트 하나만 공개해야 합니다. 다른 서비스의 호스트 포트가 있으면 거부합니다.

MariaDB `/var/lib/mysql`, Kafka `/var/lib/kafka/data`, ES `/usr/share/elasticsearch/data`, Redis `/data`에 named volume이 있는지 검사합니다. API와 worker는 같은 실제 image ID여야 합니다. 이것은 mount 존재 확인이며 실제 fsync 내구성 시험은 아닙니다.

### B. API와 worker 각각에서 읽기 전용 계약 검사

두 컨테이너에서 `python -m mvp_app.runtime_contract probe`를 별도로 실행합니다.

| 검사 | 확인 내용 |
|---|---|
| 이미지 소스 | 빌드 때 기록한 manifest, 현재 컨테이너 파일, 호스트 소스의 세 해시가 일치하는지 |
| 직접 의존성 | Python 3.12, requirements.txt의 네 패키지 배포 버전과 실제 import, Kafka topic UUID API |
| 실제 연결 설정 | DB 주소/사용자/schema/topic/group/index/cache TTL/project와 암호 해시가 호스트의 정상 모드와 같은지 |
| MariaDB | 실제 연결, DB/서버 버전/datadir, session lock wait, InnoDB 두 테이블의 컬럼 타입/필수 unique key |
| Redis | PING/INFO, master 역할, AOF 활성 여부 |
| Elasticsearch | 실제 HTTP GET, strict mapping/필드 타입, 1 shard/0 replica, index UUID |
| Kafka | topic UUID/단일 partition, committed·low·high, 보존 범위 밖 checkpoint 여부 |

소스 범위는 `mvp_app`의 Python/HTML, `requirements.txt`, `Containerfile`입니다. 호스트 테스트/도구 전체를 이미지에 넣거나 서명하는 기능은 아닙니다. SHA-256 일치는 소스 일치 검증이지 이미지 공급망 서명 인증이 아닙니다. 직접 패키지 버전만 고정 확인하며 전이 의존성 완전 잠금/CVE 검사/이미지 최신성 검사는 아닙니다.

잘못된 이미지·패키지·설정이면 네 DB 검사까지 진행하지 않습니다. DB 검사에는 INSERT/DDL, Redis SET/DEL, ES 색인/refresh, Kafka produce/commit을 넣지 않았습니다. SQL은 세션 설정 후 READ ONLY 트랜잭션으로 metadata를 읽고 rollback/close합니다. 연결·로그·내부 진단 카운터 등 관측 비용까지 없다는 뜻은 아닙니다.

### C. 실제 smoke와 선택한 시나리오

새 smoke는 제출한 값, 생성 응답, 동일 키 재시도, 원본 조회, 검색 응답의 전체 내용을 검사합니다. HTTP 리다이렉트를 따라가지 않으며 응답 크기를 제한합니다. 합성 주문 한 건이 남습니다. 정상 상세 `fresh=1` 조회는 캐시를 채울 수 있으므로 smoke 전체가 읽기 전용인 것은 아닙니다.

이후 기존 simulate/drills/messages 실행기를 호출합니다. stdout의 `passed`만 보지 않고 **이번 호출에서 새로 생성된 보고서**의 실행 ID/시나리오/실제 실행용 증거 종류/복구 또는 정리 여부를 대조합니다. 종료 코드 0인데 근거가 없거나 이전 보고서이면 실패입니다.

각 시나리오 전후에 이미지 ID, named volume, 앱 소스, 엔진·모드를 다시 검사합니다. 같은 이미지/볼륨을 유지한 정상 `redis-recreate` 또는 앱 재생성의 container ID 변화는 허용합니다. 모든 단계에서 컨테이너 ID를 절대 고정하는 방식은 아닙니다.

## 4. 선택 가능한 시험 묶음

모든 묶음은 위 A/B와 smoke를 포함합니다. 다음 숫자는 **계획된 시나리오 수**이며 실행한 수가 아닙니다.

| 묶음 | 시나리오 범위 | 수 |
|---|---|---:|
| core | baseline | 1 |
| simulations | 기존 simulate 전부 | 13 |
| transactions | baseline + 트랜잭션 비교 6개 | 7 |
| messages | baseline + 메시지 실습 6개 | 7 |
| network | 지연/손실 2개 (별도 baseline은 core에서 먼저 실행) | 2 |
| resources | baseline + Redis disk-full/OOM | 3 |
| recovery | baseline + backup-restore/deadlock | 3 |
| all | 후보 버전 실험을 제외한 전체 | 29 |
| candidate | baseline + 명시한 후보 이미지 logical restore | 2 |

```bash
bash ./all.sh mvp verify run transactions --yes
bash ./all.sh mvp verify run messages --yes
bash ./all.sh mvp verify run recovery --yes
```

`network/simulations/all`에는 netem helper가 필요합니다. 자동 빌드하지 않습니다.

```bash
bash ./all.sh mvp drills prepare --yes
bash ./all.sh mvp verify plan all
bash ./all.sh mvp verify run all --yes
# 같은 진입점의 셸 래퍼
bash ./scripts/test-runtime-acceptance.sh all --yes
```

`all`은 **29개이지 30개가 아닙니다.** MariaDB 후보 이미지를 임의 선택하지 않습니다. 사람이 검토하고 미리 pull한 공식 MariaDB 숫자 버전 태그를 shell 변수 `CANDIDATE`에 지정한 뒤:

```bash
bash ./all.sh mvp verify plan candidate --candidate-image "$CANDIDATE"
bash ./all.sh mvp verify run candidate --candidate-image "$CANDIDATE" --yes
```

후보 DB는 기존 버전 실습과 동일하게 새 임시 DB의 논리 복원입니다. 원본 볼륨의 in-place upgrade/down-grade 인증은 아닙니다. 구형 `test-runtime-mvp/messages/transactions.sh`도 유지하지만 새 통합 검사가 이미지/드라이버/대상 검사까지 묶는 진입점입니다.

## 5. 결과 읽기

실행마다 다음 경로를 새로 만듭니다.

```text
mvp-lab/reports/acceptance/accept-<12자리실행ID>/
    plan.json
    source-manifest.json
    containers-before.json          # 대상 검사가 도달했을 때
    api-runtime-contract.json       # 해당 단계가 실행됐을 때
    worker-runtime-contract.json
    smoke.json / simulate-*.json / drills-*.json / messages-*.json
    summary.json
    report.md
    junit.xml
```

원래 시나리오의 상세 report/trace/snapshot은 기존 reports/simulations, reports/drills, reports/messages에 그대로 있습니다. 통합 결과는 그 경로와 SHA-256을 연결합니다. Docker 부재로 입구에서 막히면 존재하지 않는 native/시나리오 결과를 만들지 않습니다.

```bash
bash ./all.sh mvp verify report accept-실제12자리ID
```

| 상태 | 해석 |
|---|---|
| passed / 0 | 선택한 범위의 prerequisite와 모든 시나리오 통과 |
| failed / 1 | 실행·내용·근거·정리·불변 조건 중 실패 |
| blocked / 1 또는 런타임 부재 127 | 실행 환경/선행 조건을 만족하지 못함 |
| inconclusive / 2 | 효과가 충분히 관측되지 않아 성공 판정 불가 |
| aborted / 130 | 사용자 중단 |
| 단계별 not_run | 앞 단계 중단으로 실행 안 함. 성공/실패의 실제 관측이 아님 |

JUnit의 실패/오류/건너뜀을 별도 출력합니다. `core`는 총 5단계(대상, API, worker, smoke, baseline)이며 5단계가 모두 지나야 합니다. `all`은 총 33단계이며, runtime 부재시 error 1 + skipped 32입니다. JUnit skipped를 통과로 바꾸어 합산하지 마세요.

출력은 개인 권한(디렉터리700/파일600)으로 보관합니다. raw stderr는 접속 정보가 섞일 수 있어 저장하지 않고 바이트 수만 남깁니다. API/worker의 구조화된 오류 코드와 기존 앱 로그를 함께 확인하세요. 합성 주문이 포함되는 시나리오 상세 보고서도 민감한 자료로 다룹니다.

## 6. 실패 후 행동과 시간 제한

첫 실패/inconclusive에서 중단하고 이후 단계는 not_run입니다. 단순 재시도로 통과 기록을 덮어쓰지 않으며 새 실행 ID로 다시 검사합니다. 자동 기동/복원/강제 삭제/캐시 비우기/재색인으로 증상을 숨기지 않습니다.

marker가 남았으면 실제 상태와 종류를 확인한 뒤 각각 `simulate recover --yes`, `drills recover --yes`, `messages recover --yes`를 사용합니다. 이는 별도 판단이며 통합 gate가 자동 호출하지 않습니다. 소유권이 달라진 경우 복구도 거부될 수 있습니다.

기본 시나리오 subprocess 예산은650초, 선택 범위는120..1200초입니다. 예:

```bash
bash ./all.sh mvp verify run core --step-timeout 900 --yes
```

시간 초과시 해당 자식 프로세스 그룹에 TERM과 정리 유예 후 KILL을 사용합니다. 유예 중 종료 코드0이 돼도 초과한 단계는 통과시키지 않습니다. metadata/preflight와 정리 유예를 포함한 전체 명령의 hard deadline은 아닙니다. stdout은 읽어들이는 시점에16MiB를 검사하며 실행 중 임시 파일의 디스크 사용량을 OS 차원에서 제한하는 것은 아닙니다. `docker exec` CLI 중단이 서버 내 helper까지 즉시 멈춘다는 보장도 없습니다.

같은 사본의 acceptance runner 간 flock과 기존 명령별 lock을 사용합니다. 다른 디렉터리·수동 Docker·일반 명령을 전체 묶음 동안 전역으로 잠그지는 않습니다. 실행 중 다른 조작/부하를 하지 마세요. 완전한 외부 watchdog이나 호스트 종료 뒤 자동 복구를 추가한 것은 아닙니다.

## 7. 이번 배포의 검증 경계

호스트 검사는 DB/엔진 double, 기존 일부 SQLite 어댑터, 실제 loopback HTTP를 구분합니다. 이 버전에서 실제 `verify run core/all --yes`를 호출했지만 Docker가 없어 종료127, 전체 status=blocked, passed_scenarios=0으로 기록됐습니다. 존재하지 않는 DB 실행을 PASS로 채우지 않았습니다.

실제 이미지 빌드/패키지 import, MariaDB schema/락/commit, Kafka topic UUID/ACK/checkpoint, Redis AOF, ES mapping/refresh, Docker netem/자원 실습/정리는 현장 실행 전입니다. metadata 검사를 통과하더라도 HA·성능·fsync·전체 서버 백업/PITR·다중 호스트를 인증하지 않습니다. 기존 시나리오의 학습용 경계를 유지합니다.

## 공식 계약 참고

- Docker build context와 `.dockerignore`: https://docs.docker.com/build/concepts/context/
- Docker run/publish/network 옵션: https://docs.docker.com/reference/cli/docker/container/run/
- PyMySQL Connection: https://pymysql.readthedocs.io/en/latest/modules/connections.html
- Confluent Python API의 TopicPartition.error/committed/watermarks: https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html

문서 조회는 고정 이미지의 실제 실행 근거가 아닙니다. 코드의 직접 패키지 버전은 requirements.txt가 기준입니다.
