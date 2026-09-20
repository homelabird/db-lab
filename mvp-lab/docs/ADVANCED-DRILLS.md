# DB 연동 시뮬레이션 v3 — 네트워크·자원·복원 실습

작성 기준: 2026-09-19. **구현과 호스트 테스트 통과는 실제 DB/커널 검증과 다릅니다.** 이 배포를 만든 환경에는 Docker가 없어 컨테이너 기동, 실제 패킷 손실, 실제 ENOSPC/OOM/1213, 이미지별 복원은 미실행입니다.

## 무엇이 추가됐나

기존 API/worker와 DB 4개, 총 6개 기본 컨테이너는 유지합니다. 기존 11개 `simulate`에 네트워크 실습 2개를 추가했고, 별도 `drills`에는 폐기용 DB 실습 5개를 추가했습니다. 별도 HA 프로젝트와 Helm은 이번에 변경하지 않습니다.

| 명령 대상 | 변경 대상 | 확인 기준 |
|---|---|---|
| `simulate run db-network-delay` | 원본 MVP MariaDB 컨테이너의 eth0 송신 큐만 | netem 설정+처리 패킷+관측 지연 증가+복원 후 주문 전체 필드 일치 |
| `simulate run db-network-loss` | 같은 송신 큐, 10% 확률 패킷 손실 | kernel qdisc drop 카운터>0+복원 후 전체 필드 일치 |
| `drills run backup-restore` | 원본은 SELECT, 별도 MariaDB에 논리 복원 | 두 테이블 모든 행의 해시+새 주문/중복 요청/상태 변경 canary |
| `drills run upgrade-restore` | 이미 내려받은 다른 MariaDB 이미지의 새 임시 DB | 같은 논리 데이터 복원+canary. 기존 볼륨 in-place 업그레이드 아님 |
| `drills run redis-disk-full` | 별도 Redis, 16MiB tmpfs AOF 저장 경로 | Redis AOF write err + 서버 ENOSPC 로그. 연결 오류만으로 통과하지 않음 |
| `drills run redis-oom` | 별도 Redis, 64MiB 메모리 한도 | OOMKilled=true, ExitCode=137, 프로세스 종료 모두 확인 |
| `drills run deadlock` | 이번 실행이 만든 주문 두 행 | 두 연결의 역순 잠금, 한 1213 victim+한 survivor, 두 연결 rollback, 주문 내용 불변 |

리소스 실습은 기존 DB의 데이터를 고갈시키거나 메모리 한도를 낮추지 않습니다. 대신 실제 Redis 프로세스를 새로 띄워서 자원 오류를 관찰합니다. 이 실습을 통과했다고 원본 MVP의 OOM 이후 자동 장애 전환을 검증한 것은 아닙니다.

## 1. 적용과 첫 실행

전체 ZIP을 별도 디렉터리에 풀고 기존 `.env`, `.state`, named volume을 보존하세요. `all.sh`만 교체하지 않습니다. 기존 v2 설치는 엔진 pin을 보존한 채 `mvp up`으로 API/worker 이미지를 다시 빌드합니다. 일반 `up`은 네트워크 helper를 빌드하지 않습니다.

```bash
# 프로젝트 루트, 기존 환경은 init이 기존 설정을 보존합니다.
bash ./all.sh mvp init
bash ./all.sh mvp doctor
bash ./all.sh mvp up
bash ./all.sh mvp smoke

# 실행/삭제 없이 목록·계획만 확인
bash ./all.sh mvp simulate list
bash ./all.sh mvp drills list
bash ./all.sh mvp simulate plan db-network-loss --seed 42
```

로컬 Docker Engine의 Linux 컨테이너와 Compose v2만 자동 실습 대상으로 합니다. 원격 Docker와 Podman은 자동 장애 조작 대상이 아닙니다. 기존 다른 DB 실습의 지원 범위를 확대한 것이 아닙니다. `.state`의 pin을 삭제하거나 context를 강제로 바꿔 보호 장치를 우회하지 마세요.

폐기용 실습은 Docker가 메모리·swap 한도를 지원하고 총 메모리가 6GiB 이상이라고 보고할 때만 허용합니다. **총량 검사는 현재 여유 메모리 검사나 안전성 인증이 아닙니다.** 다른 HA 실습을 동시에 실행하지 말고 여유를 확인하세요. MariaDB 복원 실습은 임시 DB 최대 768MiB와 client 256MiB 한도를 추가합니다. helper/kernel/daemon의 비용까지 모두 포함하는 수치는 아닙니다.

## 2. 응답 정지와 다른 네트워크 지연·손실

```bash
# 네트워크 실습용 작은 helper 이미지에 iproute2를 설치합니다.
# 이미지/패키지를 내려받는 작업이며 호스트 패키지/커널 모듈은 변경하지 않습니다.
bash ./all.sh mvp drills prepare --yes

# 동일 요청 계획의 정상 기준
bash ./all.sh mvp simulate run baseline --seed 42 --workload write-heavy --yes

# 각각 실행하고 복구까지 확인한 뒤 다음으로 넘어갑니다.
bash ./all.sh mvp simulate run db-network-delay --seed 42 --workload write-heavy --yes
bash ./all.sh mvp simulate run db-network-loss --seed 42 --workload write-heavy --yes
```

`db-network-delay`는 송신 패킷 지연 180ms와 jitter 20ms, `db-network-loss`는 10% 확률 손실을 사용합니다. 확률이므로 짧은 시험에서 손실이 정확히 10%가 될 필요는 없습니다. `--seed`는 기존 요청 계획의 seed이며 **커널 패킷 손실 난수까지 고정하지 않습니다.** TCP 재전송 때문에 실제 packet drop이 있어도 애플리케이션 HTTP 오류가 없을 수 있습니다. drop=0이면 손실 실습 효과를 확인한 것으로 처리하지 않습니다.

helper는 해당 MariaDB 컨테이너의 network namespace에만 들어가며 `NET_ADMIN`만 추가합니다. `--privileged`, host network, host PID, host 경로·Docker socket 마운트, iptables 변경, modprobe는 사용하지 않습니다. 송신 방향 전체이므로 SQL 응답/ACK와 해당 인터페이스의 다른 송신 트래픽도 포함합니다. 특정 SQL 쿼리만 늦추거나 양방향·다중 호스트 파티션을 구현한 것은 아닙니다.

현재 지원은 eth0 하나의 기본 noqueue 구성입니다. 기존 qdisc, host/shared 네트워크 모드, 다중 네트워크는 덮어쓰지 않고 거부합니다. 커널이 netem을 지원하지 않거나 권한이 부족하면 `netem_kernel_unavailable` / `capability_denied` 등으로 실패합니다. 호스트 커널 설정을 자동 변경하지 않습니다.

네트워크 helper 자체가 최대 30초의 요청 유지시간 뒤 자기 qdisc를 지우도록 되어 있습니다. CLI만 강제 종료되어도 helper가 살아 있으면 해제 절차가 계속됩니다. **helper까지 SIGKILL되거나 호스트가 종료되면 이 보장은 없습니다.** 그 경우 아래 수동 복구가 예약된 handle `7a11:`만 확인·제거합니다.

```bash
bash ./all.sh mvp simulate recover --yes
```

기존 stop/pause 실습까지 외부 watchdog으로 바꾼 것은 아닙니다. 기존 시나리오의 강제 종료 한계는 v2 가이드대로 남습니다.

## 3. 논리 백업 → 다른 DB로 복원 → 모든 행 대조

```bash
bash ./all.sh mvp drills run backup-restore --yes
```

원본에서 한 연결의 `REPEATABLE READ` + `WITH CONSISTENT SNAPSHOT, READ ONLY`로 `orders`, `outbox`를 읽습니다. 주문의 공개 필드뿐 아니라 `idempotency_key`, outbox의 seq/event_id/payload/created_at/sent_at도 포함합니다. TIMESTAMP는 UTC로 정규화합니다. 두 테이블이 InnoDB이고 그 외 테이블이 없는지를 확인하며, 새 테이블이 있는데 조용히 누락된 백업을 만들지 않습니다.

한 테이블 최대 5,000행, outbox payload 합계 최대 4MiB, 직렬화된 snapshot 최대 16MiB로 제한합니다. 실제 스키마가 확대되면 이 도구의 schema v1을 함께 변경해야 합니다. 출력은 checksum을 가진 JSON입니다. **물리 백업, 전체 서버 SQL dump, binlog/PITR, 사용자·권한·루틴·trigger·서버 설정·AUTO_INCREMENT의 삭제로 생긴 high-water mark까지 보존하는 백업이 아닙니다.** 실행 중 DDL 변경은 하지 마세요.

복원은 원본과 같은 **실제 image ID**의 별도 MariaDB에서 합니다. datadir는 512MiB tmpfs이며 기존 named volume을 마운트하지 않습니다. 네트워크는 `none`, client는 이 새 컨테이너의 loopback만 공유합니다. 복원 helper는 disposable marker와 `127.0.0.1`, 빈 `mvp` schema를 요구합니다. 빈 schema가 아니면 DROP/덮어쓰기 대신 거부합니다.

전체 행 해시가 맞은 다음, 새 주문 생성 → 동일 키 재요청 → paid 변경 → 조회 canary를 임시 DB에서만 실행합니다. 해시가 맞아도 canary가 실패하면 통과가 아닙니다. 복원된 outbox는 worker에 연결하거나 원본 Kafka로 재전송하지 않습니다. Redis/ES/Kafka는 이 백업에 포함되지 않습니다.

정상 완료 후 임시 DB/client와 그 tmpfs만 삭제합니다. `reports/drills/drill-.../snapshot.json`과 결과는 남습니다. snapshot에는 합성 업무 데이터와 멱등성 키가 포함되며 파일 권한은 600입니다. 실 데이터를 넣었다면 민감한 백업으로 다뤄야 하며 암호화 기능은 없습니다.

### 이전 스냅샷 복원 비교

다음 명령은 과거 스냅샷을 다시 검증할 뿐, 현재 원본 DB를 과거로 돌리지 않습니다.

```bash
bash ./all.sh mvp drills run backup-restore \
  --snapshot ./mvp-lab/reports/drills/drill-실제실행ID/snapshot.json --yes
```

`drill-실제실행ID`를 실제 결과 경로로 바꿉니다. 원본 주문을 추가·변경한 뒤 과거 snapshot을 복원하면 과거 상태가 임시 DB에 재현되는지 checksum과 행 수로 비교할 수 있습니다. **영구 저장 매체의 재시작 내구성 시험은 tmpfs 복원 실습의 범위가 아닙니다.**

## 4. DB 버전 교체를 먼저 별도 복원으로 시험

```bash
# CANDIDATE에는 직접 선택하고 미리 pull한 명시적 MariaDB 버전 태그를 지정합니다.
# 예: shell에서 CANDIDATE 변수에 해당 이미지 이름을 넣은 후 실행합니다.
bash ./all.sh mvp drills run upgrade-restore --candidate-image "$CANDIDATE" --yes
```

허용 범위는 공식 MariaDB 저장소의 숫자 버전 태그입니다. `latest`, 임의 저장소, 현재와 같은 image ID는 거부합니다. 자동으로 최신 버전을 찾거나 pull하지 않습니다. source/candidate의 실제 image ID와 `SELECT VERSION()` 결과를 남깁니다. 기본 MariaDB tag를 수정하거나 원본 datadir에 새 엔진을 붙이지 않습니다.

이것은 **애플리케이션 두 테이블의 논리 import·기본 업무 동작 호환성 시험**입니다. In-place `mariadb-upgrade`, 기존 디스크 포맷 전환, 쿼리 전체 호환성, replication protocol, 다운그레이드·서비스 무중단 교체를 인증하지 않습니다. 실패한 clone을 폐기하고 기존 원본을 유지하는 방식이지, 업그레이드된 원본을 되돌리는 rollback은 아닙니다.

## 5. 디스크가 찬 경우와 메모리로 종료된 경우

```bash
bash ./all.sh mvp drills run redis-disk-full --yes
bash ./all.sh mvp drills run redis-oom --yes
```

두 실습은 기존 Redis의 같은 image ID로 별도 Redis를 시작합니다. 네트워크 공개/원본 연결/원본 데이터 복사가 없고, 한도만 별도로 설정합니다. 지속적으로 연결된 웹 화면에는 장애가 보이지 않는 것이 정상입니다. **원본 Redis를 멈추는 연동 장애 실습은 기존 `redis-outage`로 따로 합니다.**

`redis-disk-full`은 16MiB tmpfs에 appendonly+appendfsync always로 합성 값을 쓰며 최대 전송 데이터는 32MiB입니다. write 오류만으로 통과하지 않고 `aof_last_write_status=err`와 서버의 `No space left on device`를 함께 확인합니다. 장애 전에 저장한 기준 키를 읽을 수 있는지도 보고합니다. tmpfs는 메모리 기반 용량 제한이므로 물리 디스크 고장·IOPS 저하·fsync 내구성을 모델링하지는 않습니다. 호스트 디스크를 채우지 않지만 제한된 메모리 부하는 존재합니다.

`redis-oom`은 원본이 아닌 새 Redis에서 maxmemory 정책을 끄고 cgroup 64MiB, memory-swap도 64MiB로 설정합니다. 최대 128MiB의 합성 값을 시도하고 client는 25초 경계 이후 새 SET을 보내지 않습니다. Redis `OOM command not allowed` 응답이나 임의 연결 끊김, 단독 exit 137만으로 통과하지 않습니다. Docker state의 OOMKilled/ExitCode/Running을 함께 봅니다.

판정은 resource symptom이 명확하면 passed, 조건에 맞지 않으면 inconclusive 또는 failed입니다. 실제 서버 상태를 확인하기 전에 모의 결과를 채워 넣는 실행 모드는 없습니다. 자원 실습 정리는 임시 DB 폐기이며, full/OOM이 난 기존 서비스의 데이터 복구 자동화가 아닙니다.

## 6. 잠금 대기와 실제 deadlock victim을 구분

```bash
# 기존 v2: 한 행을 잡고 다른 요청이 기다리게 함
bash ./all.sh mvp simulate run row-lock --yes

# v3: 두 연결이 서로 다른 첫 행을 잡고 서로의 두 번째 행을 기다리게 함
bash ./all.sh mvp drills run deadlock --yes
```

새 실습은 주문 2개를 합성 데이터로 생성합니다. 각 연결은 두 주문 모두 이번 run의 소유인지 검사한 뒤 역순 `SELECT ... FOR UPDATE`를 요청합니다. barrier와 세션 lock wait 제한을 두고 글로벌 설정을 바꾸지 않습니다. 한 연결에서 오류 1213, 다른 연결에서 두 행 잠금 성공을 관측해야 합니다. 오류 1205만 보이면 deadlock을 확인했다고 표시하지 않습니다. 두 연결 모두 rollback/close하며 helper는 업무 UPDATE를 하지 않습니다. 이후 멱등성 키로 읽기만 하여 생성 당시 내용과 비교합니다. 합성 주문 2개는 원본에 남습니다.

이 기능은 엔진이 반환한 1213을 관찰할 코드입니다. 이 배포 제작 환경에서는 fake connection의 1213만으로 호스트 분기를 검증했고 실제 InnoDB가 1213을 반환하는 시험은 하지 않았습니다.

## 7. 중단·복구·결과

네트워크는 기존 `reports/simulations/`, 나머지 다섯 실습은 `reports/drills/drill-.../`에 결과를 남깁니다. `summary.json`의 status/observation/failed_stage/partial_observation/cleanup을 확인합니다. 백업 실습은 snapshot.json, 모든 정상 진입 실습은 source-containers.json과 source-manifest.json도 남습니다. 선행 설정이 없어 진입 자체가 실패하면 명령의 stderr·종료 코드만 있을 수 있습니다.

```bash
# 네트워크 및 기존 simulate 실습
bash ./all.sh mvp simulate recover --yes

# 별도 임시 DB/client 실습
bash ./all.sh mvp drills recover --yes
```

복구 전 엔진 pin, 프로젝트·run label, container ID, 이미지 ID, 네트워크·마운트·메모리 한도를 재검사합니다. 다른 container/원본 볼륨이 연결된 자원은 자동 삭제하지 않습니다. 임시 자원이 남으면 `.state/drill-active.json`을 유지하며 일반 up/down/experiment를 막고 복구를 요구합니다. 다른 디렉터리의 컨트롤러나 직접 docker 명령까지 잠그지는 못합니다.

snapshot 복원과 리소스 주입 helper가 실행 중일 때 CLI를 SIGKILL하면 helper/clone이 남을 수 있습니다. 호스트에서 계속 동작하는 별도 운영 감시 서비스를 설치한 것은 아닙니다. resource client에는 요청량·시간 상한이 있으나 컨테이너 제거는 recover로 확인해야 합니다.

## 8. 여전히 별도 작업인 것

다중 호스트·복제 HA, 실제 디스크 장치 I/O 장애, in-place 메이저 업그레이드/다운그레이드, 물리 백업·PITR, poison-event/DLQ 재처리, 재고·결제 정합성 모델은 구현하지 않았습니다. 기존 ES legacy 이미지 현대화도 별도 호환성 작업입니다. 기존 Helm 검증 부족을 이 MVP 변경으로 해결했다고 주장하지 않습니다.

전체 검사는 프로젝트 루트에서 `bash ./scripts/test-offline.sh`로 실행합니다. DB가 준비된 환경에서의 최소 인수 순서는 smoke → baseline → 네트워크 두 실습 → backup-restore → resource 두 실습 → deadlock입니다. 후보 버전 trial은 의도한 이미지를 선택한 경우에만 별도 실행합니다. `scripts/test-runtime-mvp.sh`가 같은 순서를 제공하며 장애·합성 쓰기 동의를 요구합니다.

## 공식 계약 참고 (2026-09-19 확인)

- Docker run network/container·capability·resource 옵션: https://docs.docker.com/reference/cli/docker/container/run/
- Docker memory / memory-swap 한도: https://docs.docker.com/engine/containers/resource_constraints/
- Docker tmpfs 특성과 제한: https://docs.docker.com/engine/storage/tmpfs/
- MariaDB 일관된 스냅샷과 READ ONLY: https://mariadb.com/docs/server/reference/sql-statements/transactions/start-transaction
- iproute2 netem 공식 구현, delay/jitter/loss 옵션: https://raw.githubusercontent.com/iproute2/iproute2/main/tc/q_netem.c

문서 확인은 고정 이미지 실행 시험이 아닙니다. helper 기본 이미지와 apt 패키지는 완전한 digest/전이 의존성 잠금이 아니며 실행 때 실제 image ID를 기록합니다.
