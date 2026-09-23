# Redis Operations Lab · 2.0

Redis 기본 동작과 Sentinel 장애조치에 **지속 부하, 병목, 메모리 단편화, 자원 제한, 원인별 대응과 결과 기록**을 추가한 Podman Compose 실습환경입니다.

각 perf scenario의 dev 재현과 staging/prod Redis 조회 명령은 [시나리오별 대응 쿼리](../docs/SCENARIO-RESPONSE-QUERIES.md#redis-perf-scenario-대응)를 참고하세요.

> **검증 경계:** 제작 환경의 170개 정적·단위·회귀·로컬 TCP 검사는 통과했습니다. TCP 검사는 작은 테스트 서버/echo 서버를 사용합니다. 실제 Redis 서버와 Podman은 실행기가 없어 **BLOCKED(미검증)**입니다. `TESTING.md`와 `tests/evidence/`에 실제 실행 결과를 구분해 두었습니다. 이 압축파일은 소스·설정·교재이며 컨테이너 이미지나 사전 생성 데이터셋을 포함하지 않습니다.

## 2026-09-21 실행 수정

Redis Cluster의 생성 Compose 빌드 경로, cluster 볼륨 검사, 7개 이상 노드/IP 초기화,
클라이언트 생성과 CLUSTER INFO 판독, 키 소유 노드의 단일 연결 WAIT 검사를 수정했습니다.
`cluster-init`은 노드 응답을 기다린 뒤 빈 클러스터만 생성하고, 이미 정상인 클러스터는 데이터를
그대로 둔 채 검증합니다. 일부만 초기화되었거나 데이터가 있는 불완전한 클러스터를 자동 초기화하지 않습니다.

기본 Sentinel 실습은 `bash lab.sh init` → `bash lab.sh doctor` → `bash lab.sh up`입니다.
Cluster를 새로 구성할 때만 최초 기동 전 `.env`에서 `DEPLOYMENT_MODE=cluster`와
`CLUSTER_NODE_COUNT`, `CLUSTER_REPLICAS`, `CLUSTER_BASE_IP`를 정한 뒤
`bash lab.sh up` → `bash lab.sh cluster-init` → `bash lab.sh health --json`을 실행하세요.
예: 6노드/replicas=1 또는 3노드/replicas=0. 기본 설정의 비밀번호를 복사해서 덮어쓰지 마세요.
기존 Sentinel 볼륨이 있는 디렉터리의 모드를 임의로 변경하지 마세요. `ops.sh` 확장 실습은 Sentinel 구성을 전제로 합니다.

[이번 수정·검증 범위](../docs/STARTUP-REPAIR-2026-09-21.md)를 먼저 확인하세요. 아래의 초기 패키지명과
170개 검사 수치는 이전 버전 기록입니다. 통합 ZIP에서는 `cd redis-lab` 후 명령을 실행하면 됩니다.

## 1. 처음 실행

호스트: Linux, Python 3.10 이상, Podman, `podman-compose`. CPU 제한 실험은 cgroup v2와 해당 제어 권한이 필요합니다. 이미지 최초 다운로드/빌드에는 인터넷이 필요합니다. 호스트 Python에 Redis 패키지를 설치할 필요는 없습니다.

기본 노드와 확장 노드를 함께 실행할 때 여유 RAM **4~6GiB**를 실습용 설계 예산으로 잡으세요. 이것은 실측 성능 보장이 아닙니다. 다른 DB 실습을 동시에 실행한다면 자원을 더 확보하세요.

```bash
unzip redis-operations-lab.zip
cd redis-operations-lab
chmod +x lab.sh ops.sh

# 기본 HA 7개 + 확장 3개를 빌드하고 시작
./ops.sh up

./lab.sh status
./ops.sh doctor
./ops.sh list
```

`LAB_NAME=rslab`, 기본 네트워크는 `10.89.77.0/24`입니다. 호스트/VPN/다른 Podman 네트워크와 겹치면 **최초 기동 전에** `.env`의 subnet과 기존 8개 IP를 함께 바꾸세요. 확장 노드 3개는 이 네트워크에서 동적 주소/DNS를 사용합니다. 비밀번호는 최초 실행 시 `.env`에 무작위 생성됩니다. `.env`를 외부에 공유하지 마세요.

## 2. 구성

| 영역 | 구성 | 목적 |
|---|---|---|
| 기존 HA | Redis 3 + Sentinel 3 + lab-client | 기본 자료구조, 복제, Master 전환, quorum, 인증, 백업/복원 |
| 확장 실험 | redis-perf | Sentinel에 감시되지 않는 독립 Redis. 위험한 메모리·병목 실험 전용 |
| 실행·관측 | ops-runner | 부하, 실험, 지표/독립 PING, 보고서 생성 |
| 네트워크 | net-proxy | ops client ↔ redis-perf 사이에만 지연/대역폭/연결 차단 주입 |

**Sentinel은 프록시가 아니며, 단일 호스트 컨테이너 3개는 호스트 장애를 견디는 운영 HA가 아닙니다.** 기존 HA 기능 설명은 `README-HA.md`와 `docs/01-basics.md`부터 이어집니다. 관련 공식 문서는 `docs/ops/REFERENCES.md`를 참고하세요.

기본 호스트 공개 포트는 없습니다. `podman exec`를 감싼 `lab.sh`/`ops.sh`로 접근합니다. named volume만 사용하며 SELinux 비활성화, privileged, 호스트 Docker/Podman 소켓 마운트는 사용하지 않습니다.

## 3. 바로 해볼 실험

```bash
# 평상시 부하. 평균이 아니라 p50/p95/p99, 오류, 큐 드롭도 기록
./ops.sh load --seconds 30 --rate 300 --workers 8

# 동일 workload로 수행한 두 Redis 실행 결과 비교
./ops.sh results
./ops.sh compare output/ops-BASELINE/RUN_ID/report.json output/ops-CANDIDATE/RUN_ID/report.json

# 큰 Hash 전체조회 vs 필요한 필드만 조회, DEL vs UNLINK
./ops.sh run bigkey --yes

# 단편화 생성 → 무조치 → MEMORY PURGE → active defrag → 생존 데이터 검증
./ops.sh run fragmentation --mib 64 --seconds 30 --yes

# noeviction 쓰기 거부 vs allkeys-lru 퇴출
./ops.sh run eviction --yes

# maxclients 연결 제한
./ops.sh run connections --yes

# 데이터를 읽지 않는 Pub/Sub 소비자의 출력 버퍼 문제
./ops.sh run slow-consumer --seconds 20 --yes

# 쓰기 부하 중 BGSAVE / copy-on-write 관찰
./ops.sh run persistence --mib 48 --yes

# 프록시의 정상 → 지연 → 복구 3단계 비교
./ops.sh run latency --delay-ms 40 --seconds 10 --yes

# 동시 캐시 미스와 중복 원본 조회: naive vs singleflight
./ops.sh run cache-stampede --workers 24 --yes

# ACK 전 사라진 소비자: pending 회수와 중복 처리 방지
./ops.sh run stream-pending --yes
```

`run`은 실험 설정을 저장한 다음 변경하며 종료 시 원래 설정을 복구하고 해당 실험 키만 지웁니다. 단, **eviction은 독립 perf 노드의 다른 기존 키도 퇴출할 수 있습니다.** `redis-perf`에 보존할 데이터를 넣지 마세요. 시간/메모리/요청 상한이 있고, 재현 조건을 만족하지 못하면 성공으로 포장하지 않습니다.

## 4. CPU / 복제 / HA 실습

```bash
# 실제 cpu.max 적용 및 cpu.stat throttling 증가까지 확인
./ops.sh cpu --seconds 15 --rate 5000 --yes

# HA Replica 하나 중단 → 작은 backlog보다 많은 쓰기 → 재동기화 관찰
./ops.sh replication --yes

# 기존 실제 Master 강제 종료·전환·복구 시험
./ops.sh ha-test --yes
```

HA 부하 중 수동 장애를 관찰할 수도 있습니다.

```bash
# 터미널 A: Sentinel에서 Master를 발견하는 혼합 부하
./ops.sh load --target ha --seconds 120 --rate 500 --workers 16

# 터미널 B: 현재 Master 강제 종료
./lab.sh fault kill-master
./lab.sh status
./lab.sh recover
```

혼합 부하의 SET 응답은 자동 재시도하지 않습니다. 성능 부하는 유실/중복을 입증하는 시험이 아닙니다. **고유 요청 ID별 쓰기 결과 검증**은 기존 `./lab.sh workload`, `./lab.sh verify latest`를 사용하세요.

## 5. 다른 터미널에서 관찰

```bash
./ops.sh collect --seconds 180

./ops.sh cli INFO memory
./ops.sh cli INFO clients
./ops.sh cli INFO persistence
./ops.sh cli SLOWLOG GET 10
./ops.sh cli LATENCY LATEST
./ops.sh cli CLIENT LIST
```

각 실험은 `metrics.jsonl`, `events.jsonl`, `probe.jsonl`, `report.json`, `report.html`을 필요한 범위에 맞게 만듭니다. 부하 시험은 `load-timeseries.jsonl`과 제한된 오류 표본도 남깁니다. 시간이 모두 UTC로 기록됩니다. **자동 validate 동안에는 다른 부하/실험/수집을 함께 실행하지 마세요.**

```bash
# 실행이 끝난 후 결과를 호스트 output/으로 복사
./ops.sh results
cat output/ops-latest-export.json
```

생성된 `output/ops-날짜-번호/<run-id>/report.html`을 브라우저에서 여세요. 외부 JS/CDN 없이 열립니다. CPU/복제 결과 HTML은 `output/cpu-*/`, `output/replication-*/`에 바로 생성됩니다.

| 상태 | 의미 | 종료 코드 |
|---|---|---:|
| PASS | 해당 시나리오의 관측·검증 조건 통과 | 0 |
| FAIL | 실행/검증/복구 실패 | 1 |
| BLOCKED | 실행기·권한·빌드 지원 등 전제 부족 | 2 |
| NOT_REPRODUCED | 실험은 끝났지만 목표 현상이 충분히 관측되지 않음 | 3 |
| RUNNING | 실행 중이거나 비정상 종료로 완료 보고서 없음 | 해당 없음 |

`load`의 PASS는 요청이 실제 성공한 실행 완료 상태이지 무오류나 목표 처리량 달성 판정이 아닙니다. `collect`의 PASS도 수집 완료이지 모든 노드 정상 판정이 아닙니다. 보고서의 note와 개별 지표를 읽으세요.

## 6. 중단·복구·종료

```bash
# 강제 중단 뒤 남은 설정/CPU/복제 복구 기록 정리
./ops.sh recover --yes

# 확장 컨테이너부터 종료. 기존 HA와 다른 DB 실습은 건드리지 않음
./ops.sh down
./lab.sh down
```

perf 시나리오가 실행 중이면 recovery는 잠금 때문에 거부됩니다. 우선 해당 명령을 종료하고 다시 확인하세요. 네트워크 주입 규칙은 최대 120초 후 자동 만료됩니다. 강제 SIGKILL/호스트 종료는 finally 실행을 보장하지 않으므로 복구 저널이 남습니다. `docs/ops/05-safety.md`를 참고하세요.

```bash
# 결과를 먼저 export. 확장 실험 노드 데이터와 결과 볼륨만 삭제
./ops.sh results
./ops.sh reset --yes

# HA까지 완전히 초기화할 때만 별도로 실행
./lab.sh reset --yes
```

`redis-perf`는 재시작 때 기본 설정을 재생성하지만, BGSAVE로 만들어진 합성 RDB는 다시 로드될 수 있습니다. 완전히 깨끗한 메모리/할당기 비교는 결과 내보내기 후 `ops.sh reset --yes`, `ops.sh up --only`로 시작하세요. **기존 HA 볼륨을 성능 실험 목적으로 초기화하지 마세요.**

## 7. 실구동 검증

```bash
# 확장 실험 9개. 일부는 실제 현상 미재현(3), 미지원(2)일 수 있음
./ops.sh validate --yes
cat output/ops-live-validation.json

# 기본 HA/인증/저장/백업 복원 검증은 별도
./lab.sh validate --yes --no-build

# 제작 환경에서도 실행한 오프라인 검사
bash tests/check.sh
```

CPU 제한과 복제 resync, HA 전환은 위 확장 9개 자동 검증과 별도입니다. 통합 검증 결과는 실제 실행마다 새로 기록하며, 이전 PASS를 새 실행의 결과로 재사용하지 않습니다.

## 8. 기존 수정판에서 이동

기존 폴더를 백업한 다음 이 소스를 새 폴더에 풀고, 기존 `.env`와 `.lab/`을 새 폴더에 복사하세요. 같은 LAB_NAME/주소/비밀번호로 기존 HA named volume을 이어 씁니다. 예전 폴더와 새 폴더를 동시에 조작하지 마세요.

기존 HA가 정상 실행 중이면 확장만 시작할 수 있습니다.

```bash
./ops.sh up --only
```

기존 HA 이미지/설정은 그대로 두고 확장 이미지 두 개만 빌드합니다. 데이터량과 복구 상태를 먼저 확인하세요. `--no-build`는 필요한 모든 이미지를 이미 빌드한 뒤에만 사용합니다.

## 교재

- `docs/ops/00-start.md`: 순서별 실습과 정상 기준.
- `docs/ops/01-load-and-latency.md`: 부하 수치·지연·드롭·연결 결과 해석.
- `docs/ops/02-fragmentation.md`: 단편화 실험 설계와 판정 기준.
- `docs/ops/03-incident-runbooks.md`: 증상별 진단과 조치.
- `docs/ops/04-ha-and-replication.md`: 부하 중 전환과 전체 재동기화.
- `docs/ops/05-safety.md`: 변경 범위, 상한, 복구 저널, 미구현 영역.
- `docs/ops/worksheet.md`: 기록용 빈 실습지.

이 실습의 RESP2 클라이언트는 외부 Python 의존성 없이 동작하는 **교육용 최소 구현**입니다. TLS/Cluster/트랜잭션/운영 연결 풀을 제공하는 범용 클라이언트가 아닙니다. 기존 기본 HA 클라이언트는 redis-py를 계속 사용합니다.

## 2026-09-18 health 어댑터

`bash lab.sh health --json`은 읽기 전용 topology snapshot을 검사하여 부족한 노드/Sentinel 표본, 복제 연결, 역할, 클러스터 슬롯 이상을 오류로 반환합니다.
기존 `wait`/`verify`의 데이터 쓰기 또는 내구성 시험과 별개입니다. 이 변경은 Compose 복제 토폴로지를 새로 구성하지 않습니다.
Helm용 새 토폴로지는 `../helmchart`의 별도 구현이며 Compose 테스트 통과를 Helm 검증으로 해석하면 안 됩니다.
