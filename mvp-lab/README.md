# 작은 주문 시스템 — DB 연결·장애·교체 공부용 MVP

현재 시작점은 [v12 시작·진단 수정 가이드](../docs/FOLLOWUP-REPAIR-2026-09-21.md)입니다.
검사 명령의 기본 설명은 [품질 가이드](../docs/QUALITY-GUIDE.md)를 확인하세요.
전체 품질 검사 → core 인수 → 개별 장애 실습 → [같은 계획의 비교 실험](docs/COMPARATIVE-STUDIES.md) 순서로 진행합니다.
트랜잭션·메시지·네트워크 실습은 아래 문서의 별도 경계를 확인하세요.
[트랜잭션](docs/TRANSACTION-DRILLS.md) · [메시지](docs/MESSAGE-DRILLS.md) · [네트워크/복원](docs/ADVANCED-DRILLS.md) · [Ansible](../ansible/README.md)

이 시스템은 기능이 많은 쇼핑몰이 아닙니다. 주문 하나가 저장·전달·검색되는 경로를 만들고,
**어느 DB가 멈췄는지에 따라 왜 일부 기능만 실패하는지** 관찰하는 실습입니다.
로그인, 실제 결제, React 빌드, Kubernetes, CDC/Connect, Schema Registry는 넣지 않았습니다.

기본 구성은 **MariaDB 1 + Kafka 1 + Elasticsearch 1 + Redis 1 + API 1 + worker 1 = 6개 컨테이너**입니다.
앱은 Python, 화면은 HTML 한 장입니다. `redis-spare` 실험에서만 Redis 하나가 추가됩니다.

> 기존 `mariadb-ha-lab`, `kafka-lab`, `elasticsearch`, `redis-lab`의 실행 중인 클러스터에 자동 연결하지 않습니다.
> 같은 네 가지 기술을 사용하는 **별도 단일 노드 MVP**이며, 프로젝트·네트워크·볼륨·암호가 분리됩니다.
> MVP를 일반 `./all.sh up/down`의 대상에 추가하지 않았습니다. HA 실습 및 Helm과는 별도의 실행 경로입니다.

## 1. 데이터 흐름

```text
                    하나의 SQL 트랜잭션
주문 등록 ── API ── MariaDB [orders + outbox]
                                  │
                         worker의 전송 루프
                                  │ broker ACK 후 sent_at 기록
                                  ▼
                                Kafka
                                  │
                         worker의 검색 반영 루프
                                  │ ES 반영 성공 후 offset commit
                                  ▼
                           Elasticsearch

주문 상세 ── API ── Redis HIT → 응답
                    └─ MISS/접속 실패 → MariaDB 조회 → Redis 저장(TTL)

주문 상태 변경 ── MariaDB version 증가 + 새 outbox → 검색 반영
               └─ 캐시 무효화 (실패하면 로그; 쓰기 자체를 취소하지 않음)
```

MariaDB가 원본입니다. Redis와 Elasticsearch는 원본의 대체 백업이 아닙니다.
Kafka는 비동기 전달 경로이지 SQL과 하나의 트랜잭션을 공유하는 DB가 아닙니다.
`outbox`는 주문과 같은 트랜잭션에 저장하는 **전송 대기표**입니다.

## 2. 시작하기

Linux 또는 WSL2, Python 3.10 이상, 루트 진입점용 Bash 4.4+, 컨테이너 엔진과 Compose provider가 필요합니다.
우선 대상은 Docker Compose v2이며 Podman + Compose provider 감지도 제공합니다. 이번 작업 환경에서는
어느 provider도 실기동 검증하지 않았습니다. Podman에서는 `doctor`로 해당 버전의 profile/build 지원부터 확인하세요.
앱의 Python 패키지는 이미지 빌드 중 설치합니다. 호스트에 Flask/React/DB 드라이버를 설치할 필요는 없습니다.
이미지와 패키지를 처음 받을 때 인터넷 연결이 필요합니다.

전체 ZIP을 해제한 **프로젝트 루트**에서:

```bash
bash ./all.sh mvp init       # 암호·KRaft ID 생성; 기존 .env 보존
bash ./all.sh mvp doctor     # 엔진 및 실제 Compose config 검사
bash ./all.sh mvp up         # 이미지 빌드, 6개 컨테이너 기동, schema/topic/index 준비
bash ./all.sh mvp diagnose   # 읽기 전용; 의존 서비스 장애면 exit 1
bash ./all.sh mvp smoke      # 정확한 대상 확인 후 합성 주문 1건 → Kafka → ES 검색 확인
```

브라우저에서 `http://127.0.0.1:18090`을 엽니다. 순서대로 **주문 저장 → 일반 조회 두 번 → 검색 → 진단 새로고침**을 누르세요.
일반 조회의 `source`가 `mariadb`에서 `redis`로 바뀌는지 확인합니다. 검색은 비동기라 잠시 늦을 수 있습니다.

`smoke`/`diagnose` 명령은 엔진이 설치된 실습 호스트에서 실행합니다. 원격 Docker/Podman
선택 상태에서는 로컬 HTTP 명령을 거부합니다. 원격 실습 서버의 브라우저 접근은 DB/API
포트를 공개하지 말고 SSH 터널을 사용합니다.

```bash
ssh -L 18090:127.0.0.1:18090 your-lab-host
# 내 PC의 브라우저에서 http://127.0.0.1:18090 접속
```

API만 호스트 loopback에 공개됩니다. DB 포트는 호스트에 공개하지 않았고, HTTP API는 loopback Host 헤더만 받습니다.
멀티 사용자 서비스, 인증/TLS 예제, 인터넷 공개 서버로 쓰지 마세요. SQL/Redis 암호는 `.env`와 컨테이너 환경에 있으므로
컨테이너 엔진 관리자는 볼 수 있습니다. Secret 관리 솔루션을 구현한 것은 아닙니다.

메모리 제한 합계는 기본 6개 기준 3,264MiB입니다. 이것은 **실측 사용량이나 최소 사양이 아닙니다**.
엔진·호스트 여유를 포함해 초기 실습 예산을 6–8GiB 정도로 잡고, 다른 HA 실습은 같이 띄우지 않는 방향을 권합니다.
성능 측정용 구성은 아니며 ES mmap을 끄므로 이 MVP에서 얻은 성능을 운영 환경과 비교하지 않습니다.

### 기존 설정·볼륨 보존

`init`은 이미 있는 `.env`를 덮어쓰지 않습니다. `down`, `stop`, `recreate`도 볼륨을 삭제하지 않습니다.
처음 `up`한 뒤에는 프로젝트 이름·암호·KRaft ID 변경을 감지해 새 기동을 막습니다. 이는 암호를 DB 내부에서 회전시키는 기능이 아닙니다.

두 MVP 사본을 동시에 띄우려면 **첫 up 전** 서로 다른 `MVP_PROJECT=db-lab-mvp-<이름>`와 `API_PORT`를 정하세요.
같은 엔진에서 같은 project 이름을 쓰면 디렉터리가 달라도 같은 자원을 관리할 수 있습니다.
기존 배포를 새 코드로 관리할 때는 기존 `.env`와 `.state`를 보존해야 합니다. 여기에는 사용자 환경으로의 자동 적용·이전이 없습니다.

## 3. 명령 빠른 참고

| 명령 | 범위 |
|---|---|
| `./all.sh mvp up` | 이 MVP만 빌드·기동하고 초기 schema/topic/index 준비 |
| `./all.sh mvp down` | 이 MVP 컨테이너·네트워크 종료, 모든 데이터 볼륨 보존 |
| `./all.sh mvp status` | 선택된 실험과 컨테이너 상태 |
| `./all.sh mvp diagnose` | DB별 연결 상태, SQL outbox 수, Kafka consumer lag, ES 문서 수 |
| `./all.sh mvp logs worker --follow` | 전송·검색 반영·재시도 로그 |
| `./all.sh mvp stop kafka --yes` | 지정 서비스만 의도적으로 중단 |
| `./all.sh mvp resume kafka` | 같은 설정·볼륨으로 지정 서비스 재시작 |
| `./all.sh mvp recreate redis --yes` | 해당 컨테이너를 재생성하되 기존 볼륨 사용 |
| `./all.sh mvp experiment <모드> --yes` | 아래 세 실험 또는 normal 선택 |
| `./all.sh mvp rebuild-search --yes` | MariaDB 현재 상태를 현재 ES 인덱스에 재색인; 기존 인덱스 삭제 안 함 |
| `./all.sh mvp smoke` | 가짜 주문 1건 생성·동일 키 재요청·검색 반영·SQL 원본 비교 |
| `./all.sh mvp test` | 호스트 로직 테스트; 실제 DB가 없는 테스트 |

`--dry-run`은 루트 옵션입니다: `./all.sh --dry-run mvp up`.
`mvp-lab` 폴더 안에서는 `bash ./lab.sh ...`를 사용해도 됩니다.
서비스 이름: `mariadb`, `kafka`, `elasticsearch`, `redis`, `api`, `worker`, `redis-spare`.
`reset`, `prune`, 임의 Compose 옵션 전달, 볼륨 삭제 명령은 제공하지 않습니다.

## 4. 가장 먼저 해볼 실습: Kafka 중단

먼저 `smoke`가 통과하는지 확인한 뒤 진행합니다.

```bash
./all.sh mvp stop kafka --yes
# 화면에서 새 주문을 2~3개 등록. MariaDB 주문 목록에는 나타나야 합니다.
./all.sh mvp diagnose
./all.sh mvp logs worker
./all.sh mvp sql outbox

./all.sh mvp resume kafka
# 잠시 후 진단과 검색 새로고침
./all.sh mvp diagnose
./all.sh mvp smoke
```

**예상 관찰:** 주문 저장은 성공하지만 새 주문의 검색 반영은 지연됩니다. `outbox_pending`이 증가하고 worker에 `relay_retry`가 남습니다.
복구 후에는 대기가 줄고 같은 주문 ID가 검색에 나와야 합니다. 중단 전에 전송·prefetch된 이벤트는 중단 후에도 일부 처리될 수 있으므로
반드시 **중단 후 새로 만든 주문 ID**로 비교하세요.

## 5. DB별 장애 실습

한 번에 하나씩 수행하고 복구한 뒤 다음 실습으로 넘어갑니다.

| 멈출 대상 | 기대 증상 | 핵심 관찰 | 복구 |
|---|---|---|---|
| Redis | 주문 저장/검색은 유지. 상세는 SQL로 우회 | 상세 응답 `source=mariadb`, `cache=unavailable` | `resume redis` |
| Kafka | 주문은 저장되지만 검색이 늦음 | `outbox_pending`, `relay_retry` | `resume kafka` |
| Elasticsearch | 검색 503. 주문 저장·상세는 가능 | Kafka `lag`, `consumer_retry`. outbox가 0이어도 검색 미완료 가능 | `resume elasticsearch` |
| MariaDB | 신규 주문·직접 SQL 조회 503. 캐시에 남은 일부 상세와 기존 검색은 가능 | DB 연결 오류와 cache HIT의 차이 | `resume mariadb` |
| worker | DB 네 개는 연결되지만 새 주문이 검색으로 전달되지 않음 | 컨테이너 상태 vs outbox/lag vs smoke | `resume worker` |

예: `./all.sh mvp stop elasticsearch --yes` → 화면 비교 → `./all.sh mvp resume elasticsearch`.
`resume`은 시작 요청이지 준비/복구 완료 판정이 아닙니다. `status`, `diagnose`, 해당 주문 검색, `smoke`까지 확인하세요.

## 6. 실제 교체·설정 변경 실습

### A. 별도 Redis 인스턴스로 연결 대상 교체

```bash
./all.sh mvp experiment redis-spare --yes
# API/worker가 별도 redis-spare 컨테이너와 별도 볼륨을 사용합니다.
# 같은 주문을 일반 조회 두 번: 첫 사용 시 MISS → SQL → 다음 HIT.
./all.sh mvp diagnose
./all.sh mvp experiment normal --yes
```

기존 Redis는 삭제하지 않습니다. **spare 첫 사용에만 빈 캐시**이며 재사용 때는 자신의 기존 볼륨이 남아 있습니다.
여기서 공부할 것은 “캐시는 비어 있어도 원본에서 다시 채울 수 있지만 DB 부하가 늘 수 있다”입니다.
복제 기반 무중단 failover나 데이터 마이그레이션을 구현한 것은 아닙니다.

### B. API 연결 비밀번호만 잘못 바꾸기

```bash
./all.sh mvp experiment bad-db-password --yes
# 신규 주문과 MariaDB 직접 조회는 503.
# TTL 안의 cache HIT 또는 기존 ES 검색은 성공할 수도 있습니다.
./all.sh mvp diagnose
./all.sh mvp logs api
./all.sh mvp experiment normal --yes
./all.sh mvp smoke
```

DB 프로세스와 실제 DB 암호는 그대로이며 **API만 틀린 암호**로 접속합니다. worker는 원래 암호를 사용합니다.
따라서 “컨테이너는 Up인데 애플리케이션이 DB 연결에 실패하는 상태”를 비교할 수 있습니다.
실험 상태에서 `up`은 성공으로 가장하지 않고 normal 복원을 요구합니다. `.env`의 진짜 암호를 바꿔 우회하지 마세요.

### C. 기존 데이터를 보존하고 빈 검색 인덱스로 전환

```bash
./all.sh mvp experiment fresh-search --yes
# mvp-orders-v1은 보존하고 API/worker는 mvp-orders-v2를 사용합니다.
# 기존 consumer group의 offset을 유지하므로 과거 주문이 자동 재전달되지는 않습니다.
./all.sh mvp rebuild-search --yes
./all.sh mvp smoke

./all.sh mvp experiment normal --yes
# v2를 쓰던 동안 v1은 갱신되지 않았을 수 있습니다. 돌아온 인덱스도 현재 SQL로 맞춥니다.
./all.sh mvp rebuild-search --yes
```

이것은 **인덱스 교체**이지 Elasticsearch 서버·디스크 교체가 아닙니다. v2 역시 처음 쓸 때만 비어 있습니다.
`rebuild-search`는 Kafka 이력을 재생하지 않고 MariaDB의 **현재 주문 상태**를 다시 씁니다.
원본 백업이나 전체 변경 이력 복원은 아닙니다. 실행 중 오류는 숨기지 않으며 같은 명령을 재실행할 수 있습니다.

### D. 컨테이너만 재생성하고 같은 데이터 볼륨 사용

```bash
./all.sh mvp recreate redis --yes
./all.sh mvp recreate elasticsearch --yes
./all.sh mvp status
./all.sh mvp smoke
```

컨테이너의 교체와 데이터의 초기화는 별개라는 점을 확인합니다. 위 명령은 다른 DB 엔진으로의 교체나 버전 업그레이드가 아닙니다.
이미지 태그는 `.env`에서 변경 가능하지만 호환성이 자동 보장되지 않습니다. 다른 버전을 시험할 때는
새 MVP project/별도 볼륨에서 시작하고 백업·호환성·복구 경로를 확인하세요. 데이터 포맷이 바뀐 볼륨에 예전 이미지를 다시 붙이는 것을
안전한 롤백이라고 가정하지 마세요. 특히 MariaDB/Kafka/ES의 기존 볼륨을 삭제해 오류를 없애는 절차는 제공하지 않습니다.

**모드는 한 번에 하나만 활성화됩니다.** mode는 `.state/experiment.json`에 저장되며 명령 실패 시 부분 적용 상태가 남을 수 있습니다.
`normal`은 API/worker 설정을 복원할 뿐, `stop`한 DB를 자동으로 시작하거나 기존 인덱스를 자동 복구하지 않습니다.
중단한 서비스는 `resume`, 오래된 인덱스는 `rebuild-search`로 따로 복구합니다.

## 7. 로그 읽는 법

```text
order_saved       → MariaDB 주문 + outbox 트랜잭션 성공
outbox_published  → Kafka delivery ACK 이후 sent_at 갱신
search_projected  → ES 쓰기/구버전 무시 + consumer offset commit
relay_retry       → SQL/outbox 또는 Kafka 전달 실패; 전송 대기 유지
consumer_retry    → Kafka/이벤트 검증/ES/offset 처리 실패; 이후 이벤트로 건너뛰지 않음
cache_*_failed    → Redis 장애/오류, SQL 원본 읽기 또는 TTL 복구 확인
```

`order_id`로 요청을, `event_id`로 메시지를 연결해 보세요. 같은 `event_id` 로그가 반복될 수 있습니다.
Kafka 전송 후 outbox 표시 전에 worker가 죽는 간격이 있으므로 이 구조는 **중복 전달을 허용**합니다.
ES 문서 ID=주문 ID, external version=SQL 주문 버전으로 중복/늦은 이벤트가 최신 값을 덮어쓰는 것을 방지합니다.
이는 네 DB 전체에 대한 exactly-once 트랜잭션이 아닙니다. [S1, S2]

`diagnose`의 종료 코드 0은 진단 JSON 조회 성공이지 모든 DB 정상 판정이 아닙니다.
`outbox_pending=0`은 Kafka에 전송 표시를 했다는 뜻이며 ES 반영 완료가 아닙니다.
`lag=0`과 연결 정상도 업무 성공의 충분조건이 아닙니다. 실제 주문 ID/버전과 `smoke`를 함께 봅니다.
프로세스 상태와 연결 상태, 처리 대기와 업무 데이터 정합성을 구분하는 것이 핵심입니다.

## 8. 공부용으로 남겨 둔 한계

단일 호스트·단일 DB 노드·한 worker·한 Kafka partition입니다. 물리 호스트 장애, HA/RTO/RPO, 성능을 검증하는 구성이 아닙니다.
worker를 임의로 여러 개로 scale하지 마세요. outbox relay의 분산 소유권 잠금은 구현하지 않았습니다.

캐시는 best-effort cache-aside이며 선형적인 최신 읽기를 보장하지 않습니다. 캐시 무효화 실패나 동시 읽기/쓰기 경합으로
과거 값이 재유입될 수 있습니다. TTL은 **캐시 저장 시점 기준**이며 `fresh=1`로 SQL 원본과 비교하세요. [S3]

잘못된 이벤트는 삭제·건너뛰지 않고 해당 partition을 재시도합니다. DLQ/웹 재처리 콘솔/스키마 자동 변환은 넣지 않았습니다.
검색 인덱스의 strict mapping과 코드의 schema version 검사가 실패를 드러냅니다. 엔진 장애인지 독성 메시지인지 로그로 구분하세요.

검색 재구축은 페이지 단위 현재 상태 조회이며 일관된 시점의 백업이 아닙니다. 동시 변경은 이벤트 경로와 버전 검사를 사용합니다.
DB를 과거 버전으로 복원한 경우 기존 ES의 더 높은 version 때문에 덮어쓰기가 거부될 수 있으므로, 별도 새 인덱스에서 비교해야 합니다.
SQL outbox는 자동 정리하지 않습니다. 소량 실습용이며 Kafka 보존 기간 기본값은 24시간입니다.

이 MVP도 기존 프로젝트와 같은 Elasticsearch 7.17.29 legacy 기준을 사용합니다.
7.17.x는 2026-01-15 지원이 종료된 계열이며 공개 서비스에 쓰기 위한 추천이 아닙니다. [S4]
이미지는 지정 태그이며 일부 태그의 digest는 바뀔 수 있습니다. Python 직접 의존성은 버전을 지정했지만 전이 의존성은 전체 잠금하지 않았습니다. 따라서 최신 버전·취약점 검사 완료·모든 CPU 아키텍처의 pull 가능성을 의미하지 않습니다.

## 9. 테스트와 코드 읽는 순서

```bash
./all.sh mvp test
# DB 기동 환경에서 별도로 실행해야 실제 경로를 확인합니다.
./all.sh mvp smoke
```

단위/계약 테스트의 fake DB는 `tests/`에만 있고, 실제 API/worker 실행은 항상 실제 DB 드라이버를 사용합니다.
SQL 저장 로직 일부는 SQLite 테스트 어댑터로 DML·rollback을 검사합니다. 이는 MariaDB 구문·격리·잠금·지속성 검증이 아닙니다.
선택형 YAML 검사에는 PyYAML이 필요하며 없으면 해당 검사만 skip됩니다. 실제 provider의 config 검사는 `doctor/up`에 별도로 있습니다.

코드는 `mvp_app/core.py`(업무/재시도 순서) → `adapters.py`(DB 호출) → `worker.py`(전달/반영) →
`api.py`, `index.html`(API/화면) → `tools/manage.py`(실습 관리) 순서로 읽으면 됩니다.
상세 검증 범위는 `docs/VALIDATION.md`, 기록용 빈 양식은 `docs/DRILL-NOTES.md`에 있습니다.

## 공식 계약 참고

[S1] Confluent Python client: delivery callback과 수동 offset commit.
https://docs.confluent.io/kafka-clients/python/current/overview.html

[S2] Elasticsearch Index API: 명시적 document ID 및 external versioning. 코드에서 쓰는 기본 API 동작을 확인하는 참고이며
이 문서의 버전과 고정 이미지 버전은 다릅니다. 고정 이미지 실기동 확인은 별도입니다.
https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-index_.html

[S3] Redis SET: EX 기반 TTL.
https://redis.io/docs/latest/commands/set/

[S4] Elastic version EOL 정책.
https://www.elastic.co/support/eol

[S5] MariaDB 공식 이미지: 첫 초기화용 database/user/password 환경 변수. 기존 데이터가 있는 볼륨의 암호 변경과 구분.
https://mariadb.com/docs/server/server-management/automated-mariadb-deployment-and-administration/docker-and-mariadb/mariadb-server-docker-official-image-environment-variables

[S6] Confluent 7.9 이미지 설정: KRaft ID, listener, 단일 노드 복제 계수. Combined mode는 로컬 실험용.
https://docs.confluent.io/platform/7.9/installation/docker/config-reference.html

[S7] Docker Compose up: 컨테이너 재생성과 기존 마운트 볼륨 보존의 구분.
https://docs.docker.com/reference/cli/docker/compose/up/

## v6 통합 실제환경 검사

`./lab.sh verify plan core`로 계획을 보고 `./lab.sh verify run core --yes`로 컨테이너/소스/직접 의존성/DB metadata → smoke → baseline을 검사합니다. 필요한 빌드·기동은 자동 실행하지 않습니다. 기존 `.env/.state/volume`을 보존하고 전체 소스를 반영해야 합니다.

새 이미지에는 build manifest가 포함되며 호스트와 실행 이미지가 다르면 거부합니다. 후보 버전을 제외한 전체29개는 `verify run all --yes`로 묶습니다. 네트워크 helper는 `drills prepare --yes`로 별도 준비합니다. [RUNTIME-ACCEPTANCE.md](docs/RUNTIME-ACCEPTANCE.md)의 지원 환경·판정·제한을 먼저 확인하세요. 새 실환경 검사 구현은 포함됐지만 이번 제작 환경에서 Docker/실제 DB 인수는 통과하지 않았습니다.
