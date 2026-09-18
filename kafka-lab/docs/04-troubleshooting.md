# 04. 시작 실패와 장애 대응

실패했을 때 볼륨 삭제부터 하지 마세요. 기본 절차는 상태·로그 확인 → 원인 수정 → `recover`입니다. 이 프로젝트는 Linux 호스트에서 Bash와 Podman Compose를 사용하도록 작성했습니다. Windows에서는 Linux VM/WSL 안에서 실행해야 하며 Podman Machine/WSL의 포트·네트워크 동작까지 제작 환경에서 검증한 것은 아닙니다.

## 1. 우선 수집할 정보

```bash
./lab.sh doctor
podman ps -a --filter label=io.kzk.lab=kzk-lab
./lab.sh logs zk1
./lab.sh logs kafka1
./lab.sh zk
./lab.sh topics
./lab.sh status
```

`LAB_NAME`을 바꿨다면 위 필터도 바꿉니다. 컨테이너 종료 원인은 다음처럼 확인합니다.

```bash
podman inspect kzk-lab-kafka1 --format '{{json .State}}'
podman stats --no-stream
```

명령이 실패하면 다음 진단 명령을 별도로 실행하세요. `status`는 장애 중 부분 정보를 출력하고 오류로 끝날 수 있습니다. 진단을 위해 장애를 성공으로 덮어쓰지 않습니다.

명령을 잊었을 때는 `./lab.sh scenarios`와 `./lab.sh summary`를 먼저 사용하세요. 일반적인 재시작은 `./lab.sh stop && ./lab.sh start`, 데이터까지 지우는 초기화는 되돌릴 수 없으므로 `./lab.sh clean --yes`입니다.

## 2. Podman/compose/DNS 문제

호스트에 `podman`, `podman-compose`, `python3`, `bash`, `tee`가 필요합니다. `podman compose`는 별도 compose provider를 호출하는 wrapper이며 provider에 따라 동작이 다를 수 있습니다. 이 lab은 독립 실행 파일인 `podman-compose`를 명시적으로 사용합니다. [S8]

`--in-pod=false`가 중요합니다. 여러 컨테이너를 같은 Pod 네트워크 namespace에 넣으면 같은 내부 포트를 쓰는 노드끼리 충돌하거나 네트워크 격리 실습의 의미가 달라집니다. `doctor`는 해당 옵션 지원과 실제 `config` 렌더링을 확인합니다.

```bash
podman-compose --version
podman-compose --help
podman info
./lab.sh config
podman network inspect kzk-lab-net
```

KRaft를 사용할 때는 `KAFKA_MODE=kraft`를 모든 명령에 일관되게 적용하세요. 이 모드는 ZooKeeper 컨테이너 없이 3개 Kafka 노드가 controller quorum을 구성하고, `kraft-status`로 quorum 상태를 확인합니다.

```bash
KAFKA_MODE=kraft ./lab.sh doctor
KAFKA_MODE=kraft ./lab.sh kraft-status
KAFKA_MODE=kraft ./lab.sh logs kafka1
```

KRaft의 `KRAFT_CLUSTER_ID`와 Kafka data volume은 ZooKeeper 모드와 분리됩니다. 모드를 바꿀 때 기존 volume을 강제로 재사용하지 말고 `down` 후 새 모드로 시작하세요. 기존 ZooKeeper metadata를 KRaft로 변환하는 migration 절차는 이 학습 프로젝트의 범위가 아닙니다.

노드 수는 Compose 파일을 직접 편집하지 말고 `NODES=5 ./lab.sh up` 또는 `./lab.sh up --nodes 5`로 지정합니다. `lab.sh`가 `.state/compose.generated.yaml`을 생성하며, ZooKeeper ensemble/KRaft voter와 broker bootstrap을 동일하게 맞춥니다. 최대 100개까지 Compose를 생성할 수 있고, ZooKeeper에서는 짝수 노드를 거부합니다. 노드 수를 변경할 때에는 기존 클러스터를 내린 뒤 재기동하세요.

오래된 CNI backend에서 서비스 이름을 해석하지 못하면 해당 배포판의 Podman DNS 플러그인 구성을 점검하세요. 최신 Netavark/Aardvark 구성에서도 패키지 누락, rootless 네트워크, 사용자 설정을 확인합니다. 다른 lab을 포함하는 전체 network/system prune은 해결 절차에 포함하지 않습니다. [S8]

정상 기동 뒤 컨테이너에서 DNS를 확인할 수 있습니다.

```bash
podman exec kzk-lab-tools python -c \
  'import socket; print(socket.getaddrinfo("kafka1",9092)); print(socket.getaddrinfo("zk1",2181))'
```

## 3. 이미지 pull 또는 client build 실패

Docker Hub의 인증·rate limit, GHCR 접속, 사내 프록시, DNS와 인증서 검증 실패를 먼저 확인합니다. UI는 선택적이므로 UI 이미지 실패가 기본 Kafka 실습을 막지는 않습니다.

```bash
podman pull docker.io/confluentinc/cp-kafka:7.9.0
podman pull docker.io/confluentinc/cp-zookeeper:7.9.0
podman pull docker.io/library/python:3.12-slim-bookworm
```

이 lab은 client를 빌드할 때 PyPI의 `confluent-kafka==2.8.2` wheel을 설치합니다. 처음 실행은 이미지와 Python 패키지를 받을 네트워크가 필요합니다. ZIP만으로 완전한 오프라인 환경이 구성되지는 않습니다. [S7]

핀 고정은 재현성을 위한 것이며 최신 보안 패치라는 뜻이 아닙니다. CP 이미지는 양쪽 모두 같은 7.9.x로 맞추고 별도 테스트를 거쳐 변경하세요. `latest` 또는 CP 8.x로 바꾸면 요구한 ZooKeeper 모드와 맞지 않습니다. 메이저 버전 변경 후 기존 데이터 볼륨을 강제로 재사용하는 절차는 제공하지 않습니다. [S1][S2]

## 4. 볼륨 Permission denied / SELinux

브로커 및 ZooKeeper는 각각 별도의 Podman named volume을 씁니다. `:U`는 이 실습 볼륨의 사용자 소유권을 컨테이너 사용자에 맞추도록 요청하는 Podman 옵션입니다. 호스트의 임의 디렉터리를 재귀 chown하는 스크립트는 넣지 않았습니다. Confluent 이미지의 영속 데이터 경로와 비-root 실행도 고려한 설정입니다. [S8][S10]

```bash
podman volume inspect kzk-lab-kafka1-data
podman inspect kzk-lab-kafka1 --format '{{json .Mounts}}'
./lab.sh logs kafka1
```

SELinux를 통째로 끄거나 `--privileged`로 우회하지 마세요. 이 lab은 host bind mount 대신 named volume을 사용합니다. 사용자가 bind mount로 변경했다면 소유권뿐 아니라 `:Z`/`:z` 레이블과 공유 여부도 별도로 설계해야 합니다. [S13]

## 5. 포트 충돌과 advertised.listeners

기본 포트는 19092, 29092, 39092, 선택적 UI 8088입니다.

```bash
ss -ltn
```

충돌하면 `.env`의 포트를 바꾸고 다음처럼 컨테이너를 재생성합니다. 볼륨은 보존됩니다.

```bash
./lab.sh down
./lab.sh up
# UI를 쓰는 경우
./lab.sh ui up
```

다른 PC에서 Kafka에 접속하려면 `BIND_IP=0.0.0.0`과 `ADVERTISED_HOST=실제서버IP`를 함께 설정해야 합니다. Kafka는 bootstrap 접속 이후 메타데이터로 받은 개별 broker 주소에 접속합니다. 초기 포트 하나만 열려 있다고 충분한 것이 아닙니다. `ADVERTISED_HOST=localhost`이면 원격 PC는 자기 자신으로 접속하려고 합니다. [S3]

`0.0.0.0/0`은 listen 주소가 아닙니다. `0.0.0.0`은 bind 용도이고 광고할 클라이언트 목적지 주소로 쓰지 않습니다. 이 wrapper는 잘못된 advertised 값과 CIDR을 거부합니다. 인증 없는 PLAINTEXT 환경이므로 공인 인터넷에 노출하지 마세요.

## 6. RAM 부족·느린 startup·OOM

설계상 시작점은 4 vCPU / RAM 8GB / 여유 디스크 10GB이며, 실측 최소 사양을 보증하는 값은 아닙니다. 기본 Java 최대 heap 합계는 Kafka 1.5GiB + ZK 0.75GiB입니다. 실제 사용량에는 native memory, page cache, 컨테이너 이미지, tools/UI와 호스트 OS가 더해집니다.

```bash
free -h
podman stats --no-stream
podman inspect kzk-lab-kafka1 --format '{{json .State}}'
```

메모리가 부족하면 먼저 다른 실습 프로세스를 줄이거나 호스트 자원을 늘리세요. 기다리는 시간을 늘리는 것은 OOM 해결이 아닙니다. 자원은 충분하지만 느린 디스크에서 기동이 지연된다면 `.env`의 `STARTUP_TIMEOUT=480`처럼 늘릴 수 있습니다. 이것은 기다리는 상한을 조정하는 것이며 성공을 보장하지 않습니다.

heap 값을 바꾼 뒤에는 `down` → `up`으로 환경 변수를 반영합니다. `recover`는 기존 컨테이너를 시작하는 명령이므로 변경한 환경 변수로 컨테이너를 재생성하지 않습니다.

## 7. full ISR에 돌아오지 않음

```bash
./lab.sh topics
./lab.sh logs kafka1
./lab.sh logs kafka2
./lab.sh logs kafka3
./lab.sh recover
```

복구해도 full ISR이 되지 않으면 중단/paused/network disconnected 상태, DNS, 디스크 여유, 권한, replica fetch 관련 오류를 확인합니다. 자동 데모가 아닌 수동 CLI로 replication factor나 배치를 바꾸었다면 해당 토픽도 건강 확인 대상입니다.

복구된 follower가 데이터를 따라잡는 동안 즉시 full ISR이 되지 않을 수 있습니다. 임의의 출력 한 줄보다 `recover`의 최종 검사와 broker 로그를 함께 봅니다. `min.insync.replicas`를 낮춰 증상만 감추는 것을 기본 해결책으로 삼지 않습니다. [S4][S6]

## 8. 토픽이 없거나 이름이 잘못됨

자동 토픽 생성을 껐습니다. 오타 때문에 뜻하지 않은 토픽이 생성되는 것을 막기 위한 설정입니다.

```bash
./lab.sh topics
./lab.sh cli create --topic lab.my-events --partitions 6
./lab.sh seed --kind access --topic lab.my-events --count 1000
```

자동 스크립트의 쓰기/관리 토픽 이름은 `lab.`으로 시작해야 합니다. 직접 제공한 기본 Kafka CLI 명령까지 전부 제한하는 샌드박스는 아니므로 수동 `kcli` 사용에도 주의하세요.

## 9. consumer가 새 메시지를 안 읽거나 lag가 이상함

같은 group은 커밋한 위치부터 이어서 읽습니다. 이미 처리한 데이터 확인에는 `read`를 쓰거나 다른 group을 씁니다. `read`는 group에 가입하거나 commit하지 않습니다.

```bash
./lab.sh read lab.payments --max 10
./lab.sh consume lab.payments lab-fresh-group --count 100 --duration 60
./lab.sh lag lab.payments lab-fresh-group
```

처음 생성한 그룹은 일부 파티션에 commit이 없을 수 있습니다. 그때 출력의 `committed: null`, `estimated: true`는 시작 위치를 기준으로 추정한 값임을 뜻합니다. group reset은 모든 멤버를 멈춘 뒤 dry-run을 확인해야 하며, 상세 절차는 `02-hands-on.md`를 참고하세요. [S5]

consumer의 `--count 100`은 목표 처리 건수입니다. `--duration` 안에 목표에 도달하지 못하면 실제 `processed`를 출력하고 실패로 종료합니다. 반면 read의 `--max`는 최대치여서 저장된 메시지가 적으면 적은 수를 출력하고 정상 종료합니다. producer의 `delivered`와 consumer의 실제 `processed` 출력값을 확인하세요.

## 10. 쌓인 실습 데이터와 종료

`down`은 컨테이너와 네트워크를 내리지만 named volume 데이터를 유지합니다. `reset --yes`는 해당 lab의 9개 데이터 볼륨까지 삭제합니다. 외부 컨테이너나 볼륨 이름 충돌 시 소유권 레이블을 확인하고 작업을 거부합니다.

```bash
# 남겨 두고 종료
./lab.sh down

# 기존 자료가 필요 없는 실습용 환경임을 확인한 뒤 전체 초기화
./lab.sh reset --yes
./lab.sh up
```

기본 보존 기간은 24시간이지만 자동 데모 토픽의 메타데이터는 자동 삭제하지 않습니다. 개별 토픽이 필요 없어졌다면 정확한 이름을 확인하고 삭제합니다.

```bash
./lab.sh kcli kafka-topics --list
# 실제 목록에서 확인한 정확한 토픽 이름으로 치환하세요.
./lab.sh kcli kafka-topics --delete --topic lab.example-to-delete
```

## 11. 실제 검증 실행

```bash
# 먼저 up으로 클러스터를 기동
./lab.sh up
./scripts/test-live.sh

# 다른 수동 장애/consumer/재배치를 진행하지 않는 상태에서 전체 데모
./scripts/test-live.sh --faults
```

`test-live.sh`는 이미지 빌드나 최초 `up`을 대신하지 않습니다. 정상 클러스터를 전제로 health·120건 송수신 검증을 실행합니다. `--faults`는 9개 데모와 복구까지 이어서 실행합니다. 실패 시 non-zero exit와 로그를 남깁니다. 파일에 들어 있는 offline mock PASS를 이 실제 실행 결과로 오해하지 마세요.
