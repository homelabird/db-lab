# 02 · 복제와 영속화

## 복제 상태를 직접 읽기

```bash
./lab.sh master
./lab.sh status
./lab.sh cli redis-1 INFO replication
./lab.sh cli redis-2 INFO replication
./lab.sh cli redis-3 INFO replication
```

초기에는 redis-1이 Master지만 장애조치 뒤에는 달라질 수 있습니다.
출력의 `role:slave`는 Redis의 해당 버전 API가 반환하는 표현으로, 이 교재의 Replica와 같은 역할입니다.

| 필드 | 읽는 방법 |
|---|---|
| role | 현재 역할을 먼저 확인합니다. 컨테이너 이름으로 판단하지 않습니다. |
| master_host / master_port | Replica가 실제로 어느 노드를 복제하는지 봅니다. |
| master_link_status | up인지 down인지 확인합니다. |
| master_sync_in_progress | 전체 재동기화가 진행 중인지 확인합니다. |
| connected_slaves | Master에 연결된 Replica 개수입니다. |
| master_repl_offset / slave_repl_offset | 같은 복제 이력에서 처리 위치/지연을 비교할 때 사용합니다. |

offset은 쓰기 흐름의 바이트 위치 계열 지표입니다. 키 개수나 트랜잭션 개수가 아닙니다.
서로 다른 복제 이력/replication ID의 숫자만 직접 비교해서 데이터 일치를 단정하지 마세요.
DBSIZE가 같아도 값이 다를 수 있고, TTL 만료 시점으로 일시적으로 개수가 달라질 수도 있습니다.

## Replica 중단과 복구

`status`에서 Replica인 노드를 하나 선택합니다. 아래는 redis-2가 Replica일 때의 예입니다.

```bash
./lab.sh fault stop redis-2
./lab.sh cli master SET lab:replication:offline-demo added-while-replica-down
./lab.sh status
./lab.sh recover redis-2
./lab.sh cli redis-2 GET lab:replication:offline-demo
./lab.sh logs redis-2 --tail 100
```

기존 데이터와 replication backlog 범위 등에 따라 부분 재동기화 또는 전체 재동기화가 진행됩니다.
이번 짧은 중단에서 전체 동기화가 나오지 않았다고 실패가 아닙니다.
로그의 실제 PSYNC/full resync 결과를 기록하세요. 이 프로젝트는 로그를 특정 결과로 조작하지 않습니다.

## 비동기 복제와 WAIT

기본 Redis 복제는 비동기입니다. SET 성공 응답과 모든 Replica의 반영은 같은 사건이 아닙니다.
WAIT는 **같은 연결에서 앞서 수행한 쓰기**의 복제 확인을 기다립니다.

```bash
./lab.sh cli master
```

이제 열린 하나의 redis-cli 세션에서:

```text
SET lab:practice:wait same-connection
WAIT 2 1000
```

`WAIT 2 1000`은 최대 1,000ms 동안 Replica 2개의 확인을 기다리고, 실제 확인 수를 반환합니다.
`./lab.sh cli master SET ...`와 `./lab.sh cli master WAIT ...`를 별도 실행하면 서로 다른 연결이라
원하는 쓰기 보장 비교가 아닙니다. workload의 `--wait-replicas`는 같은 연결에서 실행하도록 구현했습니다.

```bash
./lab.sh workload --seconds 120 --rate 5 --wait-replicas 1
```

WAIT 결과가 0이어도 SET이 되돌려진 것이 아닙니다. 복제 확인과 강한 일관성은 다르고,
WAIT 자체로 모든 장애에서 데이터 무손실을 보장하지 않습니다.

## 복제와 백업을 혼동하지 않기

```bash
./lab.sh cli master INFO persistence
```

이 환경은 AOF `appendfsync everysec`와 RDB snapshot을 모두 켭니다.
복제는 다른 Redis 노드의 가용성을 높이는 수단이고, 영속화는 프로세스 재시작을 위한 저장입니다.
잘못된 DEL도 복제됩니다. 과거 시점으로 되돌릴 독립 백업은 따로 필요합니다.
AOF everysec도 최근 쓰기의 손실 가능성을 없애는 설정이 아닙니다.

Redis 7의 AOF는 appendonlydir 아래 base/incremental/manifest 형태일 수 있습니다.
하나의 `.aof` 파일만 임의 복사하여 완전한 백업이라고 판단하지 마세요.
이 실습의 backup 명령은 프로토콜로 RDB stream을 받아 별도 복원을 검증하는 방식입니다.

참고: [REFERENCES](../REFERENCES.md) R1/R2/R6/R7.
