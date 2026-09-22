# DB Lab 배포 진단 리포트 (2026-09-21)

이 문서는 현재 저장소(`146f645 Apply db-lab fixed v12`)를 실제로 배포해 보고,
**무엇이 배포·기동·동작하지 않는지**를 실행 증거와 함께 정리한 리포트입니다.
수정은 하지 않았고, 재현 명령과 권고안만 기록합니다.

## 1. 진단 환경

| 항목 | 값 |
| --- | --- |
| 호스트 | Fedora Linux 43 (Server), 24 vCPU / 125 GiB RAM / 디스크 여유 301 GiB |
| Docker | 29.6.2, `dockerd` (`unix:///var/run/docker.sock`, systemd) |
| Podman | 5.8.4 (컨테이너 없음) |
| podman-compose | **미설치** |
| docker compose | v5.3.1 (플러그인, `podman compose`가 이를 백엔드로 사용) |
| Helm / kubectl | 설치됨 (`~/.local/bin`) |
| Ansible | 2.18.18rc1 |
| Kubernetes | 컨텍스트 `k3d-board-msa`는 있으나 API 서버 연결 불가(`0.0.0.0:6550` connection refused) |

> 주의: 이 셸(비대화형)에는 `DOCKER_HOST`가 없습니다. 대화형 셸에서는
> `~/.bashrc`의 `docker()` 래퍼가 `DOCKER_HOST=unix:///var/run/docker.sock`를
> export합니다. 이 차이가 아래 3번 항목의 원인입니다.

## 2. 실행 결과 요약

| 배포 경로 | 명령 | 결과 |
| --- | --- | --- |
| MVP 통합 스택 | `./all.sh mvp up` | **실패** (`Engine selector changed`) |
| MVP 진단/스모크 | `./all.sh mvp diagnose` / `smoke` | **실패** (가드 차단, `DOCKER_HOST` 지정 시 스모크 실패) |
| 공통 4개 HA 랩 | `./all.sh up` | **실패** (preflight: kafka/redis `podman-compose` 없음) |
| Elasticsearch 랩 | `./all.sh es up` | **성공** (5노드 green) |
| MariaDB HA 랩 | `./all.sh mariadb up` | **실패** (종료코드 1, 단 클러스터 자체는 정상 기동) |
| Kafka HA 랩 | `./all.sh kafka up` | **실패** (`podman-compose` 필요) |
| Redis HA 랩 | `./all.sh redis up` | **실패** (`.env` 미지원 키 + `podman-compose` 필요) |
| Kubernetes/Helm | `helm lint`, `helm template` | **성공** (렌더/린트 OK, 실제 배포는 클러스터 없음) |
| Ansible | `bash scripts/test-ansible.sh` | **성공** (컨트롤러 fixture 한정) |
| 루트 self-test | `./all.sh self-test` | **성공** (51 tests OK) |
| MVP 단위 테스트 | `./all.sh mvp test` | **성공** (862 tests OK) |

`./all.sh health` 결과: `elasticsearch: READY`, `mariadb: READY`,
`kafka: NOT READY`, `redis: NOT READY`.

---

## 3. 실패 항목 상세

### [치명] 3-1. MVP Kafka 컨테이너 무한 재시작 — cp-kafka 7.9.0 / KAFKA-18281

**증상**

```
db-lab-mvp-kafka-1   Restarting (1) ...   docker.io/confluentinc/cp-kafka:7.9.0
```

로그:

```
===> Using provided cluster id 6Hv2TISzSEKW85lQ2Gt2tw ...
Exception in thread "main" java.lang.IllegalArgumentException:
  requirement failed: advertised.listeners cannot use the nonroutable
  meta-address 0.0.0.0. Use a routable IP address.
  at kafka.server.KafkaConfig.validateValues(KafkaConfig.scala:1022)
  at kafka.tools.StorageTool$...
```

**근거 파일**: `mvp-lab/compose.yaml:56`

```yaml
KAFKA_LISTENERS: PLAINTEXT://0.0.0.0:9092,CONTROLLER://0.0.0.0:9093
KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
```

**원인**

`confluentinc/cp-kafka:7.9.0`은 Apache Kafka **3.9.0**을 포함합니다.
Kafka 3.9.0은 KIP-853 도입 과정에서 KRaft `kafka-storage format` 단계의
listener 검증이 잘못되어(KAFKA-18281), **advertised에 없던 controller listener
(`CONTROLLER://0.0.0.0:9093`)** 까지 "nonroutable 0.0.0.0"으로 판정해
포맷을 거부합니다. `advertised.listeners` 값 자체(`PLAINTEXT://kafka:9092`)는
정상입니다.

재현(네트워크/DNS가 정상인 컨테이너에서도 동일):

```
docker run --rm --network db-lab-mvp_default --entrypoint sh \
  -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://kafka:9092 \
  -e KAFKA_LISTENERS=PLAINTEXT://0.0.0.0:9092,CONTROLLER://0.0.0.0:9093 ... \
  confluentinc/cp-kafka:7.9.0 \
  -c 'getent hosts kafka; /etc/confluent/docker/configure; \
      kafka-storage format --cluster-id=... -c /etc/kafka/kafka.properties'
# -> advertised.listeners cannot use the nonroutable meta-address 0.0.0.0
```

**검증된 회피책** — 리스너를 명시적 `0.0.0.0` 대신 암시적 바인딩(`:port`)으로:

```
KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093
```

동일 조건에서 `Formatting metadata directory ... with metadata.version 3.9-IV0.` 로
성공함을 확인했습니다. 대안은 이미지 `7.8.x`(Kafka 3.8.x)로 내리는 것입니다.

> 참고: `mvp-lab/tests/test_compose.py:34`는 `KAFKA_ADVERTISED_LISTENERS`만
> 검사하므로 이 결함을 잡지 못합니다. 862개 단위 테스트가 모두 통과해도
> 실기동은 실패합니다.

### [치명] 3-2. MVP 이벤트 파이프라인 정지 (Kafka 다운의 파급)

Kafka가 뜨지 못하므로 worker가 outbox 이벤트를 relay하지 못합니다.

- worker 로그(반복): `stage=relay_retry dependency=mariadb_or_kafka error=RuntimeError`
- `mvp diagnose` (DOCKER_HOST 지정 시):

```json
"mariadb":  { "reachable": true, "orders": 1, "outbox_pending": 1, "oldest_pending_seconds": 75 },
"redis":    { "reachable": true, "ping": true },
"elasticsearch": { "reachable": false, "http_status": 404, "error_type": "index_not_found_exception" },
"kafka":    { "reachable": false, "error": "KafkaException" },
"dependencies_reachable": false
```

- `mvp smoke`:

```
{"passed": false, "detail": "Search did not catch up. order_id=... last_error=HTTP 503; inspect outbox and lag"}
```

즉 주문 저장(MariaDB)·캐시(Redis)는 살아 있지만,
**이벤트 전달(Kafka) → 검색(Elasticsearch) 경로가 완전히 멈춰** 있습니다.
검색 인덱스(`mvp-orders-v1`)가 없어 ES 조회는 404입니다.
`docker ps`상 API/worker가 `Up (healthy)`로 보여도 end-to-end는 동작하지 않습니다
(API healthcheck는 MariaDB 연결만 확인).

### [높음] 3-3. MVP 엔진 가드가 lifecycle/진단 명령을 차단

**증상**

```
$ ./all.sh mvp up
MVP ERROR: Engine selector changed; restore the original environment
$ ./all.sh mvp diagnose
MVP ERROR: Engine selector changed; restore the original environment
```

`up`, `down`, `stop`, `resume`, `recreate`, `experiment`, `sql`, `smoke`,
`diagnose`, `bind-target`이 모두 차단됩니다.

**원인**

`.state/identity.json`의 `_engine_selector`(24시간 전 생성)는
`DOCKER_HOST=unix:///var/run/docker.sock`가 설정된 **대화형 셸**에서 계산된 값입니다.
현재 비대화형 셸은 `DOCKER_HOST`가 비어 있어 selector 해시가 달라집니다.

```
saved   : 01f72496...cab57   (= DOCKER_HOST=unix:///var/run/docker.sock)
current : bca0652b...7c26b   (= DOCKER_HOST unset)
```

`guard_target()` (`mvp-lab/tools/manage.py:245-255`)이 이 차이를 "환경 변경"으로
판정해 모든 lifecycle을 거부합니다.

**검증**: `DOCKER_HOST=unix:///var/run/docker.sock`를 export하면
`mvp diagnose`/`mvp smoke`가 정상 진입합니다(그 후 3-2로 실패).

`v12`에서 `diagnose`/`smoke`에 `guard_target()` 호출(`tools/http_target.py:15`)이
추가되면서, 과거에는 동작하던 진단 명령까지 같은 상태에서 막히게 되었습니다.

### [치명] 3-4. 공통 `up` preflight 실패 — podman-compose 부재

```
$ ./all.sh up
ERROR: kafka: podman-compose is missing
ERROR: redis: podman-compose is missing
Preflight: FAIL
ROOT_EXIT=1
```

`scripts/control.py:44`는 redis/kafka의 엔진을 **`podman`으로 하드코딩**합니다
(`if project in ('redis','kafka'): return 'podman'`). 따라서 `.env`에
`CONTAINER_ENGINE=docker`가 있어도 무시되고, `podman-compose`가 없으면
공통 `up`은 **아무 랩도 시작하지 않고** 전체를 실패 처리합니다.

### [치명] 3-5. Kafka HA 랩 — podman-compose 필수

```
$ ./all.sh kafka up
ERROR: podman-compose 명령이 필요합니다. README.md의 설치 절차를 확인하세요.
```

`kafka-lab/lab.sh:207`에서 `need podman; need podman-compose`를 요구하고,
`--in-pod`를 지원하는 podman-compose 1.x만 허용합니다(`lab.sh:210`).
Docker만으로는 기동할 수 없습니다.

### [높음] 3-6. Redis HA 랩 — `.env` 미지원 키로 즉시 실패

```
$ ./all.sh redis up
ERROR: Unknown .env keys: CONTAINER_ENGINE
```

`redis-lab/.env`에는 `CONTAINER_ENGINE=docker`가 있고 주석에도
"Use CONTAINER_ENGINE=docker to run the lab with Docker Compose."라고 되어 있지만,
`redis-lab/scripts/manage.py:105-106`은 `.env.example`에 없는 키를 거부합니다.
`.env.example`에는 `CONTAINER_ENGINE`이 없습니다(`grep` 결과 0건).
그 결과 `load_env()`가 실패해 **redis-lab의 거의 모든 명령이 즉시 중단**됩니다.
(설령 이 키를 제거해도 3-5와 같이 `podman-compose`가 필요합니다.)

### [중간] 3-7. MariaDB HA `up` — 프록시 준비 레이스로 종료코드 1

**증상** (2회 재현)

```
 Container mariadb-ha-proxy Started
 Container mariadb-ha-task-xxxxxxxx Creating
ERROR: (2003, "Can't connect to MySQL server on 'proxy' ([Errno 111] Connection refused)")
Run receipt: ... exit_code: 1
```

그러나 곧바로 `./all.sh mariadb status`는 전 노드 정상:

```
galera1 Primary Synced 3 True
galera2 Primary Synced 3 True
galera3 Primary Synced 3 True
```

그리고 `tools check`를 수동 실행하면 성공합니다.

**원인**

`mariadb-ha-lab/scripts/lab.py:364-390`의 `up()`은 proxy/dashboard를
`--force-recreate`로 올린 직후 `wait_frontends()`(`lab.py:324-341`)에서
`docker compose run ... tools check`를 실행합니다. HAProxy는
`resolvers`+HTTP 헬스체크(`hold valid 5s`, `inter 2s fall 2 rise 2`)가 끝나야
백엔드를 라우팅하므로, `tools check`의 재시도 창(최대 12회)보다 준비가 늦으면
`Connection refused`로 실패합니다. `wait_frontends()`는 proxy 준비를 먼저
기다리지 않고 바로 SQL 체크를 돌리는 순서 문제입니다.

### [정보] 3-8. Kubernetes/Helm 배포 불가 (환경)

- `helm lint helmchart`: 성공 (`1 chart(s) linted, 0 failed`)
- `helm template db-lab helmchart`: 성공 (507 lines)
- `./all.sh k8s preflight`: 허용 컨텍스트가 아니면 거부(설계),
  `DB_LAB_ALLOWED_CONTEXTS`를 맞춰도 kubectl이 연결 실패
  (`kubectl --context failed`). 즉 **살아있는 실습용 클러스터가 없어**
  실제 `k8s up`은 검증 불가입니다.

---

## 4. 정상 동작한 항목

- **Elasticsearch 랩**: `./all.sh es up` → 5개 노드(es01~es05) 기동,
  `active_shards_percent 100.0`, Kibana/Cerebro 기동. `health: elasticsearch READY`.
- **MariaDB HA 클러스터 자체**: galera1~3 `Primary/Synced` 3노드, proxy/dashboard
  healthy (`up` 종료코드만 1, 3-7 참조).
- **루트 컨트롤러 self-test**: 51 tests OK.
- **MVP 단위 테스트**: 862 tests OK (실기동 실패를 잡지 못함).
- **Ansible**: `test-ansible.sh` 10 tests OK (fixture 한정, SSH/DB 실기동 아님).
- **Helm 렌더/린트**: OK.

## 5. 우선순위 권고

1. **(즉시) MVP Kafka 리스너 수정**: `mvp-lab/compose.yaml:56`을
   `PLAINTEXT://:9092,CONTROLLER://:9093`로 바꾸거나 `cp-kafka:7.8.x`로 고정.
   `test_compose.py`에 `KAFKA_LISTENERS`에 `0.0.0.0` 금지 검사를 추가.
2. **(즉시) MVP 엔진 가드 완화/문서화**: `DOCKER_HOST` 유무로 selector가 바뀌지
   않도록 정규화(예: 기본 소켓을 명시값으로 정규화)하거나,
   `.state` 초기화/`bind-target` 안내를 배포 문서에 명시.
   비대화형(CI) 실행 시 `DOCKER_HOST`를 명시하도록 가이드.
3. **(즉시) podman-compose 의존 정리**: `scripts/control.py:44`의
   redis/kafka 엔진 하드코딩을 제거하고, Docker Compose 경로를 지원하거나
   최소한 preflight 실패 시 사유/설치 안내를 더 명확히. 또는
   `kafka-lab`/`redis-lab`에 Docker Compose 백엔드 추가.
4. **(높음) redis-lab `.env` 스키마 정합성**: `CONTAINER_ENGINE`을
   `.env.example`에 추가하고 `validate_env`/`load_env`에서 허용(또는
   `.env`에서 제거). `.env.example`과 실제 지원 키가 일치하도록 테스트 추가.
5. **(중간) MariaDB `wait_frontends` 순서 수정**: `tools check` 전에
   proxy `/stats` 및 백엔드 health가 ready 될 때까지 대기하거나,
   `tools check` 재시도 창을 늘리고 실패를 명확히 분류.
6. **(선택) `diagnose`/`smoke`의 `guard_target` 적용 재검토**: 진단 목적의
   읽기 명령까지 엔진 selector에 묶을 필요가 있는지 정책 확인.

## 6. 현재 남은 상태

- MVP 6개 컨테이너: 5개 Up(healthy), **kafka Restarting 루프**,
  worker relay_retry 반복, 검색 인덱스 없음.
- Elasticsearch 랩(5노드+Kibana+Cerebro), MariaDB HA 랩(galera1~3+proxy+dashboard)
  기동 상태로 남겨 둠(사용자가 확인 가능).
- Kafka/Redis HA 랩: 미기동(podman-compose 없음).

## 7. 재현 명령

```bash
# MVP (대화형 셸이 아니면 DOCKER_HOST 명시)
cd /home/server/Documents/db-lab
DOCKER_HOST=unix:///var/run/docker.sock bash ./all.sh mvp diagnose
DOCKER_HOST=unix:///var/run/docker.sock bash ./all.sh mvp smoke
docker logs db-lab-mvp-kafka-1 | grep -m1 advertised.listeners

# Kafka 3.9.0 리스너 버그 단독 재현/회피 검증
docker run --rm --network db-lab-mvp_default --entrypoint sh \
  -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://kafka:9092 \
  -e KAFKA_LISTENERS=PLAINTEXT://0.0.0.0:9092,CONTROLLER://0.0.0.0:9093 \
  -e KAFKA_PROCESS_ROLES=broker,controller -e KAFKA_NODE_ID=1 \
  -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@kafka:9093 \
  -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
  -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
  -e KAFKA_INTER_BROKER_LISTENER_NAME=PLAINTEXT -e COMPONENT=kafka \
  confluentinc/cp-kafka:7.9.0 -c '/etc/confluent/docker/configure; \
    kafka-storage format --cluster-id=6Hv2TISzSEKW85lQ2Gt2tw -c /etc/kafka/kafka.properties'

# 공통/개별 배포
bash ./all.sh preflight
bash ./all.sh up
bash ./all.sh kafka up
bash ./all.sh redis up
bash ./all.sh mariadb up
bash ./all.sh es up
```

### 참고 (외부 원인 근거)

- Apache Kafka KAFKA-18281: https://issues.apache.org/jira/browse/KAFKA-18281
- confluentinc/kafka-images #373: https://github.com/confluentinc/kafka-images/issues/373
