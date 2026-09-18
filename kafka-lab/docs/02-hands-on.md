# 02. 직접 따라 하는 실습

이 문서의 명령은 프로젝트 디렉터리에서 실행합니다. `lab.sh kcli`는 동작 중이고 lab 네트워크에 연결된 broker를 찾아 Kafka 기본 CLI를 실행하고, `--bootstrap-server`를 자동으로 붙입니다. 기본 이름 `kzk-lab`을 전제로 한 `podman` 명령은 LAB_NAME을 변경한 경우 함께 바꾸세요.

Kafka 기본 운영 명령의 근거는 [S5]입니다. 같은 단계를 처음부터 다시 할 때 이미 존재하는 topic/group의 데이터와 offset이 남아 있음을 고려하세요.

## 실습 A. JSON 두 건을 손으로 입력

```bash
printf '%s\n' \
  'account-001:{"event_id":"manual-1","amount":10000}' \
  'account-002:{"event_id":"manual-2","amount":25000}' |
./lab.sh kcli kafka-console-producer \
  --topic lab.manual \
  --property parse.key=true \
  --property key.separator=: \
  --producer-property acks=all
```

```bash
./lab.sh kcli kafka-console-consumer \
  --topic lab.manual --from-beginning --max-messages 2 \
  --property print.key=true \
  --property print.partition=true \
  --property print.offset=true
```

관찰: 두 key는 어느 partition에 들어갔는가? JSON 내부 event_id와 Kafka offset은 왜 다른가? 기존 `lab.manual` 데이터가 있으면 이전 두 건이 먼저 보일 수 있습니다.

`console-consumer`를 계속 실행하면 새 데이터도 따라옵니다. 위 예시는 종료를 위해 `--max-messages 2`를 지정했습니다.

## 실습 B. 여러 데이터 형식 비교

```bash
./lab.sh seed --kind payments --count 1000
./lab.sh seed --kind access --count 1000
./lab.sh seed --kind metrics --count 1000
./lab.sh read lab.payments --max 3
./lab.sh read lab.access --max 3
./lab.sh read lab.metrics --max 3
```

JSON을 schema registry나 Avro 없이 전달합니다. 이 단계에서는 record key, value, topic, partition, offset의 관계에 집중하세요. 입력은 테스트용 값이며 실제 고객/장치 데이터가 아닙니다.

## 실습 C. 같은 그룹 두 consumer와 rebalance

먼저 데이터를 채웁니다.

```bash
./lab.sh seed --kind payments --count 30000 --rate 3000
```

터미널 A:

```bash
./lab.sh consume lab.payments rebalance-g1 --duration 120 --sleep-ms 20
```

터미널 B:

```bash
./lab.sh consume lab.payments rebalance-g1 --duration 120 --sleep-ms 20
```

터미널 C:

```bash
./lab.sh kcli kafka-consumer-groups --describe --group rebalance-g1 --members --verbose
./lab.sh kcli kafka-consumer-groups --describe --group rebalance-g1
```

클라이언트의 `ASSIGNED`/`REVOKED` 출력과 각 구성원이 맡은 partition을 비교합니다. B를 Ctrl+C로 종료한 뒤 A의 재할당을 봅니다. 120초가 지나면 이 실습 consumer는 종료하도록 제한했습니다.

서로 다른 그룹 ID로 실행하면 각 그룹이 독립적으로 같은 토픽을 읽습니다. 같은 그룹 안에서의 분담과 다른 그룹 사이의 독립 읽기를 비교하세요. [S4, S5]

## 실습 D. Lag와 오프셋 재개

```bash
./lab.sh consume lab.payments offset-g1 --count 500 --duration 60
./lab.sh lag lab.payments offset-g1
./lab.sh consume lab.payments offset-g1 --count 500 --duration 60
./lab.sh lag lab.payments offset-g1
```

두 번째 실행은 동일 그룹의 committed offset에서 이어집니다. `auto.offset.reset=earliest`는 유효한 committed offset이 없는 경우 등에 적용되며, 실행할 때마다 무조건 처음부터 읽으라는 뜻이 아닙니다. [S7]

`lag`에서 `committed:null`은 해당 partition에 아직 commit이 없다는 의미입니다. 이때 이 도구는 earliest 시작을 가정한 추정 backlog를 표시하고 `estimated:true`로 구분합니다. 추정치를 실제 committed offset으로 오해하지 마세요.

## 실습 E. 그룹 오프셋 초기화 — 해당 그룹 중지 후 실행

먼저 `offset-g1`을 쓰는 consumer를 모두 종료하고 확인합니다.

```bash
./lab.sh kcli kafka-consumer-groups --describe --group offset-g1 --state

# 변경 예정값만 확인
./lab.sh kcli kafka-consumer-groups \
  --group offset-g1 --topic lab.payments \
  --reset-offsets --to-earliest --dry-run

# 실제 적용: offset-g1이 lab.payments를 다시 읽게 됩니다.
./lab.sh kcli kafka-consumer-groups \
  --group offset-g1 --topic lab.payments \
  --reset-offsets --to-earliest --execute

./lab.sh consume lab.payments offset-g1 --count 500 --duration 60
```

이것은 broker 데이터 복원도, 데이터 삭제도 아닙니다. **특정 consumer group의 재시작 위치 변경**입니다. `--to-latest`는 기존 미처리 데이터의 읽기를 건너뛰게 만들 수 있으므로 장애를 감추기 위한 lag 제거 수단으로 사용하지 마세요. [S5]

## 실습 F. 파티션 수 늘리기

전용 토픽을 만듭니다.

```bash
./lab.sh kcli kafka-topics --create --topic lab.expand \
  --partitions 3 --replication-factor 3 --config min.insync.replicas=2
./lab.sh seed --topic lab.expand --kind payments --count 1000
./lab.sh kcli kafka-topics --alter --topic lab.expand --partitions 6
./lab.sh seed --topic lab.expand --kind payments --count 1000
./lab.sh topics --topic lab.expand
./lab.sh offsets lab.expand
```

파티션을 늘린다고 기존 레코드를 새 파티션에 자동으로 다시 나누지 않습니다. 동일 key의 이후 목적지가 달라질 수도 있으므로 key별 순서 요구가 있는 서비스에서 계획 없이 늘리면 안 됩니다. Kafka는 이 명령으로 파티션 수를 줄이는 기능을 제공하지 않습니다. [S5]

## 실습 G. 복제본을 실제로 다른 broker로 이동

RF=3/broker=3에서는 모든 partition에 세 broker의 복제본이 있으므로, **RF=2인 전용 토픽**을 만들어 이동을 명확히 봅니다. 다음은 장애 주입 없이 모두 정상인 상태에서 실행합니다.

```bash
./lab.sh health
./lab.sh kcli kafka-topics --create --topic lab.move \
  --replica-assignment 1:2 --config min.insync.replicas=2
./lab.sh seed --topic lab.move --kind payments --count 10000 --rate 2000
./lab.sh topics --topic lab.move
```

현재 복제본은 1/2입니다. 목표를 2/3으로 정합니다.

```bash
mkdir -p .state
cat > .state/move.json <<'JSON'
{"version":1,"partitions":[{"topic":"lab.move","partition":0,"replicas":[2,3]}]}
JSON
cat > .state/move-original.json <<'JSON'
{"version":1,"partitions":[{"topic":"lab.move","partition":0,"replicas":[1,2]}]}
JSON

# kcli가 어느 정상 broker를 선택해도 같은 파일을 보게 모두 복사
for id in 1 2 3; do
  podman cp .state/move.json "kzk-lab-kafka$id:/tmp/lab-move.json"
  podman cp .state/move-original.json "kzk-lab-kafka$id:/tmp/lab-move-original.json"
done

./lab.sh kcli kafka-reassign-partitions \
  --reassignment-json-file /tmp/lab-move.json \
  --execute --throttle 5000000

./lab.sh kcli kafka-reassign-partitions \
  --reassignment-json-file /tmp/lab-move.json --verify
./lab.sh topics --topic lab.move
```

`--verify`에서 진행 중이면 완료될 때까지 다시 확인합니다. 완료 확인은 throttle 정리에도 중요합니다. 새 replica가 따라잡는 과정과 최종 replica 목록을 관찰하세요. 실행 전 원래 배치를 저장해 둔 이유도 설명해 보세요. [S5, S9]

원래 배치로 되돌리기:

```bash
./lab.sh kcli kafka-reassign-partitions \
  --reassignment-json-file /tmp/lab-move-original.json \
  --execute --throttle 5000000
./lab.sh kcli kafka-reassign-partitions \
  --reassignment-json-file /tmp/lab-move-original.json --verify
```

이 RF=2 토픽은 기본 min ISR=2를 유지하므로 해당 replica 둘 중 하나가 중단되면 쓰기 가용성이 줄어듭니다. 기본 RF=3 토픽과 장애 허용 조건이 다릅니다. `recover`는 수동으로 건 throttle이나 진행 중인 reassignment를 취소하지 않습니다.

## 실습 H. 장애 후 preferred leader 복원 관찰

이 Lab은 장애 전/후 leader를 관찰하기 쉽도록 자동 leader rebalance를 껐습니다. broker 재기동과 full ISR 복귀가 곧 원래 leader 복귀를 뜻하지는 않습니다.

```bash
./lab.sh recover
./lab.sh topics --topic lab.payments
./lab.sh kcli kafka-leader-election --election-type preferred --all-topic-partitions
./lab.sh topics --topic lab.payments
```

Preferred replica가 이미 leader인 경우 CLI가 별도 동작이 없다고 알릴 수 있습니다. 이것은 복제본 이동과도 다른 작업입니다. 수동 CLI 동작은 `recover`에서 자동 되돌리지 않습니다.
