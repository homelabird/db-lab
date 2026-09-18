# 단계별 운영 실습

실습 중 두 터미널과 대시보드를 함께 열어 둡니다. 원본 업무 DB가 아니라 새 랩에서만 실행하세요. 아래 예상 결과는 기술상 기대되는 동작이며 제작 환경에서 실제 실행했다고 주장하는 결과가 아닙니다.

## 01. 구성 증명: 컨테이너 세 개와 클러스터 하나는 다르다

```bash
./lab.sh status
./lab.sh sql galera1 < labs/01-health.sql
./lab.sh sql galera2 < labs/01-health.sql
./lab.sh sql galera3 < labs/01-health.sql
./lab.sh verify
```

세 노드에서 `wsrep_cluster_size=3`, `wsrep_cluster_status=Primary`, `wsrep_local_state_comment=Synced`, `wsrep_ready=ON`, 동일 `wsrep_cluster_state_uuid`를 확인합니다. `@@server_id`는 101/102/103으로 달라야 합니다. `verify`는 한 노드에 UUID probe를 쓰고 다른 노드에서 causal-read 장벽 후 같은 행이 보이는지도 검사합니다.

스스로 설명할 내용: 같은 데이터 복사본 3개와 서로 다른 shard 3개의 차이, wsrep 상태와 프로세스 생존의 차이, HAProxy가 복제 엔진이 아닌 이유.

## 02. InnoDB buffer pool, redo/binlog, 쿼리 계획

```bash
./lab.sh sql galera1 < labs/02-innodb-observability.sql
./lab.sh sql galera1 < labs/03-index-and-partition.sql
./lab.sh sql galera1 < labs/10-binlog.sql
```

`Innodb_buffer_pool_read_requests`는 논리적 읽기 요청, `Innodb_buffer_pool_reads`는 buffer pool에서 해결되지 않아 읽어야 했던 요청을 관찰하는 지표입니다. 누적 수치를 단순히 현재 초당 값으로 해석하지 말고 표본 간 차이와 워크로드를 함께 보세요. redo log는 crash recovery, binlog는 변경 이벤트 기록, Galera write-set은 분산 복제 계층이라는 역할을 구분합니다.

EXPLAIN의 예상 비용과 실제 실행을 동반하는 `ANALYZE FORMAT=JSON`은 다릅니다. 작은 데이터에서는 인덱스가 있어도 full scan이 합리적일 수 있습니다. 특정 실행 계획이 반드시 나온다고 가정하지 말고 선택도/행 수/조건을 설명하세요. 날짜 함수 조건은 MariaDB 버전의 최적화에 따라 sargable하게 변환될 수 있어, 항상 인덱스를 못 탄다고 단정하지 마세요.

## 03. 같은 노드의 row lock과 lock wait

터미널 A:

```bash
./lab.sh sql galera1 < labs/04-lock-a.sql
```

A가 대기하는 동안 터미널 B:

```bash
./lab.sh sql galera1 < labs/04-lock-b.sql
```

세 번째 터미널에서 `./lab.sh sql galera1 < labs/02-innodb-observability.sql`을 실행합니다. B의 UPDATE가 A의 커밋까지 기다리는지, `INNODB_TRX`, `INNODB_LOCK_WAITS`에서 무엇이 보이는지 확인합니다. A와 B를 서로 다른 노드에 연결하면 같은 로컬 lock wait 실습이 아닙니다.

## 04. 같은 노드의 deadlock

10초 안에 두 터미널에서 각각 실행합니다.

```bash
./lab.sh sql galera1 < labs/05-deadlock-a.sql
./lab.sh sql galera1 < labs/05-deadlock-b.sql
```

둘이 서로 다른 행을 선점한 뒤 반대 행을 요청합니다. 기대 결과는 한 트랜잭션의 1213 오류입니다. `SHOW ENGINE INNODB STATUS\G`의 최근 deadlock을 읽습니다. 이 실습의 비정상 종료 코드는 의도한 실패일 수 있습니다. 두 SQL 파일을 한 터미널에서 차례로 완료시키면 재현되지 않습니다.

## 05. 서로 다른 노드의 certification conflict / BF abort

```bash
./lab.sh conflict
./lab.sh sql galera1 < labs/06-replication-metrics.sql
./lab.sh sql galera2 < labs/06-replication-metrics.sql
```

galera1과 galera2에서 동일 PK 행을 동시에 미커밋 갱신한 뒤 커밋합니다. 다른 노드의 row lock을 직접 공유하는 것이 아닙니다. 시점에 따라 certification 실패 또는 원격 apply를 위한 BF abort로 실패가 나타날 수 있습니다. 따라서 `wsrep_local_cert_failures`만 보지 말고 `wsrep_local_bf_aborts`도 전후 비교하세요.

고장 난 클러스터와 정상적인 충돌 처리의 차이, 애플리케이션이 전체 트랜잭션을 재시도해야 하는 이유를 설명하세요.

## 06. writer/reader 연결 경로와 기존 연결 유지

```bash
./lab.sh routes
./lab.sh load --seconds 60 --workers 4
```

writer는 신규 연결에서 galera1을 우선합니다. reader는 신규 연결들을 준비된 노드에 분배합니다. 하나의 세션에서 SELECT를 여러 번 실행하는 것과 매번 새 접속을 만드는 것을 비교하세요. HAProxy가 TCP 연결마다 backend를 선택하므로 같은 세션은 같은 노드에 남습니다.

`readonly` 사용자의 쓰기 거부는 DB GRANT로 강제합니다. reader 포트에 `lab` 사용자로 접속하면 그 포트 자체가 쓰기를 막아 주는 것은 아닙니다. 이 구성은 SQL-aware read/write splitting이 아닙니다.

## 07. 노드 한 개 강제 장애와 신규 연결 전환

A에서 부하를 시작하고, B에서 장애를 일으킵니다.

```bash
# A
./lab.sh load --seconds 90 --workers 4

# B
./lab.sh kill galera1
./lab.sh status
./lab.sh routes
```

기존 galera1 연결은 오류가 날 수 있습니다. failure detection과 HAProxy health 판정 후, 남은 2개 노드가 Primary를 유지하면 새 writer 연결은 galera2로 갑니다. `load` 종료 보고서의 backend/재시도/오류 코드와 전체 잔액 100,000,000 유지 여부를 확인합니다.

```bash
./lab.sh start galera1
./lab.sh logs galera1 --tail 150
./lab.sh verify
```

갈레라가 쓰기 가능한 새 리더 한 개를 선출한 것이 아니라 프록시의 접속 경로가 바뀌었다는 점을 구분하세요. 아직 살아 있는 galera2의 pooled connection까지 galera1로 자동 이동하지는 않습니다.

## 08. 정상 중단과 IST

```bash
./lab.sh stop galera3
./lab.sh load --seconds 20 --workers 2
./lab.sh start galera3
./lab.sh logs galera3 --tail 200
```

노드가 빠진 동안 생긴 write-set이 donor의 gcache에 남고 state UUID/위치가 맞으면 IST가 가능해집니다. **중단이 짧았다는 사실만으로 IST를 보장하지 않습니다.** gcache에서 이미 빠졌거나 필요한 상태가 없으면 SST가 선택될 수 있습니다.

전후의 `wsrep_last_committed`, `wsrep_local_cached_downto`와 실제 IST/SST 로그를 비교합니다. crash 후에는 위치 복구나 SST가 필요할 수 있으며, 정상 종료와 같은 경로라고 단정하지 마세요.

## 09. SST와 donor/joiner

```bash
./lab.sh rebuild galera3 --confirm-rebuild
./lab.sh logs galera3 --tail 200
./lab.sh logs galera1 --tail 200
./lab.sh logs galera2 --tail 200
```

3번 노드의 전용 볼륨 내용과 초기화 marker만 지우고 새 노드로 가입시킵니다. 남은 두 노드가 준비되지 않았으면 명령이 거절됩니다. 새 노드에는 전체 상태가 없으므로 donor에서 SST가 필요합니다. donor는 구성원 상태에 따라 선택되므로 언제나 galera1이라고 가정하지 않습니다.

이 랩은 `mariabackup` SST를 선택했습니다. SST 명칭과 실행 유틸리티 `mariadb-backup` 명칭은 다릅니다. `socat`, Galera provider, SST 계정의 로컬 backup 권한도 이미지에 준비했습니다. donor에서 완전한 무잠금/영향 없음이라는 뜻은 아니며 backup 단계에서 짧은 commit blocking이 있을 수 있습니다.

## 10. Quorum loss: 순차 정상 stop으로 시험하지 않기

```bash
./lab.sh quorum-demo --confirm-pause
./lab.sh status
```

2번과 3번의 실행을 빠르게 pause하여 1번에서 두 노드가 응답하지 않는 상황을 만듭니다. membership 장애 감지가 끝나면 1번이 Non-Primary/NOT READY가 되는지 확인합니다. pause된 컨테이너는 프로세스가 삭제된 것이 아니지만 SQL도 health API도 응답하지 않습니다. 강제 bootstrap하지 않습니다.

```bash
./lab.sh resume galera2
./lab.sh resume galera3
./lab.sh status
./lab.sh verify
```

원래 구성원들이 다시 통신하며 Primary component가 회복되는지 확인합니다. `SHOW`류 상태 명령은 Non-Primary에서도 허용되는 반면 일반 데이터 쿼리는 막힐 수 있습니다.

## 11. 계획된 전체 중단과 안전한 재시작

```bash
./lab.sh down
./lab.sh up
./lab.sh verify
```

`down`은 프록시를 내리고 3→2→1 순서로 현재 살아 있는 노드를 정상 종료합니다. `up`은 실제 각 노드의 `grastate.dat`를 보고 유일한 safe node를 선택합니다. 명령행에 bootstrap 옵션을 고정하거나 모든 노드에 `gcomm://`를 상시 설정하지 않습니다. bootstrap 허가는 별도 control volume의 1회용 token으로 전달하고 실행 전에 소비합니다.

## 12. 전체 crash와 recovered seqno 비교

먼저 모든 사용자를 끊고 합성 데이터를 사용하는지 확인합니다. 다음 명령은 정상 종료와 달리 crash를 의도합니다.

```bash
./lab.sh kill galera1
./lab.sh kill galera2
./lab.sh kill galera3
./lab.sh recover
```

`grastate.dat`의 seqno가 -1이어도 데이터가 전부 사라졌다는 뜻이 아닙니다. `--wsrep-recover`로 각 노드의 UUID/최종 복구 위치를 계산합니다. **같은 UUID끼리만** seqno를 비교해야 합니다. 세 노드 모두 확인하지 못하거나 UUID가 다르면 자동으로 최신 노드를 고를 수 없습니다.

```bash
./lab.sh recover --execute --confirm-recovery
./lab.sh verify
```

실행 옵션을 줄 때도 복구 위치를 다시 계산합니다. 최고 위치 노드 하나에만 safe flag와 bootstrap token을 설정합니다. 강제 복구는 마지막 수단이며, 다른 Primary가 살아 있지 않음을 먼저 확인해야 합니다. 이 랩은 단일 project의 3개 노드만 관리하므로 운영의 다른 호스트까지 fencing했다고 간주하면 안 됩니다.

## 13. 읽기 가시성, auto increment, DDL

```bash
./lab.sh sql galera2 < labs/07-consistency.sql
./lab.sh sql galera1 < labs/08-auto-increment.sql
./lab.sh sql galera2 < labs/08-auto-increment.sql
./lab.sh sql galera3 < labs/08-auto-increment.sql
./lab.sh sql galera1 < labs/09-toi-ddl.sql
./lab.sh sql galera2 "SHOW CREATE TABLE lab_ops.ddl_demo;"
```

`wsrep_sync_wait`, auto_increment increment/offset, TOI를 관찰합니다. RSU는 해당 노드의 schema를 먼저 바꾸는 방식이며 다른 노드와의 호환성 관리가 필요합니다. 실습 파일은 의도하지 않은 schema divergence를 만들지 않도록 RSU를 자동 실행하지 않습니다.

## 14. flow control을 읽는 법

```bash
./lab.sh sql galera1 < labs/06-replication-metrics.sql
./lab.sh load --seconds 60 --workers 8 --target multi
./lab.sh sql galera1 < labs/06-replication-metrics.sql
```

빠른 노드의 전송량, 느린 노드의 receive queue, flow_control sent/recv를 비교합니다. 이 소규모 워크로드로는 flow control이 눈에 띄지 않을 수도 있습니다. **0이 나오면 고장이라고 판단하지 않습니다.** 배치 적재나 자원 제한으로 느린 applier 조건을 만든 뒤 전후 차이를 보는 것이 다음 과제입니다.

`wsrep_desync=ON`은 복제를 멈추는 설정이 아니며 flow-control 참여 동작을 바꿉니다. 실습 중 임의로 켰다면 다시 OFF로 복원하고 상태를 확인해야 합니다. dashboard의 paused ratio는 누적 통계 구간 값이고 화면의 지난 5초를 의미하지 않습니다.

## 15. 백업은 별도 노드에 복원해서 검증

```bash
./lab.sh backup
./lab.sh restore --confirm-restore
./lab.sh sql restore "SELECT @@hostname,@@wsrep_on; SELECT COUNT(*),SUM(balance) FROM lab_ops.account;"
./lab.sh sql restore "SELECT COUNT(*) FROM commerce_lab.orders;"
```

`restore`의 wsrep가 OFF인지, 합성 주문과 계정 수가 맞는지 확인합니다. 원본 Galera에 dump를 바로 재적용하지 않습니다. backup 파일은 gzip 논리 dump, JSON에는 SHA-256/DB 버전/출처를 기록합니다. SHA-256은 파일 손상 검증이지 업무 의미 무결성 검증이 아닙니다.

`--single-transaction`은 InnoDB 데이터의 일관된 snapshot을 위한 것이며 동시 DDL까지 안전하게 처리한다는 의미는 아닙니다. 백업 실습 중 DDL과 부하 발생기를 멈추세요. 기본 백업에는 자동 PITR에 필요한 binlog 좌표/보관/재생 파이프라인이 없으므로 `SHOW BINARY LOGS`만 보고 PITR 준비가 끝났다고 생각하면 안 됩니다.

## 16. 완주 과제

작성할 장애 기록은 여섯 항목이면 충분합니다: 장애 시각과 명령, 사용자에게 보인 오류, 각 노드의 Primary/Synced/Ready, 프록시가 고른 backend, IST/SST 또는 복구 판단 근거, 복구 뒤 데이터 확인 결과. `쿼리가 다시 된다`와 `모든 노드의 데이터와 권한이 검증되었다`를 구분해 기록하세요.


## 자동화된 장애·성능 시나리오 추가

[SCENARIOS.md](SCENARIOS.md)에서 `scenario slow`, `fragmentation`, `node-failure`,
`node-hang`, `lock`, `quorum`을 실행할 수 있습니다.
문제만 구성하려면 데이터 시나리오의 `--leave-broken`을 사용하고,
실제 실행 계획·로그·공간·장애 접속 결과는 `scenario report`로 확인하세요.
