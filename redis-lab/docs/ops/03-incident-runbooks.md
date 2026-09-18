# 증상 → 증거 → 조치 → 재검증

이 문서의 명령은 **독립 redis-perf** 대상입니다. 실험 스크립트가 종료하면 설정을 원복하므로 관찰은 다른 터미널에서 진행하세요. 초기화 전에 결과부터 `./ops.sh results`로 내보냅니다.

## 큰 키 때문에 다른 요청도 늦어짐

```bash
./ops.sh run bigkey --fields 100000 --yes
# 다른 터미널
./ops.sh cli SLOWLOG GET 20
./ops.sh cli INFO commandstats
./ops.sh cli INFO memory
```

HGETALL/HMGET 차이, DEL/UNLINK 시간, 독립 probe 지연을 비교합니다. `HGETALL`을 작은 범위 조회와 동일한 작업이라고 보지 마세요. DEL/UNLINK는 한 번씩의 관측이므로 어느 쪽이 항상 빠르다고 단정하지 않습니다. UNLINK 후에는 논리적 키 삭제와 실제 메모리 회수 완료가 다를 수 있습니다. [UNLINK](https://redis.io/docs/latest/commands/unlink/)

## 쓰기만 OOM 오류 — 프로세스는 살아 있음

```bash
./ops.sh run eviction --yes
./ops.sh cli INFO memory
./ops.sh cli INFO stats
./ops.sh cli CONFIG GET maxmemory maxmemory-policy
```

이 실험은 Redis maxmemory에 따른 거부와 eviction을 비교합니다. cgroup OOM kill이 아닙니다. noeviction 거부 후 allkeys-lru로 바꾸면 기존 키도 퇴출될 수 있습니다. 실제 서비스에서는 캐시인지 원본 데이터인지 먼저 구분해야 하며, 단순히 퇴출 정책을 바꾸는 것은 데이터 보존 해결책이 아닙니다. [Eviction](https://redis.io/docs/latest/develop/reference/eviction/)

## 연결이 너무 많음

```bash
./ops.sh run connections --yes
./ops.sh cli INFO clients
./ops.sh cli INFO stats
./ops.sh cli CONFIG GET maxclients
./ops.sh cli CLIENT LIST
```

연결 수와 rejected_connections 증가를 함께 확인합니다. 연결 풀/연결 누수/요청마다 재접속 여부를 구분합니다. maxclients 증가만으로 OS FD나 메모리 제약이 없어지는 것은 아닙니다. [Client handling](https://redis.io/docs/latest/develop/reference/clients/)

## 느린 Pub/Sub 소비자

```bash
./ops.sh run slow-consumer --seconds 20 --yes
./ops.sh cli CLIENT LIST
./ops.sh cli INFO clients
./ops.sh logs redis-perf
```

`name=ops-slow-consumer`의 omem/oll/obl을 관찰합니다. 소비자는 subscribe 응답 후 더 읽지 않으며 발행자는 최대 64MiB를 제한된 시간 동안 보냅니다. 작은 출력 버퍼 제한을 넘어서 연결이 사라지는지 판정합니다. 순간적인 버퍼 peak를 샘플러가 놓칠 수 있으므로 연결 소멸과 로그를 함께 봅니다. Pub/Sub 재연결은 지난 메시지를 복구하지 않습니다. 중요 메시지는 별도 재전송/저장 모델을 검토해야 합니다. [Pub/Sub](https://redis.io/docs/latest/develop/pubsub/)

## BGSAVE 중 메모리·응답 변화

```bash
./ops.sh run persistence --mib 48 --yes
./ops.sh cli INFO persistence
./ops.sh cli INFO memory
```

RDB 작업 중 같은 크기의 값을 계속 덮어쓰며 COW를 관찰합니다. 완료 상태와 불변 marker도 확인합니다. 작은 데이터/빠른 저장장치에서는 스파이크가 짧을 수 있습니다. last_cow, current_cow, 독립 probe와 원본 metrics를 비교하세요. 호스트 디스크를 채우거나 무제한 데이터를 생성하지 않습니다. [Persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)

## 캐시 만료 후 원본 조회 폭증

```bash
./ops.sh run cache-stampede --workers 24 --yes
```

naive/singleflight의 원본 호출 횟수와 요청 지연을 비교합니다. 모의 원본은 runner 안의 4개 동시 슬롯과 60ms 지연입니다. 실제 DB가 아니며 계산한 TPS를 DB 성능으로 해석하면 안 됩니다. SET NX PX와 토큰 비교 삭제는 이 단일 Redis 실험에서만 사용합니다. Sentinel failover 환경의 잠금 안전성을 증명하지 않습니다. [Distributed lock cautions](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)

## 소비자가 ACK 전에 사라짐

```bash
./ops.sh run stream-pending --yes
```

40개를 생성하고 20개를 consumer-a에 전달합니다. 10개 효과를 기록한 뒤 ACK를 하지 않아 pending으로 남깁니다. consumer-b가 XAUTOCLAIM으로 회수하며 이미 처리한 ID를 중복 적용하지 않고 ACK합니다. 마지막 20개는 아직 배달하지 않은 메시지이므로 pending 0이 stream 비어 있음과 같은 뜻이 아닙니다. [XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/)

## 복구 공통 기준

프로세스 PING, 업무 요청 성공, 원래 설정 복구, 복제/감시 복구, 데이터 검증을 구분합니다. 오류 로그 하나가 사라졌다는 이유로 모두 정상으로 판정하지 마세요. perf 시나리오 실패 시 report.recovery.ok와 recovery.json 존재 여부부터 봅니다.
