# MariaDB HA / Galera 실습 환경

> **2026-09-17 수정본** — [수정 내역·검증 결과](docs/FIXES-VALIDATION.md)를 먼저 확인하세요.
> 쉘 9개/Python 14개 문법 검사와 호스트 테스트 182개는 통과했습니다.
> **실제 MariaDB/Galera 컨테이너 검사는 런타임 미설치로 BLOCKED이며 완료했다고 주장하지 않습니다.**
> 수정 코드 적용: 기존 `.env`·`.state`·볼륨을 유지하고 `bash lab.sh down → build → up` 순서로 실행하세요.
> 호스트 검사는 `bash tests/validate.sh`, 실제 실습 검사는 `RUN_DISRUPTIVE_TESTS=1 bash tests/run-real.sh`입니다.
> 실제 실습 검사는 합성 데이터 교체·노드 중단/재구축을 포함하므로 폐기 가능한 실습에서만 실행하세요.


## 새 기능: 슬로우쿼리·단편화·장애 자동 실습

```

개별 SQL lab도 파일 경로 없이 목록과 이름으로 실행할 수 있습니다.

```bash
./lab.sh labs
./lab.sh run-lab 01-health galera1
./lab.sh run-lab 08-auto-increment galera1 --allow-write
```

`labs`는 읽기 전용/쓰기 lab을 구분해 보여주며, 쓰기 SQL은 실수 방지를 위해
`--allow-write` 없이는 실행하지 않습니다. lock/deadlock처럼 두 세션이 필요한 lab은
각각의 터미널에서 같은 명령을 실행해야 합니다.bash
# 기존 3노드가 정상 실행 중일 때, 별도 commerce 데이터 적재 불필요
./lab.sh scenario demo --rows 5000 --hold 20

# 개별 실행
./lab.sh scenario slow
./lab.sh scenario fragmentation
./lab.sh scenario node-failure
./lab.sh scenario node-hang
./lab.sh scenario lock
./lab.sh scenario report
```

상세 사용법은 [docs/SCENARIOS.md](docs/SCENARIOS.md), 새 코드의 검증 범위는
[docs/SCENARIOS-VALIDATION.md](docs/SCENARIOS-VALIDATION.md)를 보세요.
`--leave-broken`으로 문제 상태를 유지하고 `scenario fix`로 개선할 수도 있습니다.
새 합성 데이터는 `incident_lab`에만 생성합니다. 장애 종료 시 자동 복구를 시도하며,
중단 기록이 남았으면 `./lab.sh scenario repair`를 사용하세요.
**quorum은 `--confirm-quorum`으로 따로 승인해야 하며 demo에는 포함되지 않습니다.**

---


MariaDB를 **SQL, InnoDB 내부 동작, 복제, 장애 전환, 재동기화, 전체 중단 복구, 백업 복원**까지 연결해서 공부하기 위한 로컬 실습 프로젝트입니다.

**용어 구분:** InnoDB는 스토리지 엔진입니다. **MySQL InnoDB Cluster**는 MySQL Group Replication + MySQL Shell + MySQL Router를 사용하는 MySQL 제품 구성입니다. 여기서는 MariaDB에 맞는 **Galera Cluster**를 구축합니다. MySQL InnoDB Cluster 자체를 설치하거나 두 제품이 같은 구현이라고 가정하지 않습니다.

> 이 패키지에서 수행한 검증과 아직 수행하지 못한 검증은 `docs/VALIDATION.md`에 구분했습니다. 제작 환경에는 Podman/Docker와 MariaDB 실행 파일이 없어 실제 이미지 빌드·SST·장애 전환의 종단 간 실행은 하지 못했습니다. 실행 결과를 조작한 모의 DB가 아니라 실제 MariaDB/Galera를 시작하는 구성입니다. 첫 실행 후 `./lab.sh verify`로 본인 호스트에서 확인하세요.

## 1. 구성

```text
Linux 호스트: Podman Compose 우선 / Docker Compose 대안

애플리케이션·CLI·CloudBeaver
       │
       ├─ 13306 → HAProxy writer → galera1 우선, 장애 시 galera2 → galera3
       └─ 13307 → HAProxy reader → 준비된 노드에 신규 연결 round-robin

   galera1  ←──────── Galera write-set replication ────────→ galera2
       ↖───────────────── galera3 ─────────────────────────↗
       각각 독립 InnoDB 데이터 볼륨 / 같은 데이터의 전체 복사본

   각 노드의 내부 :9200 → 실제 wsrep 상태 검사
       ├─ HAProxy /ready 검사
       └─ 읽기 전용 대시보드 → 호스트 18081

   선택: CloudBeaver 18080
   선택: 복원 전용 standalone MariaDB 13309 (별도 네트워크·별도 볼륨)
```

단일 호스트 안에서 여러 컨테이너로 프로세스/네트워크 장애를 학습합니다. **물리 서버 3대나 가용영역 3개를 대체하는 운영 HA는 아닙니다.** HAProxy도 한 인스턴스이므로 프록시 자체의 HA까지 제공하지 않습니다.

**새 랩은 기존 `mariadb-learning`, 기존 3306/8080, 기존 데이터 볼륨을 사용하지 않습니다.** 첨부 프로젝트는 `standalone-original/`에 원본 파일 그대로 보존했습니다. 그 폴더의 예전 reset 명령은 새 Galera 랩 관리 명령이 아닙니다.

## 2. 바로 실행

Linux 호스트에 Python 3.9+, Podman + podman-compose 또는 Docker + Compose가 필요합니다. Python 외의 호스트 라이브러리는 필요 없습니다. 이미지 내려받기와 최초 빌드에는 인터넷 연결이 필요합니다.

```bash
unzip mariadb-ha-lab.zip
cd mariadb-ha-lab
chmod +x lab.sh

# .env와 서로 다른 무작위 비밀번호 생성
./lab.sh init

# 런타임·Compose·포트·디스크 확인
./lab.sh doctor

# DB/프록시 이미지 빌드 → 안전한 최초 bootstrap → 2·3번 순차 join
./lab.sh up

# 데이터 자동 생성·분할 적재. 처음에는 small, 익숙해지면 standard도 가능
./lab.sh seed --size standard

# 3노드/UUID/복제 데이터/데이터 행 수/접속 경로/readonly 권한 검증
./lab.sh verify
./lab.sh status
```

**처음부터 `podman compose up -d`만 실행하지 마세요.** 빈 Galera 클러스터에는 최초 bootstrap 결정이 필요합니다. `lab.sh up`이 안전성 검사와 순서를 담당하고 실제 컨테이너 관리는 Compose로 수행합니다.

`.env`의 `CONTAINER_ENGINE=auto`는 설치된 Podman을 우선 사용합니다. Docker를 선택하려면 첫 실행 전 `CONTAINER_ENGINE=docker`로 바꾸세요. 같은 디렉터리에서 실행 도중 런타임이나 `LAB_PROJECT`를 바꾸면 잘못된 볼륨을 조작하지 않도록 거절합니다. 다른 랩은 새 디렉터리와 다른 포트를 사용하세요.

### 리소스

기본값은 노드마다 InnoDB buffer pool 256 MiB / Galera gcache 128 MiB입니다. gcache는 주로 디스크 파일이고, buffer pool만으로 DB 프로세스 전체 메모리를 계산할 수는 없습니다. **출발점으로 4 vCPU, 여유 RAM 6~8 GiB, 여유 디스크 10 GiB 이상**을 권합니다. 이는 실측 보장이 아닌 학습용 용량 계획입니다. CloudBeaver, 큰 데이터, 백업·복원, 동시에 구동 중인 Elasticsearch/JVM 랩에는 추가 여유가 필요합니다.

이미지 태그 `mariadb:11.8`, `haproxy:3.0-alpine`, CloudBeaver `latest`는 같은 계열 안에서도 내용이 바뀔 수 있습니다. 실행 성공 후 `podman image inspect`로 digest를 기록하고 `.env`의 이미지 값을 `image@sha256:...`로 고정하면 재현성이 더 좋습니다. 모든 Galera 노드는 같은 빌드 이미지를 사용합니다. `./lab.sh build`는 이미지를 다시 빌드하지만 실행 중 노드의 무중단 업그레이드를 자동 수행하지 않습니다.

운영에 가까운 단일 호스트 재시작 정책이 필요하면 랩 검증과 분리해 다음 overlay를 사용합니다.

```bash
docker compose -f compose.yaml -f compose.production.yaml up -d
```

이 overlay는 컨테이너 재시작과 proxy/dashboard healthcheck만 추가합니다. 다중 호스트 장애 격리,
TLS, secret manager, 원격 백업/PITR, 이미지 digest 고정은 별도로 구성해야 하며 이 프로젝트만으로
운영 배포가 완료되는 것은 아닙니다.

## 3. 접속

| 접속점 | 기본 호스트 주소 | 용도 |
|---|---|---|
| Galera 1 | 127.0.0.1:13301 | 개별 노드 실습 |
| Galera 2 | 127.0.0.1:13302 | 개별 노드 실습 |
| Galera 3 | 127.0.0.1:13303 | 개별 노드 실습 |
| Writer | 127.0.0.1:13306 | 우선 writer + 신규 연결 장애 전환 |
| Reader | 127.0.0.1:13307 | 연결 단위 조회 부하 분산 |
| 상태 대시보드 | http://127.0.0.1:18081 | wsrep/대기열/충돌/락/QPS |
| HAProxy 상태 | http://127.0.0.1:18084/stats | backend UP/DOWN/접속 수 |
| CloudBeaver | http://127.0.0.1:18080 | 선택 SQL 웹 UI |
| 복원 전용 노드 | 127.0.0.1:13309 | 선택, 원본 클러스터와 분리 |

`.env`에서 포트를 바꿀 수 있습니다. 내부 Galera/SQL 포트는 바꾸지 않아도 됩니다. 공유 호스트에서 Elasticsearch가 9200을 사용해도 새 랩의 내부 health 9200은 **호스트에 publish하지 않으므로** 충돌하지 않습니다.

```bash
# 컨테이너 내부 root socket 접속. 별도 호스트 MariaDB client 불필요
./lab.sh sql galera1
./lab.sh sql galera2 "SELECT @@hostname, VERSION();"
./lab.sh sql galera1 < labs/01-health.sql

# 호스트에 MariaDB client가 있는 경우: -p 뒤에 비밀번호를 직접 쓰지 않고 프롬프트 입력
mariadb -h 127.0.0.1 -P 13306 -ulab -p commerce_lab
mariadb -h 127.0.0.1 -P 13307 -ureadonly -p commerce_lab
```

비밀번호는 `.env`의 `LAB_PASSWORD`, `READONLY_PASSWORD`를 확인합니다. `root`는 내부 socket 관리용이며 원격 root 접속을 열지 않았습니다. `lab`은 `commerce_lab`/`lab_ops` 학습용 전체 권한, `readonly`는 두 스키마 조회 권한만 있습니다.

### CloudBeaver

```bash
./lab.sh ui
```

첫 화면에서 CloudBeaver 관리자 계정을 직접 만듭니다. DB의 root 비밀번호와 UI 관리자 비밀번호는 별개입니다. 연결 설정은 다음과 같습니다.

| 연결 이름 예시 | Host | Port | User | Database |
|---|---|---:|---|---|
| writer | proxy | 3306 | lab | commerce_lab |
| readers | proxy | 3307 | readonly | commerce_lab |
| node1 | galera1 | 3306 | lab | commerce_lab |
| node2 | galera2 | 3306 | lab | commerce_lab |
| node3 | galera3 | 3306 | lab | commerce_lab |

CloudBeaver **컨테이너 안에서** `localhost`는 MariaDB가 아니라 CloudBeaver 자신입니다. 위 서비스 이름을 사용하세요. UI가 JDBC 드라이버 추가 다운로드를 요구하면 허용된 인터넷 연결이 필요할 수 있습니다. 실패해도 내장 CLI 실습은 가능합니다.

### 원격 Linux 서버에서 실행할 때

기본 포트는 127.0.0.1에만 열었습니다. Windows PowerShell의 OpenSSH로 터널을 만들면 됩니다.

```powershell
ssh -L 18081:127.0.0.1:18081 -L 18084:127.0.0.1:18084 -L 18080:127.0.0.1:18080 -L 13306:127.0.0.1:13306 server@YOUR_SERVER
```

그 뒤 PC 브라우저에서 `http://localhost:18081`을 엽니다. 기본값을 무심코 `0.0.0.0`으로 바꾸지 마세요. 대시보드·HAProxy 통계에는 별도 인증/TLS가 없습니다.

## 4. 데이터와 SQL 공부

`commerce_lab`에는 고객·주소·카테고리·상품·창고·재고·주문·주문상품·결제·배송·리뷰·상태 이력·파티션 로그·계정 잔액이 있습니다. FK, 복합 인덱스, generated column, JSON, FULLTEXT, view, trigger, 송금 procedure를 포함합니다.

| 주요 테이블 | small | standard | large |
|---|---:|---:|---:|
| 고객 | 5,000 | 50,000 | 100,000 |
| 상품 | 1,000 | 10,000 | 20,000 |
| 주문 | 20,000 | 200,000 | 400,000 |
| 주문상품 | 60,000 | 600,000 | 1,200,000 |
| 결제 | 20,000 | 200,000 | 400,000 |
| 배송 | 18,000 | 180,000 | 360,000 |
| 리뷰 | 12,000 | 120,000 | 240,000 |
| API 로그 | 50,000 | 500,000 | 1,000,000 |

standard는 보조 테이블을 합쳐 **2,040,068행**입니다. 큰 SQL 데이터 파일을 새로 ZIP에 넣지 않고, 시작 후 MariaDB Sequence 가상 테이블에서 결정적 합성 데이터를 생성합니다. 기존 첨부의 Sakila 파일은 원본 보존 폴더에 남아 있습니다.

적재는 기본 500개의 기준 행씩 나눕니다. 주문상품은 기준 주문당 3행, 재고는 상품당 창고 8행이므로 **최종 변경 행 수가 언제나 500행이라는 뜻은 아닙니다.** 거대한 한 번의 INSERT SELECT나 MEMORY helper 데이터 복제에 의존하지 않습니다. FK 검사는 켜 둡니다.

API 로그 JSON에는 기본 256바이트 payload를 추가합니다. 실제 데이터·인덱스 크기는 적재 마지막의 `information_schema.tables` 집계로 확인합니다. 이는 페이지/통계 기준 값이며 파일시스템 실사용량, binlog, gcache, 백업 용량과 다릅니다. 동일 데이터가 각 노드에 복제되므로 노드 수만큼 저장 공간을 계획해야 합니다.

```bash
# 데이터 양/배치/payload 조절
./lab.sh seed --size small --batch 500 --payload-bytes 128

# 이미 적재한 합성 commerce_lab만 새로 만들겠다고 명시한 경우
./lab.sh seed --size standard --replace --confirm-replace
```

`seed` 중 실패하면 `lab_ops.dataset_manifest`에 `loading` 상태가 남습니다. 오류 원인을 고친 뒤 명시적 교체 옵션으로 다시 적재하세요. 기존 사용자 데이터나 운영 DB를 가리키도록 구성하지 마세요.

재현 가능한 시드 프로필도 제공합니다.

```bash
./lab.sh seed --profile smoke
./lab.sh seed --profile balanced
./lab.sh seed --profile skewed-api
./lab.sh seed --profile stress
./lab.sh seed --profile large-replay
```

프로필은 기존 스키마의 실제 행 수·배치·payload 조합을 고정한 preset입니다. `stress`와
`large-replay`는 충분한 디스크와 시간을 확보한 일회용 랩에서만 실행하세요.

실행 중인 commerce dataset에 API 요청 트래픽을 주기적으로 추가하는 시뮬레이터도 지원합니다.
기본 5분 동안 초당 10건을 넣으며, 기존 원본 Galera 볼륨을 삭제하지 않습니다.

```bash
./lab.sh simulate --seconds 1800 --rate 25 --mode mixed --seed 20260918
```

`--mode api`는 API 로그만, `--mode orders`는 기존 주문 조회·상태 변경만,
`--mode mixed`는 API 로그 65%와 주문 작업 35%를 섞습니다. 주문 상태 변경은 기존
트리거를 통해 `order_status_history`도 생성합니다. 결과에는 작업별 카운트와 p50/p95
지연시간이 포함됩니다. 운영 데이터에는 사용하지 말고 시뮬레이션 결과를 보관하세요.

기존 SQL 실습은 `standalone-original/labs/`, 새 클러스터 실습은 `labs/`와 `docs/WORKBOOK.md`를 봅니다. Sakila는 새 클러스터에 자동 적재하지 않고 기존 단일 서버 랩에서 계속 공부하도록 보존했습니다.

## 5. 운영 실습 명령

```bash
./lab.sh status
./lab.sh routes                           # 신규 연결이 실제 어느 노드로 가는지
./lab.sh logs galera1 --tail 100
./lab.sh logs galera2 -f                   # 종료: Ctrl+C

./lab.sh load --seconds 60 --workers 4     # writer 경유 합성 송금
./lab.sh load --seconds 60 --workers 6 --target multi
./lab.sh conflict                         # 서로 다른 노드의 동일 PK 갱신 충돌

./lab.sh kill galera1                     # 강제 장애: 해당 노드만 SIGKILL
./lab.sh start galera1                    # 살아 있는 Primary에 정상 재합류

./lab.sh stop galera3                     # 정상 종료 후 IST 학습
./lab.sh start galera3

./lab.sh rebuild galera3 --confirm-rebuild # 이 랩의 3번 데이터만 지우고 SST 재가입

./lab.sh quorum-demo --confirm-pause      # 2·3번 동시에 도달 불가 상태 시뮬레이션
./lab.sh resume galera2
./lab.sh resume galera3

./lab.sh backup
./lab.sh restore --confirm-restore        # 별도 restore 볼륨만 초기화·복원
./lab.sh sql restore

./lab.sh down                             # 역순 정상 종료, 데이터 유지
./lab.sh up                               # safe_to_bootstrap 확인 후 재기동

# 전체 crash 후: 반드시 모든 DB 컨테이너가 중지된 상태
./lab.sh recover                          # 세 노드 recovered UUID/seqno 비교, 시작은 하지 않음
./lab.sh recover --execute --confirm-recovery
```

`recover`는 매번 실제 `--wsrep-recover`를 다시 수행합니다. 과거 보고서의 숫자를 그대로 신뢰해 bootstrap하지 않습니다. UUID 불일치, 일부 노드 상태 미확인, 음수 위치, 실행 중/paused 노드는 거부합니다. 복구 로그는 `reports/recovery.json`에 남습니다. `--wsrep-recover`는 InnoDB 복구를 수행할 수 있어 **완전한 읽기 전용 작업은 아닙니다.** 중요한 데이터를 대상으로는 먼저 디스크 복사본을 확보해야 합니다.

전체 초기화는 다음과 같이 명시적으로 요청해야 합니다. 기본 `down`은 볼륨을 지우지 않습니다.

```bash
./lab.sh reset --confirm-delete-lab-data
```

이 명령은 **현재 새 랩 프로젝트**의 Galera/restore/UI 볼륨을 삭제합니다. 다른 Compose 프로젝트나 `standalone-original`의 기존 볼륨, 호스트 `backups/`는 삭제하지 않습니다. `podman system prune -a --volumes` 같은 전역 삭제 명령은 사용하지 않습니다.

## 6. 반드시 이해할 차이

### Galera는 샤딩이 아니다

각 노드가 전체 데이터 복사본을 가집니다. Elasticsearch처럼 테이블 데이터를 노드마다 나눠 담는 구성은 아닙니다. 노드 수가 늘어난다고 쓰기 처리량이 자동으로 N배가 되지 않습니다.

### writer는 Galera 리더가 아니다

이 랩의 writer는 **프록시의 우선 경로**입니다. Galera 자체는 여러 노드에서 쓰기를 받을 수 있습니다. HAProxy가 SQL을 분석해 SELECT와 UPDATE를 자동 분류하는 구성도 아닙니다. 조회는 reader 포트와 readonly 사용자로 명시적으로 분리합니다.

기존 DB 연결이나 진행 중 트랜잭션을 다른 노드에 그대로 이사시키지 않습니다. 장애 시 클라이언트 재접속이 필요하며, COMMIT 응답 유실은 실제 커밋 여부가 불명확할 수 있습니다. 부하 스크립트는 같은 request_id 재사용과 receipt 조회로 중복 적용을 피하도록 만들었습니다. 이것이 모든 애플리케이션의 exactly-once를 자동 보장한다는 뜻은 아닙니다.

복구된 galera1은 **새 연결**의 우선 대상이 됩니다. 기존 galera2 연결은 그대로 남을 수 있습니다. 따라서 이 프록시 정책은 전체 접속에 대한 엄격한 single-writer fencing을 제공하지 않습니다.

### 정상 종료와 장애는 다르다

3개 중 2개를 **정상적으로 순차 종료**하면 membership이 줄어 마지막 1개가 Primary를 유지할 수도 있습니다. 이것으로 quorum 보호가 없다고 판단하면 안 됩니다. 두 노드 도달 불가 실습은 `quorum-demo`의 pause를 사용합니다. 설정된 failure detection이 끝날 때까지 상태 변화에는 지연이 있습니다.

### virtually synchronous ≠ 다른 노드의 모든 SELECT가 즉시 최신

복제 write-set의 순서·인증과 실제 원격 apply 완료는 구분해야 합니다. 다른 노드의 읽기 가시성을 비교할 때 `SET SESSION wsrep_sync_wait=1`을 사용합니다. 이 설정이 모든 격리 수준 문제를 해결하거나 전역 serializability를 제공하는 것은 아닙니다.

### 복제는 백업이 아니다

실수로 삭제한 행도 복제됩니다. `backup`은 논리 백업이며 SST용 mariadb-backup 물리 전송과 다릅니다. 현재 백업 명령은 자동 PITR을 위한 binlog 좌표·연속 보관을 구성하지 않습니다. 복원 노드에서 dump 복원과 조회를 검증하는 범위입니다.

## 7. 파일 안내

```text
compose.yaml                실제 다중 컨테이너 구성
.env.example                포트·이미지·메모리 설정 예시
lab.sh / scripts/lab.py      기동·검증·장애·복구·데이터·백업 CLI
images/node/                공식 MariaDB 기반 이미지와 Galera 시작 래퍼
images/proxy/               실제 Galera readiness를 검사하는 HAProxy
scripts/health.py           노드 내부 /ready, /status, /metrics
scripts/dashboard.py/html   읽기 전용 통합 상태 화면
scripts/workload.py         합성 송금·재시도·multi-writer 충돌 실습
scripts/seed.py             분할 SQL 생성기
scripts/state.py            중지 노드 상태 확인·복구·재가입 유틸리티
datasets/                   commerce 스키마/템플릿, 별도 lab_ops
labs/                       복사해서 실행할 SQL
docs/WORKBOOK.md             실습 단계·예상 결과·확인할 로그
docs/TROUBLESHOOTING.md      Podman·SELinux·포트·SST·quorum 문제 해결
docs/VALIDATION.md           검증 범위와 실행하지 못한 항목
docs/SOURCES.md              공식 근거 문서
standalone-original/        첨부한 기존 프로젝트 원본
backups/                    사용자가 생성한 논리 백업
reports/                    복구/검증 보고서
```

## 8. 안전성과 한계

새 DB/프록시 설정과 초기화 스크립트는 이미지에 COPY하고 데이터는 named volume을 사용합니다. 호스트 bind mount에 의존하지 않아 기존에 겪을 수 있는 `initdb/: Permission denied`, SELinux 공유 라벨, rootless UID 매핑 문제를 줄입니다. **SELinux를 끄거나 privileged/host networking을 요구하지 않습니다.** 새 이미지 파일을 바꿨으면 `./lab.sh build`로 다시 빌드해야 하며, DB 설정 변경은 적용 대상 노드의 재생성 계획도 필요합니다.

이것은 로컬 학습용입니다. SQL·Galera·SST 내부 통신에 TLS를 설정하지 않았고, DB 비밀번호는 `.env` 및 컨테이너 환경변수에 있습니다. SST auth는 프로세스 인자로도 보일 수 있습니다. 컨테이너 관리자에게 숨기는 비밀 저장소가 아닙니다. 운영에는 분리 호스트/AZ, 프록시 HA, 네트워크 ACL, SQL/Galera/SST 각각의 암호화, 비밀 관리, 보존·복구 정책, 모니터링과 업그레이드 검증이 추가로 필요합니다.
