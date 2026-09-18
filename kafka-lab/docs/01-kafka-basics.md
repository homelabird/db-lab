# 01. 카프카 기본 개념 — 이 Lab에 대입해서 보기

## 데이터가 지나가는 경로

이 실습의 `seed`는 producer입니다. JSON 이벤트를 만들고 특정 topic/partition의 leader broker에 씁니다. follower broker는 leader로부터 복제합니다. `consume`은 consumer로서 기록된 이벤트를 읽고, 업무 처리에 해당하는 동작을 마친 뒤 진행 위치를 commit합니다. ZooKeeper에는 이벤트 본문을 넣지 않습니다. [S2, S4]

```text
합성 결제 이벤트 생성
      |
      v
lab.payments의 partition 선택 ← Kafka key: test-account-0042
      |
      v
해당 partition의 leader broker에 append
      |
      +── follower broker가 복제
      +── 다른 follower broker가 복제
      |
      v
consumer group이 읽고 처리 → 처리 위치 commit
```

## 꼭 구분할 용어

| 용어 | 이 실습에서 보는 방법 |
|---|---|
| Broker | `kafka1`, `kafka2`, `kafka3`: Kafka 서버 프로세스 |
| Topic | `lab.payments`: 같은 종류의 기록을 모으는 논리적 이름 |
| Partition | `lab.payments`의 0~11: 토픽을 나눈 로그 단위 |
| Record key | 합성 계정 ID 등. 생성기는 key와 JSON value를 별도로 전송 |
| Offset | 파티션 안의 위치. JSON 내부 `sequence`와 다른 값 |
| Consumer group | `study-g1`: 같은 그룹 구성원이 파티션을 나눠 처리 |
| Committed offset | 해당 그룹이 재개할 다음 위치 |
| Lag | log-end-offset과 committed offset의 차이로 보는 밀린 정도 |
| Replica | 한 파티션 로그의 복제본. leader도 replica에 포함 |
| ISR | 충분히 따라잡았다고 관리되는 in-sync replica 집합 |

기본 개념의 기준은 Apache Kafka 설계/운영 문서입니다. [S4, S5]

## Partition과 key, 순서

이 생성기의 정상 데이터는 여러 key에 분산됩니다. `--hot-key`는 모든 레코드의 Kafka key를 동일하게 바꿉니다. 같은 partitioner와 고정된 파티션 수 아래에서는 같은 key가 같은 partition으로 향합니다. 토픽 전체에 하나의 전역 순서가 있는 것은 아닙니다. [S4]

따라서 `read`의 JSON `sequence`가 `0, 1, 2, 3` 순서대로 출력되지 않아도 곧바로 오류는 아닙니다. 이 프로그램은 여러 파티션을 함께 읽습니다. 먼저 출력된 파티션의 레코드와 나중 파티션의 레코드가 섞일 수 있습니다.

확인:

```bash
./lab.sh read lab.payments --max 20
./lab.sh offsets lab.payments
./lab.sh demo hot-key
```

핫 파티션 하나에만 입력되는 상황에서 동일 그룹의 consumer를 더 늘려도 그 파티션을 여러 구성원이 동시에 나눠 맡는 일반 consumer-group 구조가 되지는 않습니다. 먼저 key 설계와 실제 파티션 분포를 확인해야 합니다. [S4]

## Leader, replica, ISR 읽기

```bash
./lab.sh topics --topic lab.payments
```

학습용 예시 출력:

```text
Topic: lab.payments  Partition: 0  Leader: 1  Replicas: 1,2,3  Isr: 1,2,3
```

이 뜻은 partition 0의 leader가 broker 1이고, 총 복제본은 broker 1/2/3에 있다는 것입니다. **leader 1개에 replica 3개가 추가되어 총 4개라는 의미가 아닙니다.**

```text
정상:      Replicas=1,2,3  ISR=1,2,3
2번 중단:  Replicas=1,2,3  ISR=1,3
```

두 번째 상태에서도 지정된 복제본 목록은 3개입니다. 다만 그중 2개만 ISR에 남았습니다. 미복제/복제 지연 상태와 리더가 아예 없는 상태를 구분하세요. 출력값은 예시이며 실제 broker/leader 선택은 실행 결과로 확인합니다. [S5]

## RF=3, min ISR=2, acks=all

이 Lab의 기본 조합은 다음과 같습니다.

```text
복제본 총수:                    3
정상 쓰기에 요구하는 최소 ISR:  2
producer acknowledgement:      all
```

`acks=all`은 현재 ISR을 기준으로 복제를 확인하며, `min.insync.replicas`가 쓰기 허용 하한을 정합니다. `acks=all`이 언제나 원래 배치한 세 broker 전부의 응답을 요구하는 것은 아닙니다. [S6]

| 조건 | 이 Lab에서 기대하는 정상 상태 |
|---|---|
| 정상 3개, ISR 3 | 쓰기 가능 |
| broker 1개 중단, leader 전환 완료, ISR 2 | 쓰기 가능 |
| ISR 1, 살아 있는 leader는 존재 | `acks=all` 쓰기 거절 |

`min-isr` 데모는 leader를 남겨 둔 채 follower 두 개를 차례로 종료합니다. 마지막 단계에서 단순 timeout을 성공적인 재현으로 간주하지 않고 `NOT_ENOUGH_REPLICAS` 계열의 실제 delivery 오류를 확인합니다. 오류를 선명하게 관찰하기 위해 이 probe에만 idempotence를 끄고 retries=0을 씁니다. 일반 `seed`는 idempotence를 켭니다.

## ZooKeeper 모드라고 해서 모든 것이 ZooKeeper에 저장되지는 않음

이 구성에서는 ZooKeeper가 broker 등록과 controller 조정에 관여하고, Kafka broker가 실제 partition 로그를 저장합니다. Kafka controller는 broker 하나가 맡는 클러스터 관리 역할입니다. [S2, S3]

```bash
./lab.sh zk
./lab.sh cli controller
./lab.sh zk-shell ls /brokers/ids
./lab.sh zk-shell get /controller
```

첫 명령의 ZooKeeper leader와 두 번째 명령의 Kafka controller ID를 별개로 적어 보세요. 현대 Kafka consumer-group의 offset은 Kafka의 내부 토픽 `__consumer_offsets`를 확인하는 대상이지, 이 Lab의 ZooKeeper 노드에서 찾는 대상이 아닙니다. [S5]

**ZooKeeper 과반수 상실과 모든 기존 메시지 송수신의 즉시 중단은 같은 명제가 아닙니다.** `zk-quorum` 데모는 기존 데이터 경로의 성공/실패를 관찰값으로 남기고 ZooKeeper serving 상태와 관리 작업의 실패를 별도로 봅니다.

## Commit, 재처리, idempotence

이 consumer 구현은 자동 commit을 끄고 처리한 메시지만 `store_offsets`에 기록한 뒤 기본 100건마다 동기 commit합니다. 종료 시에도 처리 완료 위치를 commit합니다. 강제 종료가 commit 전에 발생하면 처리한 일부 이벤트가 재처리될 수 있습니다. [S7]

Producer의 idempotence가 consumer의 DB 처리까지 자동으로 exactly-once로 바꿔 주지는 않습니다. 실습 결과의 `event_id`는 이런 재처리를 추적하기 위한 식별자로 활용할 수 있지만, 외부 DB의 중복 제거 로직은 이 Lab에 구현되어 있지 않습니다. [S4, S7]

## Kafka 저장공간 읽을 때 주의

Kafka는 소비자가 읽었다고 바로 해당 레코드를 삭제하지 않습니다. 이 Lab의 일반 토픽은 retention으로 정리합니다. 삭제는 segment 단위이며, compacted topic에서는 offset에 빈 구간도 생길 수 있으므로 `high-low`를 항상 실제 메시지 개수라고 부르면 안 됩니다. 이 도구도 그 값을 `offset_span`으로 표시합니다. [S6]

또한 broker 3개에 RF=3이면 각 partition의 복제본이 세 broker 모두에 존재합니다. 이 구성에서 보이는 균형은 주로 **leader 역할의 분산**입니다. 일부 broker에만 데이터를 배치하는 replica 이동 연습은 다음 문서에서 별도 RF=2 토픽으로 진행합니다.
