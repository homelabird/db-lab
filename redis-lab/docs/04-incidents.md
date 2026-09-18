# 04 · 증상별 장애 대응

원인 확인 전에 FLUSHALL, 데이터 볼륨 삭제, 무조건 REPLICAOF NO ONE, quorum 축소부터 하지 않습니다.
정상 동작을 가정하지 말고 서비스 접속 → 인증 → 역할 → 복제 → Sentinel → 저장/메모리 순서로 나눕니다.

## 1. 공통 진단 순서

```bash
./lab.sh status
./lab.sh faults
./lab.sh logs sentinel-1 --tail 150
./lab.sh logs redis-1 --tail 100
./lab.sh cli redis-1 ROLE
./lab.sh cli redis-1 INFO replication
./lab.sh cli redis-1 INFO memory
./lab.sh cli redis-1 INFO persistence
./lab.sh cli redis-1 INFO clients
./lab.sh cli redis-1 INFO stats
./lab.sh cli redis-1 SLOWLOG GET 20
./lab.sh collect
```

redis-1은 예시입니다. 대상 노드 이름을 실제 장애 노드로 바꾸세요.
대상 Redis가 멈췄으면 CLI 접속 자체가 실패하는 것이 진단 결과입니다. 그때는 컨테이너 상태와 로그를 봅니다.
SLOWLOG는 명령 실행시간 중심 지표이며 네트워크 왕복 전체 지연시간이 아닙니다.
workload의 latency_ms는 진단용 INFO/CONFIG 조회·SET·선택 WAIT·GET까지 포함하므로 성능 benchmark와 다릅니다.

| 증상 | 우선 확인 | 조치와 검증 |
|---|---|---|
| Connection refused/timeout | 컨테이너 상태, network, 로그, 현재 Master | 장애를 복구한 뒤 실제 SET/GET, 복제 연결, Sentinel 상태를 따로 확인 |
| READONLY | 접속 노드의 ROLE, 앱의 고정 주소/연결 풀 | 현재 Master 재발견, Sentinel 지원 클라이언트의 재접속 동작 확인 |
| NOAUTH/WRONGPASS | 어느 연결 구간이 실패하는지 | Redis/복제/Sentinel 간 인증을 구분해 맞춤; 전체 인증 해제는 하지 않음 |
| OOM command not allowed | used_memory, maxmemory, policy, evicted_keys | 데이터/TTL/정책/용량 진단; Redis 프로세스의 cgroup OOM 종료와 구분 |
| MISCONF | INFO persistence, RDB/AOF 로그, 저장 경로 | 저장 실패 원인을 제거하고 저장 성공·쓰기 재개 확인 |
| Master 죽었는데 승격 안 됨 | CKQUORUM, Sentinel 상호 발견, Replica 상태 | 다수/연결/후보 자격 복구; 무작정 quorum이나 role을 바꾸지 않음 |
| 프로세스는 정상, 복제 down | master_host, masterauth, 링크, 로그 | 올바른 복제 대상·인증 복원, 재동기화 완료 확인 |

## 2. Replica 인증 오류

Replica인 노드를 status에서 선택합니다.

```bash
./lab.sh fault auth-replica redis-2
./lab.sh cli redis-2 INFO replication
./lab.sh logs redis-2 --tail 100
./lab.sh recover redis-2
./lab.sh cli redis-2 INFO replication
```

스크립트는 선택된 Replica의 masterauth만 틀리게 바꾸고 기존 upstream 연결을 끊어 재인증을 유도합니다.
Redis 자체의 관리자 접속 비밀번호를 바꾸지 않으므로 CLI는 접속되지만 복제는 실패하는 차이를 볼 수 있습니다.
기존 복제 연결을 끊지 않으면 바뀐 비밀번호가 재연결 때까지 드러나지 않을 수 있습니다.
복구는 컨테이너 환경의 원래 비밀번호로 masterauth를 되돌리고 복제를 다시 연결합니다.

## 3. 메모리 한도: 프로세스는 살아 있는데 SET이 거부됨

이후 두 실습은 주 HA 구성과 **다른 독립 sandbox**에서 합니다. 각 회차 사이에 sandbox-recover를 실행하세요.

```bash
./lab.sh sandbox-oom
./lab.sh cli redis-sandbox PING
./lab.sh cli redis-sandbox INFO memory
./lab.sh cli redis-sandbox CONFIG GET maxmemory maxmemory-policy
./lab.sh sandbox-recover
```

이 시나리오는 현재 사용량보다 약 2 MiB 높은 maxmemory, noeviction을 적용한 뒤
16 KiB 값들을 제한된 수만큼 넣어 Redis의 메모리 쓰기 거부를 재현합니다.
데이터 주입 루프는 최대 16 MiB이며 호스트 메모리를 무제한 채우지 않습니다.
실제 Redis OOM 오류가 관찰되지 않으면 성공으로 표시하지 않습니다.

recover는 이 시나리오가 만든 `lab:sandbox:oom:fill:*`만 제거하고 기존 메모리 설정을 되돌립니다.
전체 데이터 FLUSHALL을 하지 않습니다. 키 삭제 후에도 프로세스 RSS가 곧바로 같은 폭으로 줄어들지는
않을 수 있습니다. allocator/fragmentation/OS 반환 여부와 used_memory를 구분해서 관찰하세요.

실제 컨테이너 OOMKilled와의 차이는 `podman inspect rslab-redis-sandbox`의 State와 로그로 확인합니다.
**호스트/컨테이너 OOM을 강제 유발하는 스크립트는 기본 제공하지 않습니다.**

## 4. RDB 저장 실패와 MISCONF

```bash
./lab.sh sandbox-persistence
./lab.sh cli redis-sandbox INFO persistence
./lab.sh cli redis-sandbox SET lab:test value
./lab.sh logs redis-sandbox --tail 100
./lab.sh sandbox-recover
./lab.sh cli redis-sandbox INFO persistence
./lab.sh cli redis-sandbox SET lab:test recovered
```

실습은 sandbox 안에서 RDB 파일 목적지 `dump.rdb`를 디렉터리로 만들어 rename이 실패하도록 합니다.
기존 RDB가 있으면 임시 보존하고 복구 때 되돌립니다. 호스트 디스크를 채우지 않고 파일시스템 장애를 재현합니다.
이는 **권한 오류나 디스크 full 자체를 재현하는 것은 아니며**, RDB 저장 실패와 그 이후 동작을 연습합니다.

복구에서는 방해 디렉터리를 제거하고 BGSAVE를 다시 성공시킨 뒤 실제 쓰기 재개를 확인합니다.
`CONFIG SET stop-writes-on-bgsave-error no`로 경보/제한만 우회하지 않습니다.
AOF가 켜져 있어도 RDB 저장 실패와 관련된 쓰기 제한을 관찰할 수 있습니다.

## 5. 복구 완료의 기준

서비스 복구: 현재 Master에 새 요청을 쓰고 읽을 수 있습니다.
이중화 복구: 원래 중단된 노드가 돌아와 새 Master를 정상 복제합니다.
감시 복구: Sentinel 3대가 동일 Master를 인식하고 CKQUORUM이 성공합니다.
데이터 검증: workload 성공 응답과 키/값을 비교합니다. 서비스 재개만으로 이전 쓰기의 생존을 단정하지 않습니다.

참고: [REFERENCES](../REFERENCES.md) R1/R2/R6/R10/R11.
