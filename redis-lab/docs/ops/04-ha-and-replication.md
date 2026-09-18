# 부하 중 장애조치와 복제

## 읽기/쓰기 부하와 전환

```bash
# 터미널 A
./ops.sh load --target ha --seconds 180 --rate 500 --workers 16
# 터미널 B
./ops.sh collect --seconds 180
# 터미널 C
./lab.sh fault kill-master
./lab.sh status
./lab.sh logs sentinel-1 --tail 100
./lab.sh recover
```

각 worker는 고정 Redis 주소가 아니라 Sentinel에서 Master를 발견합니다. 연결/명령 오류가 발생하면 해당 요청은 그대로 실패/불확실로 기록하며 다음 요청에서 재발견합니다. 현재 요청을 자동으로 다시 쓰지 않습니다. 최소 RESP2 클라이언트이므로 운영용 retry/backoff/pooling의 모범 구현은 아닙니다.

Sentinel이 보는 Master와 실제 쓰기 가능한 노드를 구분하고, role 변화와 첫 성공 요청 시점을 비교하세요. 정상 서비스 복구와 Replica 2대의 재합류는 다른 시각일 수 있습니다. Redis 복제는 기본적으로 비동기입니다. [Replication](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/) · [Sentinel](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/)

## 데이터 결과까지 검증

기존 클라이언트는 Redis 외부 저널에 고유 request ID를 남깁니다.

```bash
./lab.sh workload --seconds 120 --rate 5
# 다른 터미널에서 fault kill-master / recover
./lab.sh verify latest
./lab.sh results
```

혼합 benchmark는 같은 키를 덮어쓰므로 write loss를 정확히 판별하는 도구가 아닙니다. 위 고유 ID 검증을 병행하세요. ACK, 오류 응답, 미응답을 같은 사건으로 세지 마세요.

## Replica 전체 재동기화

```bash
./ops.sh replication --yes
```

현재 Master와 Replica를 동적으로 찾습니다. Master의 backlog 크기를 저장하고 64KiB로 축소한 뒤 Replica 한 대를 stop합니다. 다른 Replica는 계속 둡니다. 4,000개 × 512 byte의 seed와 8초 쓰기 부하를 발생시켜 backlog를 넘게 만든 후 원래 Replica를 복구합니다. master의 sync_full 증가와 Replica link up을 확인하고 원래 backlog 크기를 되돌립니다.

전체 동기화가 안 보이면 NOT_REPRODUCED입니다. sync_full delta는 Master 측 통계이며 다른 작업을 동시에 실행하면 원인 귀속이 어려워집니다. 자동 실험 중에는 다른 장애를 주입하지 마세요. 부분 재동기화는 backlog뿐 아니라 replication ID와 offset 이력 조건에도 의존합니다. [Replication](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/)

실패/중단 시 `.lab/ops-replication-recovery.json`에 원래 backlog와 노드가 남습니다. 이 실험은 HA 노드를 변경하므로 다음 명령으로 복구하세요.

```bash
./ops.sh recover --yes
./lab.sh status
```

## 기존 전체 검증

`./lab.sh validate --yes`는 Master 종료·quorum 부족·인증·저장 오류·백업 복원 등을 검사합니다. 이때 기존 redis-sandbox 데이터가 백업본으로 교체될 수 있습니다. `./ops.sh validate --yes`는 독립 perf 시나리오 9개이며 두 검증은 다릅니다.
