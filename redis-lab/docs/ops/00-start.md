# 학습 순서와 정상 기준

## 1회차 — 평상시 기준을 먼저 저장

```bash
./ops.sh up
./lab.sh demo
./lab.sh seed --users 2000 --payload-bytes 256
./lab.sh status
./ops.sh load --seconds 30 --rate 100 --workers 4
./ops.sh load --seconds 30 --rate 500 --workers 8
./ops.sh load --seconds 30 --rate 2000 --workers 16
./ops.sh results
```

같은 호스트에서도 데이터 수, CPU quota, 다른 컨테이너 부하에 따라 수치가 바뀝니다. 특정 수치가 정답이라고 생각하지 말고 **목표 RPS 대비 실제 성공 RPS**, 오류, 큐 드롭, p99를 함께 기록하세요. 오류/드롭이 생기는 구간에서는 요청률을 낮춰 회복되는지 확인합니다. seed는 각 load run에 다른 prefix를 사용하고 종료 후 정리하며, 강제 종료를 대비해 TTL도 설정합니다.

## 2회차 — 원인별 실험

bigkey → connections → eviction → slow-consumer 순서로 실행합니다. 명령만 보고 답을 외우지 말고, 다른 터미널에서 INFO/SLOWLOG/CLIENT LIST를 관찰하세요. 각 시나리오에는 자체 설정 복구와 독립 PING probe가 있습니다.

## 3회차 — 메모리 문제를 분리

fragmentation → persistence 순서로 실행합니다. `used_memory` 증가, RSS 증가, allocator 빈 공간, BGSAVE COW를 같은 문제로 분류하지 마세요. 실제 단편화가 관측되지 않으면 `NOT_REPRODUCED`입니다. 더 큰 데이터를 쓰기 전 호스트 여유 메모리를 확인하세요.

## 4회차 — 서비스 동작과 HA

latency → cache-stampede → stream-pending → replication → ha-test를 실행합니다. 정상 Redis여도 네트워크나 클라이언트가 느릴 수 있고, Master 전환 완료와 쓰기 재개 시각은 다릅니다.

## 기본 접속

`./ops.sh cli ...`는 언제나 redis-perf입니다. `./lab.sh cli master ...`는 현재 HA Master, `./lab.sh cli redis-2 ...`는 이름이 고정된 HA 노드입니다. 두 환경을 혼동하지 마세요.

```bash
./ops.sh cli ROLE
./lab.sh cli master ROLE
./lab.sh cli sentinel-1 SENTINEL GET-MASTER-ADDR-BY-NAME mymaster
```

`MASTER_NAME`을 바꿨다면 위 `mymaster`도 실제 이름으로 변경합니다.
