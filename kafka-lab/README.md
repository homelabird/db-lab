# Kafka + ZooKeeper 학습 Lab

**ZooKeeper 3개 + Kafka broker 3개 또는 KRaft combined cluster를 Podman Compose로 구성하는 로컬 실습 프로젝트입니다.**

카프카 기본 송수신, 파티션/복제/ISR, 컨슈머 그룹/오프셋/lag를 학습한 다음 직접 장애를 넣고 복구할 수 있습니다. 데이터 파일은 들어 있지 않습니다. 실행할 때 Python 생성기가 합성 JSON 이벤트를 만들어 실제 Kafka producer로 전송합니다.

각 장애 시나리오의 dev 재현과 staging/prod 읽기 전용 진단 명령은 [시나리오별 대응 쿼리](../docs/SCENARIO-RESPONSE-QUERIES.md#kafka-failure-scenarios)를 참고하세요.

> **검증 범위:** 제작 환경에서 75개 단위·모의 테스트 및 Bash/YAML 검사가 통과했습니다. 이 환경에는 Podman/Docker가 없어 이미지 다운로드, 실제 Kafka 기동·장애 전환, UI 접속은 실행하지 못했습니다. 실제 실행 검증용 `smoke`와 `scripts/test-live.sh`가 포함되어 있습니다. 자세한 내역은 `reports/VALIDATION.md`에 있습니다.

## 2026-09-21 실행 수정

가변 노드 기동의 고정 `--brokers 3`을 실제 `NODES`로 수정했습니다. 최초 기동 시 선택한
`NODES`, `KAFKA_MODE`, `LAB_NAME`은 `.env`에 저장되고, `.state/active-topology.json`으로 고정됩니다.
후속 `health`, `status`, `down`은 같은 구성을 사용합니다. 다른 기존 `.env` 값과 KRaft ID는 보존합니다.
이미 기동한 구성을 다른 노드 수/모드로 덮어쓰는 요청은 거부합니다. `down`은 볼륨과 구성을 유지합니다.
다른 토폴로지는 별도의 실습 디렉터리와 `LAB_NAME`을 사용하세요. 기존의 명시적 `reset --yes`가
성공한 경우에만 고정을 해제합니다. **reset은 데이터를 삭제하므로 연결 오류 해결책으로 쓰지 마세요.**

이번 호스트 검사와 미검증 범위는 [통합 수정 기록](../docs/STARTUP-REPAIR-2026-09-21.md)에 있습니다.
위의 75개 테스트라는 문구는 초기 버전의 기록입니다.

## 1. 구성과 버전

```text
                             선택적 읽기 전용 Kafka UI :8088
                                           |
데이터 생성기/CLI ──────────────── Kafka API ─┤
                                           |
                           +---------------+---------------+
                           |               |               |
                        kafka1          kafka2          kafka3
                       broker.id=1     broker.id=2     broker.id=3
                           |               |               |
                           +---------------+---------------+
                                           |
                              ZooKeeper 연결/메타데이터
                                           |
                                 zk1 ── zk2 ── zk3
                                 3-node ensemble
```

메시지 본문은 **Kafka broker**가 저장합니다. ZooKeeper를 통해 메시지를 중계하는 구조가 아닙니다. ZooKeeper 모드에서는 Kafka broker 중 하나가 Kafka controller 역할도 담당합니다. ZooKeeper 자체의 leader와 Kafka controller는 다른 역할입니다. [S2, S3, S5]

| 항목 | 이 Lab의 설정 |
|---|---|
| Kafka 이미지 | `docker.io/confluentinc/cp-kafka:7.9.0` |
| ZooKeeper 이미지 | `docker.io/confluentinc/cp-zookeeper:7.9.0` |
| Kafka 계열 | Apache Kafka 3.9 계열 / `KAFKA_MODE=zk` 또는 `kraft` |
| Python 클라이언트 | Python 3.12, `confluent-kafka==2.8.2` |
| 선택 UI | `ghcr.io/kafbat/kafka-ui:v1.3.0`, 읽기 전용 |
| 일반 토픽 복제 | `replication.factor=3`, `min.insync.replicas=2` |
| 기본 producer | `acks=all`, idempotence 활성화, delivery callback 확인 |
| 데이터 보존 | 일반 토픽 24시간, segment 16MiB |
| 데이터 볼륨 | Kafka 3개 + ZooKeeper data/log 각 3개 = named volume 9개 |
| 보안 | **인증/TLS 없음. 기본 호스트 바인딩은 127.0.0.1.** |

CP 7.9.x는 Kafka 3.9.x 계열이며 ZooKeeper 구성을 지원합니다. Kafka 4.0에서는 ZooKeeper 모드가 제거되었으므로 `latest`나 CP 8.x로 바꾸면 안 됩니다. 핵심 이미지는 학습용 고정 태그를 선택했으며, 최신 보안 패치를 보장하는 선택이 아닙니다. 운영 환경에 그대로 배포하지 마세요. [S1, S2]

기본 모드는 기존 실습과 호환되는 `KAFKA_MODE=zk`입니다. KRaft를 사용하려면 `.env`에서 `KAFKA_MODE=kraft`로 바꾸거나 `KAFKA_MODE=kraft ./lab.sh up`을 실행합니다. KRaft는 3개 노드가 모두 `broker,controller`인 학습용 combined mode입니다. ZooKeeper와 KRaft는 Compose 파일·볼륨·metadata 저장소가 분리됩니다.

노드 수는 Compose 파일에 고정되어 있지 않습니다. `.env`의 `NODES` 또는 명령 옵션으로 지정하면 `lab.sh`가 실행 직전에 `.state/compose.generated.yaml`을 렌더링합니다. 따라서 broker bootstrap, listener 포트, ZooKeeper ensemble, KRaft voter, volume, 복제 계수가 같은 노드 수로 생성됩니다.

```bash
# 5-node ZooKeeper cluster
./lab.sh up --nodes 5

# 3-node KRaft cluster: 위 ZooKeeper 예시와 별도의 신규 실습 디렉터리에서 실행
KAFKA_MODE=kraft ./lab.sh up --nodes 3
```

ZooKeeper는 quorum 특성상 홀수 노드 수를 사용해야 합니다. 현재 `1..100` 노드까지 Compose를 생성할 수 있으며, 따라서 ZooKeeper 모드에서는 홀수 노드 수를 사용하고 KRaft 모드에서는 10·20·100 같은 구성도 생성할 수 있습니다. Kafka replication factor는 `min(NODES, 3)`으로 설정되고 1노드에서는 `min.insync.replicas=1`로 조정됩니다. 이 옵션은 신규 토폴로지 선택용이며 기존 클러스터의 무중단 확장 기능이 아닙니다. 이미 기동한 토폴로지 변경은 위의 고정 규칙을 따릅니다.

```bash
# KRaft 기동
KAFKA_MODE=kraft ./lab.sh doctor
KAFKA_MODE=kraft ./lab.sh up
KAFKA_MODE=kraft ./lab.sh kraft-status
KAFKA_MODE=kraft ./lab.sh smoke
```

KRaft 최초 실행 시 `KRAFT_CLUSTER_ID`를 `.state/kraft-cluster-id`에 생성하고 이후 재기동에 재사용합니다. ZooKeeper 모드에서 사용한 Kafka volume을 KRaft에서 재사용하지 않으며, 기존 클러스터를 단순 모드 변경으로 변환하지 않습니다. 다른 모드는 별도 실습 디렉터리와 LAB_NAME에서 시작하세요. `zk`, `zk-shell`, `stop-zk`, `zk-quorum`은 KRaft에서 지원하지 않습니다.

태그는 고정했지만 이미지 digest까지 잠그지는 않았습니다. Python base image의 patch release도 고정하지 않았습니다. 따라서 바이트 단위 재현 빌드는 보장하지 않습니다.

## 2. 준비

목표 환경은 **Linux + Podman(rootless 가능)** 입니다. Linux x86_64를 우선 대상으로 작성했습니다. ARM, Windows/macOS Podman machine, 각 배포판별 실기동 호환성은 이 패키지 제작 환경에서 확인하지 않았습니다. **Docker + Compose v2 플러그인**으로도 실행할 수 있습니다(`.env`의 `CONTAINER_ENGINE=docker`).

설계상 권장 자원은 **4 vCPU, RAM 8GB, 여유 디스크 10GB 이상**입니다. 이는 실측 벤치마크가 아니라 이 Lab의 JVM/데이터 규모를 위한 권장 예산입니다. 다른 JVM/DB 실습을 동시에 켜면 더 필요합니다.

Fedora 계열:

```bash
sudo dnf install -y podman podman-compose python3
# Docker를 쓰는 경우: sudo dnf install -y docker-ce docker-compose-plugin python3
```

Ubuntu/Debian 계열:

```bash
sudo apt update
sudo apt install -y podman podman-compose python3
# Docker를 쓰는 경우: sudo apt install -y docker-ce docker-compose-plugin python3
```

Podman 4/5 계열과 `podman-compose` 1.x를 대상으로 합니다. `--in-pod=false` 옵션을 지원해야 합니다. 이 프로젝트는 `podman compose`의 외부 provider 자동 선택이 아니라 **`podman-compose` 실행 파일을 직접 사용**합니다. netavark/aardvark-dns 등 컨테이너 DNS가 정상이어야 합니다. [S8]

엔진은 `.env`의 `CONTAINER_ENGINE`으로 선택합니다.

```bash
CONTAINER_ENGINE=auto    # 설치된 엔진 중 podman 우선 (기본값)
CONTAINER_ENGINE=podman  # podman-compose 또는 podman compose 사용
CONTAINER_ENGINE=docker  # docker compose (Compose v2 플러그인) 사용
```

Docker 경로도 동일한 `./lab.sh` 명령과 검증 절차를 사용하며, `doctor`가 엔진·Compose 버전을 함께 출력합니다. 컨테이너/볼륨/네트워크 소유권 확인에는 동일한 `io.kzk.lab` 라벨을 사용합니다.


호스트에 Kafka, JDK, Python Kafka 패키지를 설치할 필요는 없습니다. 이미지 레지스트리와 PyPI에 대한 최초 다운로드 접근은 필요합니다. 컨테이너 실행은 한 사용자로 통일하고 `sudo podman`과 일반 `podman`을 섞지 마세요.

## 3. 바로 시작

```bash
unzip kafka-zookeeper-lab.zip
cd kafka-zookeeper-lab
chmod +x lab.sh scripts/*.sh

# .env가 없으면 .env.example을 자동 복사합니다.
./lab.sh doctor
./lab.sh up

# 실제 broker 응답과 읽은 데이터의 누락/중복 검사
./lab.sh smoke

# 합성 금융 거래 10,000건 생성 → Kafka에 적재
./lab.sh seed --kind payments --count 10000

# leader / replica / ISR 확인
./lab.sh topics

# 10건 미리 보기: consumer-group offset은 변경하지 않음
./lab.sh read lab.payments --max 10

# 같은 데이터의 일부를 consumer group으로 실제 처리/commit
./lab.sh consume lab.payments study-g1 --count 1000 --duration 60
./lab.sh lag lab.payments study-g1
```

`up`은 ZooKeeper 3노드의 leader/follower 상태를 확인한 후 Kafka를 기동합니다. 단순히 컨테이너가 `Running`이라는 이유로 준비 완료로 간주하지 않습니다. 이후 broker 3개와 leader/ISR 상태를 확인하고 기본 토픽을 만듭니다.

명령을 처음 접할 때는 다음 흐름을 사용하면 됩니다.

```bash
./lab.sh scenarios       # 장애/시드/시뮬레이션 목록
./lab.sh quickstart      # doctor → 기동 → health → smoke
./lab.sh summary         # 현재 설정과 컨테이너/클러스터 요약
./lab.sh check           # health의 짧은 별칭
./lab.sh start           # up의 별칭
./lab.sh stop            # down의 별칭
```

자주 쓰는 데이터 흐름은 preset 명령으로 줄일 수 있습니다.

```bash
./lab.sh seed-preset fraud --count 5000
./lab.sh seed-preset outage --count 2000
./lab.sh simulate-preset traffic
./lab.sh simulate-preset burst --batches 5
```

Preset은 안전한 유한 기본값을 사용하며, 뒤에 옵션을 붙여 조정할 수 있습니다.

`smoke`는 고유한 새 토픽에 120건을 넣고, broker delivery 확인 120건과 실제 읽기 120건, `sequence=0..119`의 누락/중복을 검사합니다. 성공했을 때만 `status: PASS`가 출력됩니다. 이 테스트를 통과해도 모든 장애 시나리오나 원격 listener가 검증된 것은 아닙니다.

## 4. 기본 데이터와 크기

| 토픽 | 파티션 | 데이터 예 |
|---|---:|---|
| `lab.payments` | 12 | 합성 계정, 가맹점, 금액, 채널, 승인 결과, 위험 점수 |
| `lab.access` | 6 | 합성 사용자, 문서용 IP, 경로, HTTP 상태, 응답 시간 |
| `lab.metrics` | 6 | 합성 장치, 지역, 온도, CPU/메모리 사용률 |
| `lab.manual` | 3 | 직접 입력하는 console producer 실습용 |

기본 토픽은 모두 RF=3입니다. 실제 계좌/고객 정보는 사용하지 않습니다. JSON 값과 별도로 Kafka record **key**도 전달합니다.

```bash
# 종류별 데이터
./lab.sh seed --kind payments --count 20000 --payload-bytes 512
./lab.sh seed --kind access --count 15000 --payload-bytes 256
./lab.sh seed --kind metrics --count 10000 --payload-bytes 128

# 약 100MiB: 생성된 key+JSON value의 합계 기준
./lab.sh seed --kind payments --mib 100 --payload-bytes 1024 --rate 3000

# 제한 시간 동안 계속 입력: 초당 목표 500건, 60초
./lab.sh seed --kind access --duration 60 --rate 500
```

`--mib 100`은 **논리적 key+value 크기**입니다. 브로커 디스크 사용량이나 네트워크 전송량 100MiB를 의미하지 않습니다. RF=3의 복제본, record batch/index/segment, 파일시스템과 이미지 공간이 별도로 필요합니다. 마지막 메시지 단위로 목표를 넘을 수 있습니다.

`--payload-bytes`는 JSON 전체 크기가 아니라 JSON 안 `payload` 필드의 크기입니다. 기본값은 256바이트입니다. 메모리에 전체 데이터를 모으지 않고 한 건씩 생성하며, 실패한 delivery나 flush 미완료는 성공으로 세지 않습니다. 결과의 `queued`가 아니라 **`delivered`, `failed`, `pending`**을 확인하세요.

입력 옵션별 상한은 `--mib 512`, `--count 2000000`, `--duration 600`입니다. 건수/시간 모드의 총 바이트 수는 메시지 크기와 전송률에 따라 달라집니다. 여러 번 실행한 누적 디스크 사용량까지 제한하는 quota는 아닙니다. 기본 보존 24시간과 디스크 여유를 함께 확인하세요. 기본 전송률은 1,000건/초이며 `--rate 0`은 무제한입니다.

`--seed 42`는 합성 필드의 난수 재현용입니다. 실행마다 `run_id`와 시작 시간이 달라 전체 바이트가 항상 같지는 않습니다. `event_id=run_id:sequence`로 한 번의 실행을 추적할 수 있습니다.

### 고도화된 시드와 지속 시뮬레이션

기본 데이터 외에 업무 상황별 profile을 선택할 수 있습니다.

```bash
# 고위험 결제/사기 후보
./lab.sh seed --kind payments --profile fraud --count 10000

# 장애 상황의 5xx/고지연 access
./lab.sh seed --kind access --profile outage --count 5000

# 장비 과열·CPU/메모리 포화 metrics
./lab.sh seed --kind metrics --profile outage --count 5000

# 소수 key에 트래픽 집중
./lab.sh seed --kind payments --profile skewed --count 10000
```

`simulate`는 batch 단위로 주기적인 이벤트를 생성합니다. `--batches` 또는 `--duration` 중 하나를 사용해 실행 한계를 지정하는 것을 권장합니다.

```bash
# 30초마다 500건, 총 10 batch
./lab.sh simulate --kind payments --profile fraud \
  --batch-count 500 --interval 30 --batches 10 --rate 1000

# 5초 간격으로 10분 동안 장애 지표 생성
./lab.sh simulate --kind metrics --profile outage \
  --batch-count 100 --interval 5 --duration 600
```

여러 업무 topic을 하나의 시뮬레이션에서 섞고, 시간대별 profile과 burst를 지정할 수 있습니다.

```bash
# kind 가중치에 따라 세 topic을 선택
./lab.sh simulate --kind all \
  --mix payments=60,access=30,metrics=10 \
  --batch-count 300 --batches 20 --interval 10

# 10 batch 정상 → 5 batch 사기 → 5 batch 장애
./lab.sh simulate --kind all \
  --phases baseline=10,fraud=5,outage=5 \
  --batch-count 100 --batches 20 --interval 5

# 5번째 batch마다 5배 burst, interval은 ±20% 흔들림
./lab.sh simulate --kind payments --profile seasonal \
  --batch-count 100 --batches 20 --interval 10 \
  --burst-every 5 --burst-multiplier 5 --jitter 20
```

`--kind all`의 기본 선택 비율은 세 kind 균등이며, `--mix`로 양의 정수 weight를 지정합니다. `--phases`는 `profile=batch수` 순서로 적용되고 마지막 phase 이후에는 마지막 profile을 유지합니다. `--seed`는 kind 선택과 jitter에도 사용되므로 같은 입력이면 스케줄이 재현됩니다.

`simulate`는 각 batch에 별도의 `run_id`를 부여하고 delivery 결과를 확인합니다. `Ctrl-C`를 누르면 현재까지의 batch/전송 건수를 출력하고 종료합니다. 실행 중인 simulator를 백그라운드로 남기는 기능은 제공하지 않으며, shell/systemd 등 외부 supervisor가 필요합니다.

## 5. 화면으로 보기 — 선택 사항

```bash
./lab.sh ui up
```

기본 주소:

```text
http://localhost:8088
```

UI 없이도 모든 핵심 학습/장애 명령을 사용할 수 있습니다. UI는 broker/topic/partition/message/consumer group 관찰을 위한 **읽기 전용 설정**입니다. Kafka 자체에는 ACL이 없으므로 UI 설정이 클러스터의 보안 경계인 것은 아닙니다.

### 다른 PC에서 접근하는 경우

`.env`를 편집합니다. 아래 IP는 예시이므로 실제 Linux 서버의 IP로 바꾸세요.

```dotenv
BIND_IP=0.0.0.0
ADVERTISED_HOST=192.168.0.50
```

```bash
# 데이터 볼륨은 보존하면서 listener 설정을 반영
./lab.sh down
./lab.sh up
./lab.sh ui up
```

외부 Kafka client의 bootstrap 예시:

```text
192.168.0.50:19092,192.168.0.50:29092,192.168.0.50:39092
```

내부 실습 도구는 `kafka1:9092,kafka2:9092,kafka3:9092`로 접근합니다. 외부 client는 bootstrap 이후에도 각 broker의 advertised address로 접속하므로 **세 broker 포트 모두 접근 가능**해야 합니다. `0.0.0.0`은 bind 주소이지 advertised destination이 아니며 `/0` 같은 CIDR을 넣지 않습니다. [S3]

외부 인터페이스 개방 시 인터넷에 포트 포워딩하지 말고, 실습망/관리 PC로 방화벽 접근을 제한하세요. ZooKeeper 포트는 호스트에 공개하지 않습니다.

## 6. 장애 실습

### 직접 장애를 유지하며 관찰

```bash
./lab.sh fault kill-broker 1
./lab.sh topics
./lab.sh status
./lab.sh logs kafka2
./lab.sh recover
```

`fault`는 자동 복구하지 않습니다. 학습자가 관찰한 뒤 `recover`로 복구합니다.

### 자동 데모 9개

```bash
./lab.sh demo broker-failover
./lab.sh demo controller-failover
./lab.sh demo min-isr
./lab.sh demo zk-one
./lab.sh demo zk-quorum
./lab.sh demo lag
./lab.sh demo hot-key
./lab.sh demo oversize
./lab.sh demo retention
```

데모마다 새로운 `lab.demo.*` 토픽을 만듭니다. 기본 학습 데이터를 삭제하거나 운영 중인 그룹의 오프셋을 초기화하지 않습니다. 정상 종료·오류·Ctrl+C에서 노드 복구를 시도하고 **복구 확인까지 성공해야** `DEMO PASS`를 출력합니다. SIGKILL/호스트 종료에서는 trap이 실행되지 않으므로 `recover`가 필요합니다.

데모 결과는 `reports/run-*/output.log`, 종료 코드는 `exit-code.txt`에 남습니다. 데이터/topic 자체는 복구 후에도 남고, 데모가 설정한 토픽별 retention 등도 유지됩니다. `recover`는 노드 상태 복원이지 데이터/토픽 설정의 시점 복원이 아닙니다.

장애 내용, 관찰값, 대응 절차는 `docs/03-failure-scenarios.md`를 먼저 읽으세요.

## 7. 종료와 초기화

```bash
# 컨테이너/네트워크 제거. Kafka/ZooKeeper 데이터 볼륨 보존.
./lab.sh down

# 이후 재기동
./lab.sh up

# 이 Lab의 데이터도 삭제. 되돌릴 수 없음.
./lab.sh reset --yes
```

중단된 demo lock이 남았다면:

```bash
./lab.sh recover
./lab.sh unlock --yes
```

스크립트는 `kzk-lab` 이름과 `io.kzk.lab` 라벨을 확인하고 대상을 제한합니다. `podman system prune`, 전체 컨테이너 삭제, 호스트 디스크 채우기는 하지 않습니다. `.env`의 `LAB_NAME`은 사용 중에 바꾸지 마세요. 다른 이름으로 바꾸면 기존 환경을 지우는 대신 별도 환경으로 취급합니다.

## 8. 읽는 순서

| 문서 | 내용 |
|---|---|
| `docs/01-kafka-basics.md` | broker/topic/partition/key/offset, replica/ISR, ZooKeeper 역할 |
| `docs/02-hands-on.md` | 기본 CLI, consumer group, rebalance, offset reset, replica 이동 |
| `docs/03-failure-scenarios.md` | 9개 데모와 수동 장애의 원인·관찰·복구 |
| `docs/04-troubleshooting.md` | Podman, DNS/listener, 권한, OOM, 로그 수집과 대응 순서 |
| `docs/SOURCES.md` | 공식 문서와 버전 선택 근거 |
| `reports/VALIDATION.md` | 통과한 검사와 미실행 항목 |

**첫 실습은 `up → smoke → seed → read → consume → lag → broker-failover → min-isr` 순서로 진행하세요.**

## 2026-09-18 실행 계약 수정

실제 실행 파일은 `.state/compose.generated.yaml`입니다. tools build context는 프로젝트의 실제 `client/` 경로로 생성합니다.
`KAFKA<N>_PORT`, `KAFKA_HEAP_OPTS`, `ZOOKEEPER_HEAP_OPTS`가 renderer에 반영되며, `.env`는 실행 가능한 셸이 아닌 KEY=value 데이터입니다.
이미 export된 값이 우선하고 노드 번호는 설정한 NODES 범위 내에서 검사합니다. KRaft 상태 점검은 ZooKeeper를 호출하지 않습니다.

KRaft ID는 canonical URL-safe Base64 16바이트 표현(22자)을 사용합니다. 정상 ID는 보존하며, 잘못된 기존 ID나 서로 다른 지정 ID는 거부합니다.
기존 볼륨이 있을 때 저장 ID가 없으면 새 ID를 만들지 않습니다. **ID 오류 해결을 위해 상태 파일이나 볼륨을 무작정 지우지 마세요.**
실제 meta.properties/백업과 일치하는 ID를 조사해야 합니다. 새 빈 실습을 시작할 때만 별도 프로젝트 이름/볼륨을 사용하세요.
생성 산출물·경계·설정 반영 회귀 검사는 `tests/test_generated_config.py`를 포함한 `bash scripts/test-static.sh`로 실행합니다.
