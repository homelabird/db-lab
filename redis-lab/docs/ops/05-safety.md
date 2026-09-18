# 변경 범위와 복구

## 기본 제한

| 항목 | 상한/범위 |
|---|---|
| perf Redis maxmemory | 기본 256MiB, 컨테이너 선언 640MiB |
| runner / proxy 컨테이너 선언 | 각각 384MiB / 96MiB |
| 부하 | 600초, 목표 10,000 RPS, 64 worker |
| seed | 50,000 keys, 값 8KiB, 원시 데이터 합계 96MiB 이하 |
| fragmentation / persistence 규모 | 16~96MiB 옵션 범위, 별도 현재 메모리 점검 |
| bigkey | 1,000~200,000 hash fields |
| slow subscriber | 최대 64MiB 발행, 시간 제한 |
| 프록시 | per-direction 지연 0~1,000ms, 규칙 TTL 최대 120초, 연결 128개 |
| 실험 | runner 내부 SIGALRM 600초, 개별 소켓 timeout, 호스트 900초 제한 |

메모리 선언이 실제로 적용됐는지는 호스트의 cgroup/Podman 설정에 달려 있습니다. CPU 실험은 cpu.max 적용값을 읽고 cpu.stat 변화를 확인하며, 불가능하면 BLOCKED 처리합니다. 컨테이너 한도와 Redis maxmemory는 같은 지표가 아닙니다. [Podman update](https://docs.podman.io/en/latest/markdown/podman-update.1.html)

## 소유권

기존 HA는 `io.redis-sentinel-lab.id`, 확장 노드는 `io.redis-sentinel-lab.ops` 라벨로 검증합니다. 임의 컨테이너 이름/볼륨 이름은 허용하지 않습니다. 원본 호스트 파일 경로를 Redis에 bind mount하거나 운영 Redis 주소를 입력받는 기능은 제공하지 않습니다.

실험 키는 run별 `ops:<run-id>:` prefix를 사용합니다. 정상 정리는 SCAN + 제한된 UNLINK 배치이며 FLUSHALL/FLUSHDB를 사용하지 않습니다. 예외는 데이터 정책 실험에서 eviction 자체가 기존 perf 키를 제거할 수 있다는 점입니다. 오직 버려도 되는 perf 데이터만 두세요.

## 저널

| 저장 위치 | 내용 |
|---|---|
| ops-results 볼륨의 `/results/ops/recovery.json` | 원래 Redis 런타임 설정, server run ID, 실험 prefix |
| `.lab/ops-cpu-recovery.json` | 변경 전 cpu.max와 정확한 컨테이너 ID |
| `.lab/ops-replication-recovery.json` | 원래 HA backlog와 대상 노드 |
| 기존 `.lab/faults.json` | 기본 HA 장애 주입/복구 기록 |

원래 설정을 저장한 뒤 변경하며 복구 후 CONFIG GET으로 재확인합니다. 복구 실패 시 저널을 남기고 FAIL 처리합니다. perf Redis가 재시작되어 run ID가 바뀌었다면 오래된 설정을 다른 실행에 덮어쓰지 않습니다. 해당 경우 재시작 기본값을 사용하고 예전 실험 prefix만 정리합니다.

동일 runner의 실험은 파일 잠금으로 직렬화합니다. 복구도 같은 잠금을 사용합니다. 메모리 퇴출로 사라질 수 있는 Redis 키를 유일한 잠금으로 쓰지 않습니다. 일반 load/collect는 병행할 수 있지만, 자동 validate/CPU/replication 측정 때는 병행하지 마세요.

## 강제 중단

Ctrl+C나 SIGTERM은 복구를 시도하지만 `podman exec`가 전달하는 신호는 환경에 따라 차이가 있을 수 있습니다. SIGKILL, 호스트 정전, 컨테이너 런타임 중단은 finally를 실행하지 못할 수 있습니다. 프록시 규칙은 자동 만료되며 나머지는 명시적으로 복구합니다.

```bash
./ops.sh recover --yes
```

실험이 아직 실행 중이라 잠금을 잡고 있으면 이 명령은 거부됩니다. 실행 중 프로세스를 확인하고, 필요하다면 **소유권을 확인한 ops-runner만** 재시작한 뒤 복구하세요. `ops.sh up --only --no-build`는 컨테이너가 단순 정지한 경우 다시 시작하는 경로입니다. `recovery.json`을 무작정 지우지 마세요.

기존 `.env`와 `.lab`을 새 패키지로 옮기면 이름·인증·주소가 유지됩니다. 임의로 비밀번호를 변경하고 기존 HA 볼륨을 재사용하지 마세요.

## 이번 범위에 포함하지 않은 것

Prometheus/Grafana 서버, exporter, memtier, 별도 실제 DB/HTTP 업무 서비스는 넣지 않았습니다. 대신 내장 bounded workload, JSONL 수집, 독립 PING, 오프라인 HTML 보고서를 제공합니다. cache-stampede의 원본은 runner 내부 모의 처리입니다.

HA 경로 전체를 통과하는 프록시, 비대칭 네트워크 split-brain, 호스트 장애, 실제 디스크 full, 컨테이너 OOM kill, 파일시스템 I/O throttling, Redis Cluster, TLS/세분화 ACL, 운영 잠금 정확성 증명도 자동화 범위 밖입니다. 기존 `lab.sh fault isolate/pause`는 별도 수동 실습이며 환경별 지원을 확인해야 합니다.

이러한 영역을 검증했다고 보고서에 표시하지 않습니다. `ops.sh validate`의 PASS는 그 실행의 9개 perf 시나리오 조건에 한정됩니다.
