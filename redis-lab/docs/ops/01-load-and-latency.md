# 부하와 지연을 읽는 방법

## 옵션

```bash
./ops.sh load --target perf --seconds 60 --rate 1500 --workers 16 \
  --keys 10000 --payload 512 --read-pct 80 --hot-pct 70
```

`read-pct`는 GET 비중, 나머지는 SET입니다. `hot-pct`는 요청 중 상위 1% 키 집합을 선택할 비율입니다. 완전한 Zipf 분포가 아니라 읽기 쉬운 두 구간 분포입니다. `payload`는 값 크기(byte), `keys`는 초기 키 수입니다. 고정 크기 benchmark와 다양한 크기의 fragmentation 시나리오는 목적이 다릅니다.

서버 처리량 자체를 인증하는 벤치마크가 아닙니다. Python 부하 발생기가 먼저 포화될 수 있습니다. 같은 rate에서 workers를 늘려도 개선이 없고 scheduler_skipped가 커지면 Redis만 탓하지 마세요. CPU 제한 시험에서도 runner 자체는 제한하지 않고 perf만 변경합니다.

## 카운터

| 필드 | 해석 |
|---|---|
| offered | 실제 스케줄러가 큐에 넣으려고 한 요청 수 |
| scheduler_skipped | 클라이언트 스케줄러가 늦어 아예 생성하지 못한 슬롯 수 |
| enqueued | 제한된 작업 큐에 들어간 요청 |
| queue_dropped | 작업 큐가 가득 차 생성 단계에서 드롭 |
| expired_before_send | 실행 종료 시점 전에 처리하지 못해 전송하지 않은 큐 항목 |
| attempted | 클라이언트가 연결/명령 실행을 시도한 작업 수 |
| command_send_attempted | 명령 바이트의 sendall을 시도한 수. 부분 전송/미응답일 수 있음 |
| command_not_sent | 연결/발견 실패 등으로 명령 sendall 이전에 실패 |
| success / error | 응답을 처리한 결과. 성공에는 GET miss도 포함 |
| writes_not_sent | 쓰기 명령 바이트를 보내기 전에 실패 |
| writes_uncertain | 전송/연결 계열 문제로 쓰기 결과를 확정하지 못함 |
| writes_rejected | Redis의 오류 응답을 받은 쓰기 |

`sendall` 성공은 Redis가 적용했다는 뜻이 아닙니다. 이 부하는 오류 뒤 **같은 SET을 재실행하지 않습니다.** 다음 요청에서 연결을 새로 만들며, HA 대상이면 Sentinel에서 주소를 재발견합니다. 응답 유실/중복 방지 의미는 기존 ID 기반 workload와 verify로 별도 확인합니다.

`rtt_ms`는 worker의 요청 실행 시작부터 응답/오류까지이며 연결 설정·Sentinel 발견이 포함될 수 있습니다. `scheduled_to_reply_ms`는 여기에 클라이언트 큐 대기까지 포함합니다. 드롭된 요청에 가짜 지연을 부여하지 않습니다. 두 분포 모두 처리 시도한 요청에 관한 값입니다.

p50/p95/p99는 로그 버킷의 **상단값 근사치**이며 약 3% 버킷 폭입니다. mean/max는 관측값에 기반합니다. 정확한 order-statistic 분위수나 coordinated-omission 없는 측정이라고 주장하지 않습니다. 모든 성공 요청의 원본을 무제한 기록하지 않고 시간별 집계와 최대 200개 오류 표본을 남깁니다.

## 네트워크 지연

```bash
./ops.sh run latency --delay-ms 40 --seconds 10 --yes
```

정상/지연/복구 3단계 각각의 결과가 같은 보고서에 기록됩니다. 프록시는 한 방향당 read chunk별 지연을 주므로 작은 단일 요청에서는 왕복이 대략 두 방향 지연만큼 늘어날 수 있지만, AUTH/큰 응답/분할전송에서는 정확히 두 배라고 단정할 수 없습니다.

Redis SLOWLOG는 명령 실행시간이며 네트워크 I/O 시간을 포함하지 않습니다. 따라서 client RTT와 서버 slowlog가 서로 다를 수 있습니다. [공식 SLOWLOG 설명](https://redis.io/docs/latest/commands/slowlog-get/)

프록시는 **perf 경로 전용**입니다. Sentinel Master 주소 반환이나 Replica 통신을 투명하게 프록시하지 않습니다. 프로토콜 수준 지연 주입이지 실제 WAN이나 패킷 손실 모델도 아닙니다. control API는 내부 8080, Bearer token 인증이 필요하고 호스트에 공개하지 않습니다.
