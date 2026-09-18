# 03. 장애 주입 → 관찰 → 원인 → 복구

이 파일은 **실행 절차와 기대 관찰값**입니다. 제작 환경에서 실제 클러스터 장애 재현을 완료했다는 보고서가 아닙니다. 실행 결과는 `reports/run-*/output.log`와 `exit-code.txt`로 확인하세요.

## 두 가지 실행 방식

`demo`는 전용 토픽을 만들고 사전 조건·결과를 검사한 뒤 노드를 자동 복구합니다. 관찰 시간을 직접 확보하려면 `fault`를 사용하세요. `fault`는 장애를 유지하고, `recover`를 실행해야 복구합니다.

```bash
./lab.sh health
./lab.sh demo broker-failover

# 또는 수동 실습
./lab.sh fault kill-broker 1
./lab.sh status
./lab.sh topics
./lab.sh recover
```

브로커에 접속할 수 없는 상태에서는 `status`의 메타데이터 조회가 실패할 수 있습니다. 이 경우 `podman ps -a`와 `./lab.sh logs kafka1`부터 확인합니다. 여러 장애를 동시에 겹치기 전에 각각 하나씩 실습하세요.

자동 데모는 종료·오류·Ctrl+C에서 복구를 시도하며, **복구까지 성공해야 `DEMO PASS`**를 출력합니다. 강제 종료(SIGKILL), 호스트 전원 종료는 자동 복구할 수 없습니다. 이 경우 `./lab.sh recover` 후 `./lab.sh unlock --yes`로 종료된 데모의 잠금을 정리합니다. 실행 중인 PID의 잠금은 해제하지 않습니다.

**복구의 범위:** 멈춘 노드 기동, pause 해제, 실습망 재연결, ZooKeeper 역할 확인, 모든 브로커/파티션 full ISR 확인입니다. 토픽 삭제, 데이터 복원, 설정 원복, 진행 중인 사용자 재배치 취소까지 수행하지 않습니다.

---

## 1. 파티션 리더 브로커 강제 종료

```bash
./lab.sh demo broker-failover
```

전용 토픽의 실제 파티션 리더를 조회한 다음 해당 브로커를 SIGKILL합니다. 지정한 숫자의 브로커를 무조건 종료하는 방식이 아닙니다. 리더가 바뀌고 ISR이 3에서 2로 수렴한 뒤 새로운 100건을 전송합니다.

관찰할 것은 `leader`, `replicas`, `isr`의 차이입니다. 브로커 하나가 사라져도 배치 설정인 `replicas`는 3개이며, 현재 동기화 멤버인 ISR은 줄어듭니다. 복구한 브로커가 로그를 따라잡으면 full ISR이 됩니다. 기본 설정은 자동 preferred leader 재균형을 꺼 두었으므로, 복구와 동시에 원래 리더로 돌아가야 하는 것은 아닙니다. [S4][S9]

수동 관찰:

```bash
./lab.sh topics --topic lab.payments
./lab.sh fault kill-broker 1
./lab.sh topics --topic lab.payments
./lab.sh seed --kind payments --count 100 --rate 10
./lab.sh logs kafka2
./lab.sh recover
```

여기서 1번 브로커가 모든 파티션의 리더인 것은 아닙니다. 서로 다른 파티션의 변화를 비교하세요.

## 2. Kafka controller 브로커 장애

```bash
./lab.sh demo controller-failover
```

현재 Kafka controller ID를 조회하여 그 브로커를 종료합니다. controller ID 변경, 살아 있는 브로커 2개, 전용 토픽 ISR 2, 이후 전송 성공을 검사합니다.

**Kafka controller와 ZooKeeper leader는 서로 다릅니다.** ZooKeeper 모드에서는 Kafka 브로커 하나가 controller 역할을 맡습니다. 그 브로커가 일부 파티션의 리더이기도 했다면 controller 교체와 파티션 리더 변경을 함께 관찰할 수 있습니다. [S4][S9]

```bash
./lab.sh cli controller
./lab.sh zk-shell get /controller
./lab.sh zk
```

실제 운영에서는 controller만 반복해서 바뀌는지, 브로커 재시작·GC·ZooKeeper 세션 만료가 동반되는지 로그의 시각을 맞춰 조사합니다. 실습은 임의 재시작을 운영 대응의 정답으로 가르치지 않습니다.

## 3. ISR 부족: 살아 있는 브로커가 있어도 쓰기 실패

```bash
./lab.sh demo min-isr
```

전용 파티션을 `[1,2,3]`에 배치하고 리더 1을 확인합니다. 2번을 종료한 상태에서는 쓰기를 확인하고, 3번까지 종료하여 리더 1만 남으면 거절 오류를 검사합니다.

| 단계 | ISR 수 | 설정 | 기대 결과 |
|---|---:|---|---|
| 정상 | 3 | RF=3, min ISR=2, acks=all | 쓰기 가능 |
| follower 1개 중단 | 2 | 동일 | 쓰기 가능 |
| follower 2개 중단 | 1 | 동일 | 충분한 동기 복제본이 없어 쓰기 거절 |

실습의 오류 probe는 이 경우를 명확히 식별하려고 retries=0·idempotence 비활성으로 1건을 보냅니다. 정상 데이터 생성기의 idempotent producer 설정과 구분하세요. `NOT_ENOUGH_REPLICAS` 또는 `NOT_ENOUGH_REPLICAS_AFTER_APPEND`만 예상 오류로 인정하며, 단순 timeout을 성공으로 판정하지 않습니다. 실패 응답이 모든 경우에 “어느 복제본에도 기록되지 않았다”를 보장하는 것은 아닙니다. [S4][S6]

복구 후 직접 재전송을 확인하세요.

```bash
./lab.sh health
./lab.sh seed --kind payments --count 100
```

운영 대응은 중단 원인과 복제 지연을 조사하고 ISR을 복원하는 것이 먼저입니다. `min.insync.replicas=1`이나 unclean leader election을 무조건 켜는 것은 데이터 보호와 가용성의 선택을 바꿉니다.

## 4. ZooKeeper 1개 장애

```bash
./lab.sh demo zk-one
```

`zk2`를 중단하고 나머지 2개가 leader/follower로 서비스를 제공하는지 확인합니다. 그 상태에서 토픽 생성과 전송을 시도합니다. ZK 프로세스가 켜져 있다는 사실과 quorum에 참여하며 요청을 처리한다는 사실을 구분하세요. [S12]

## 5. ZooKeeper 과반수 상실

```bash
./lab.sh demo zk-quorum
```

`zk2`, `zk3`를 중단한 뒤 `srvr`에서 서비스를 제공하는 노드가 0개가 되는지 확인합니다. 이어서 기존 토픽 쓰기와 새 토픽 생성의 결과를 기록합니다.

**ZooKeeper를 잃었다고 기존 Kafka 메시지 송수신이 무조건 같은 순간에 전부 끊긴다고 가정하지 않습니다.** 기존 리더와 데이터 경로, 세션 만료와 controller 처리 시점에 따라 관찰 결과가 달라질 수 있습니다. 이 데모는 기존 토픽 쓰기가 성공하든 실패하든 그대로 기록합니다. 메타데이터 관리와 장애 전환을 정상적으로 계속할 수 있다는 뜻은 아닙니다. [S4][S12]

새 토픽 생성 실패 자체만으로 특정 ZooKeeper 오류가 입증되었다고 판정하지도 않습니다. 원문 오류를 남기고, 주된 전제는 ZooKeeper의 `serving=0` 결과로 확인합니다. 요청 timeout 후 서버 작업이 늦게 완료될 가능성도 있으므로 복구 뒤 `.new` 토픽이 있는지 조회할 수 있습니다.

```bash
./lab.sh fault zk-quorum
./lab.sh zk
./lab.sh logs zk1
./lab.sh logs kafka1
./lab.sh recover
```

실제 장애에서 남은 ZK 데이터 디렉터리나 myid를 임의로 삭제하여 새 클러스터를 만들지 마세요. 이 lab의 `recover`도 기존 ZK 데이터를 보존합니다.

## 6. 느린 consumer → lag 누적 → 따라잡기

```bash
./lab.sh demo lag
```

새 3파티션 토픽에 5,000건을 넣고, 새로운 그룹이 건당 25ms 지연을 넣어 100건만 처리하도록 합니다. committed offset과 남은 lag를 확인한 뒤, 같은 그룹에서 인위적 지연을 제거하고 남은 데이터를 처리합니다. 모든 파티션에 실제 커밋이 있고 lag 합계가 0인지 검사합니다.

이것은 **애플리케이션 처리 지연** 모의입니다. Kafka 디스크·네트워크·GC 병목이 자동으로 만들어진다고 표현하지 않습니다. 운영에서는 입력률과 처리율, 파티션별 lag, consumer 에러, downstream DB/API 지연과 재시도부터 분리해 봅니다.

직접 지속 입력과 처리량을 비교하려면 다른 터미널에서 실행합니다.

```bash
# 터미널 A: 제한된 시간 동안 생산
./lab.sh seed --kind payments --duration 120 --rate 200

# 터미널 B: 같은 그룹을 추가 기동하면 파티션 재할당도 관찰 가능
./lab.sh consume lab.payments lab-slow --duration 120 --sleep-ms 20

# 터미널 C
./lab.sh lag lab.payments lab-slow
```

`lag=0`은 처리된 메시지를 확인하고 커밋한 위치가 따라잡았다는 관찰값이지, 외부 데이터베이스의 업무 처리가 정확하다는 증명은 아닙니다. [S4][S5]

## 7. Hot key → 한 파티션 집중

```bash
./lab.sh demo hot-key
```

6개 파티션의 새 토픽에 3,000건을 동일한 Kafka key로 전송합니다. 한 파티션에만 새 offset 범위가 생기는지 검사합니다. JSON 안의 ID와 Kafka key는 다른 개념입니다.

파티션 하나에 순서가 묶이면 같은 그룹의 consumer 수를 무작정 늘려도 그 파티션을 여러 consumer가 동시에 분할 담당하지 않습니다. key 설계, 업무 순서 요구, 입력 편향을 함께 봐야 합니다. 파티션 수 변경은 기존 데이터를 자동 재분산하는 해결책이 아닙니다. [S4][S9]

## 8. 메시지 크기 제한 위반

```bash
./lab.sh demo oversize
```

전용 토픽에 `max.message.bytes=1024`를 설정하고 8KiB padding을 포함한 메시지를 보냅니다. broker의 `MSG_SIZE_TOO_LARGE`를 확인한 뒤 작은 정상 메시지 1건의 전달을 검사합니다.

크기 제한은 JSON의 특정 필드뿐 아니라 **Kafka record batch**와 압축 등에 관련됩니다. 작은 메시지 여러 건을 모아도 한 batch가 제한을 넘을 수 있어, 정상 확인은 1건만 보냅니다. `--payload-bytes`는 padding 길이이지 완성된 Kafka 요청 전체 크기가 아닙니다. 실제 운영 변경 시 producer·broker/topic·consumer의 제한과 메모리 영향을 함께 검토합니다. [S6][S7]

## 9. Retention 삭제 → 이전 데이터 재처리 불가

```bash
./lab.sh demo retention
```

전용 토픽에 짧은 보존 기간과 작은 segment를 설정합니다. 6,000건을 넣어 segment가 닫힐 수 있도록 하고 log-start offset이 전진하는지 기다립니다.

`retention.ms=5000`이라고 각 메시지가 정확히 5초 뒤 사라지는 것이 아닙니다. segment 단위 삭제, segment roll, retention 검사 주기와 파일 삭제 지연이 개입합니다. 마지막 활성 segment에는 일부 레코드가 남을 수 있습니다. 이 lab은 전체 건수가 0이 되기를 기다리는 대신 **low offset의 전진**을 검사합니다. [S6]

삭제 후 consumer offset을 과거로 돌려도 이미 삭제된 기록을 Kafka에서 되살릴 수 없습니다. 설정을 되돌리는 것과 데이터를 복구하는 것은 다른 작업입니다. `recover`는 삭제된 기록을 복원하지 않습니다.

---

## 추가 수동 장애: pause와 네트워크 격리

```bash
# 정지된 프로세스가 살아 있는 것처럼 보이는 상황
./lab.sh fault pause-broker 1
./lab.sh topics --topic lab.payments
./lab.sh recover

# kafka1을 lab bridge에서 통째로 분리
./lab.sh fault isolate-broker 1
./lab.sh status
./lab.sh recover
```

`pause`는 실제 GC가 아니라 컨테이너 실행을 정지시키는 모의입니다. rootless Podman의 pause 지원은 호스트의 cgroup/권한에 따라 다릅니다. 미지원 오류를 정상 시나리오로 판정하지 않습니다.

`isolate-broker`는 지연 100ms나 특정 방향 패킷 손실이 아니라, 해당 컨테이너의 **실습 네트워크 전체 단절**입니다. 호스트 네트워크·iptables·다른 lab은 수정하지 않습니다. 재연결 시 서비스 DNS 별칭도 복원합니다. 일부 rootless/network backend 조합은 live disconnect를 거부할 수 있으며 실제 호스트에서 확인해야 합니다. [S8]

## 결과 기록 양식

```text
시나리오/전용 토픽:
장애 발생 시각:
장애 대상:
전/후 controller:
전/후 partition leader:
전/후 replicas와 ISR:
생산 결과와 정확한 오류명:
consumer lag:
ZooKeeper 역할:
복구 조치:
복구 후 full ISR 및 새 메시지 송수신 결과:
reports 경로:
```

디스크 전체 채우기, 운영 데이터 디렉터리 파손, 호스트 방화벽 변경은 포함하지 않았습니다. 단일 호스트 안의 여러 컨테이너는 실제 서로 다른 물리 서버/전원/디스크의 고가용성을 검증하지 않습니다.
