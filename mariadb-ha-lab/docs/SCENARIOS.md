# 슬로우쿼리·단편화·노드 장애 원클릭 실습

기준: 2026-09-17 / 기존 MariaDB 11.8 Galera 3노드 랩의 확장.
이 문서의 출력 설명은 **관찰할 항목**이며 특정 지연·회수 용량·장애 전환 시간을 보장하는 실측값이 아닙니다.

## 1. 시작

처음 설치한다면 프로젝트 루트에서:

```bash
chmod +x lab.sh
./lab.sh init
./lab.sh doctor
./lab.sh up
./lab.sh scenario list

# 작은 크기로 핵심 3종 연속 실습: 슬로우쿼리 → 단편화 → 1번 노드 강제 종료/복귀
./lab.sh scenario demo --rows 5000 --hold 20
```

`commerce_lab`의 큰 데이터를 먼저 적재할 필요는 없습니다. 각 시나리오가 **incident_lab**에 필요한 합성 데이터를 직접 만듭니다. 설정된 모든 노드가 같은 UUID의 Primary/Synced, 현재 membership 크기 상태여야 시작합니다.

이미 이전 `mariadb-ha-lab`을 실행 중이라면 **프로젝트가 있는 상위 폴더에서** 업데이트 ZIP을 덮어씁니다.

```bash
# 현재 위치 예: ~/labs/  (그 아래 mariadb-ha-lab/이 존재)
unzip -o mariadb-ha-lab-scenarios-update.zip
cd mariadb-ha-lab
./lab.sh scenario list
./lab.sh scenario slow
```

업데이트 ZIP에는 `.env`, `.state`, 기존 데이터/볼륨, compose 설정이 들어 있지 않습니다. 기존 `.env`와 프로젝트 이름을 유지하세요. 새 기능은 호스트의 코드를 `podman/docker exec`로 전달하므로 **이번 업데이트만을 위해 이미지 재빌드/DB 재시작을 할 필요가 없도록 구현**했습니다. 실행 중인 이전 실습 스크립트는 끝낸 뒤 덮어쓰세요. 코드에 직접 넣은 개인 수정 사항은 `scripts/lab.py` 교체 전에 별도로 보관하세요.

전체 ZIP 이름은 `mariadb-ha-lab-scenarios.zip`이며 그 안의 최상위 폴더도 `mariadb-ha-lab/`입니다. 별도 프로젝트를 만들 때는 이전 것과 **LAB_PROJECT와 공개 포트가 겹치지 않도록** 설정해야 합니다.

## 2. 명령 표

| 명령 | 재현하는 현상 | 기본 마무리 |
|---|---|---|
| `./lab.sh scenario slow` | 인위적 지연 로그 + 인덱스 없는 조회 | 인덱스 추가 후 전후 비교 |
| `./lab.sh scenario fragmentation` | 대량 삽입 후 80% 삭제, 파일 내부 빈 공간 | `ALTER TABLE ... FORCE` 후 공간 비교 |
| `./lab.sh scenario node-failure` | galera1 컨테이너에 SIGKILL | 살아 있는 클러스터로 재가입 |
| `./lab.sh scenario node-hang` | galera1 컨테이너 일시 정지 | unpause 후 3노드 상태 검사 |
| `./lab.sh scenario lock` | 같은 노드 두 세션 사이의 행 락 대기 | 두 세션 ROLLBACK |
| `./lab.sh scenario quorum --confirm-quorum` | galera2·galera3 일시 정지 | 두 노드 unpause, 과반수 회복 검사 |
| `./lab.sh scenario demo` | slow → fragmentation → node-failure | 세 단계별 결과 저장 |

`demo`는 **quorum 실습을 포함하지 않습니다**. 데이터 시나리오는 재실행 시 해당 **incident_lab 전용 테이블만 재생성**합니다. 다른 DB의 인덱스·데이터·전역 자원 제한을 일부러 망가뜨리지 않습니다. 다만 동일한 Galera 클러스터를 공유하므로 부하/DDL/노드 장애의 성능·가용성 영향까지 격리되는 것은 아닙니다.

## 3. 슬로우쿼리

```bash
./lab.sh scenario slow

# 더 많은 행으로 비교
./lab.sh scenario slow --rows 200000

# 인덱스 없는 상태까지만 만들어 두고 직접 진단
./lab.sh scenario slow --leave-broken
```

### 스크립트가 하는 일

`incident_lab.slow_orders`를 만들고 기본 50,000행을 500행씩 적재합니다. 조회 조건인 `customer_id`에는 처음에 인덱스가 없습니다.

슬로우로그 테스트는 `SLEEP(0.3)`으로 지연을 의도적으로 만들고, 테스트 세션의 기준을 0.05초로 설정합니다. 이것은 **로그 파이프라인 확인용**입니다. `SLEEP` 자체를 인덱스로 빠르게 만드는 실습이 아닙니다.

별도로 같은 `SUM(amount) WHERE customer_id=42` 쿼리를 인덱스 전후 각각 세 번 수행합니다. `EXPLAIN`, 클라이언트 관측 실행 시간, `Handler_read_*` 증분을 저장합니다. 기본 실행은 `idx_customer(customer_id)`를 추가합니다. 동일한 집계 결과가 유지되는지도 검사합니다.

로그 출력은 해당 노드에서 기존 FILE 설정을 보존하면서 TABLE을 임시로 추가합니다. 이미 TABLE이면 그대로 사용합니다. **GLOBAL log_output/slow_query_log 원래 값**을 복구 기록에 보관하고 종료 시 복구합니다. `long_query_time`, `min_examined_row_limit` 등은 테스트 세션에서만 바꿉니다. 기존 애플리케이션 연결의 슬로우 기준을 일괄 변경하지 않습니다. 원래 `log_output=NONE`이었다면 실습 중 TABLE을 사용하고 종료 시 NONE으로 되돌립니다.

슬로우로그는 노드 로컬입니다. 다른 노드의 `mysql.slow_log`에 같은 기록이 있다고 가정하지 마세요. 전체 시스템 로그를 지우지 않고 이번 실행 태그에 해당하는 기록만 조회/내보냅니다. 로그에 SQL 내용이 들어가므로 실데이터 대신 제공된 합성 데이터만 사용하세요.

### 직접 확인

```bash
./lab.sh sql galera1 "EXPLAIN SELECT SUM(amount) FROM incident_lab.slow_orders WHERE customer_id=42;"

./lab.sh sql galera1 "SELECT start_time,query_time,rows_examined,sql_text FROM mysql.slow_log WHERE sql_text LIKE '%lab_slow_%' ORDER BY start_time DESC LIMIT 10;"

# 기존 FILE 로그 확인 (기본 LAB_PROJECT=mariadb-ha일 때)
podman exec mariadb-ha-galera1 tail -n 80 /var/lib/mysql/slow.log

# --leave-broken으로 남겼던 인덱스 개선
./lab.sh scenario fix slow
```

관찰할 것은 전체 스캔과 인덱스 접근의 차이입니다. 작은 테이블/빠른 호스트에서는 인덱스 없는 쿼리도 0.05초 미만일 수 있습니다. 그것을 가짜 지연으로 부풀리지 않습니다. 모든 전체 스캔이 슬로우로그에 들어가야 성공인 것은 아니며, 로그 기록 자체는 별도의 SLEEP 태그로 검증합니다. `Handler_read_*`는 측정용 SQL의 작은 오버헤드도 포함할 수 있습니다. 캐시를 강제로 비우지 않으므로 결과는 cold-cache 벤치마크가 아닙니다.

## 4. InnoDB 단편화·공간 회수

```bash
./lab.sh scenario fragmentation

# 약 97.7 MiB의 원시 payload를 노드당 생성 (파일 크기와는 다름)
./lab.sh scenario fragmentation --rows 100000 --payload-bytes 1024

# 삭제 후의 상태를 유지해서 직접 관찰
./lab.sh scenario fragmentation --leave-broken
./lab.sh scenario inspect
./lab.sh scenario fix fragmentation
```

`incident_lab.fragmented_events`에 기본 50,000행 × 1,024바이트 payload를 넣습니다. 원시 payload는 **노드당 약 48.8 MiB**이며, 인덱스·페이지·binlog·gcache 등은 별도입니다. Galera는 세 노드에 데이터를 저장하므로 데이터 용량을 노드 수로 나눠 생각하면 안 됩니다.

키 범위 전체에 걸쳐 `MOD(id,5)<>0`인 행을 500개 ID 범위씩 나눠 삭제합니다. 즉 5개 중 4개, 약 80%의 행을 삭제합니다. 이어서 기본 실행은 `ALTER TABLE incident_lab.fragmented_events FORCE`로 테이블을 재구축합니다. **80% 삭제가 80% 단편화율을 뜻하지 않습니다.**

다음 항목을 삭제 전 → 삭제 후 → 재구축 후로 비교합니다.

| 항목 | 해석 |
|---|---|
| `exact_rows` | 실제 `COUNT(*)` |
| `estimated_rows` | InnoDB 통계의 추정 행 수 |
| `data_bytes`, `index_bytes` | INFORMATION_SCHEMA의 데이터/인덱스 크기 통계 |
| `free_extent_bytes` | `DATA_FREE`: 할당되었으나 사용되지 않는 공간 지표 |
| `file.logical_bytes` | `.ibd` 파일의 `stat.st_size` |
| `file.allocated_bytes` | 파일에 할당된 블록 수 × 512; 지원되는 파일시스템 기준 |
| `payload_bytes` | 실제 남아 있는 payload 바이트 합계 |

삭제한 공간이 즉시 운영체제에 반환되지 않는 현상과 테이블 재구축 후의 변화를 관찰하는 실습입니다. purge는 비동기이므로 2초 후의 `DATA_FREE`도 최종 상태라고 보장할 수 없습니다. 페이지 내부의 여유 공간과 완전히 비어 있는 extent는 다른 개념입니다. `DATA_FREE=0`이라고 페이지가 완전히 차 있다는 뜻은 아닙니다. 이 실습은 **파일시스템 조각/섹터의 물리적 단편화**를 측정하는 도구도 아닙니다.

`TABLE_ROWS`는 추정값이므로 검증에는 `COUNT(*)`를 씁니다. 재구축 전후 행 수와 payload 합계가 같아야 합니다. 파일이 얼마나 줄어드는지, 조회가 더 빨라지는지는 환경에 따라 다르며 고정 수치를 성공 조건으로 두지 않았습니다. 파일이 작아졌다고 호스트의 snapshot/스토리지 계층까지 같은 양이 즉시 반환되는 것도 아닙니다.

기본 11.8 환경에서 제거된 구형 `innodb_defragment` 변수를 쓰지 않습니다. file-per-table 설정이 꺼져 있으면 스크립트는 임의로 전역 설정을 바꾸지 않고 중단합니다. TOI DDL이 세 노드에 적용되며 클러스터 쓰기가 잠시 대기할 수 있으므로 다른 부하 실습과 동시에 돌리지 마세요. 한 노드만 독립적으로 무중단 정리하는 RSU 실습이 아닙니다.

각 노드의 파일 크기는 다를 수 있습니다. 전체 파이프라인 전후 비교는 실행 노드인 galera1 기준이고, 끝나면 galera1·2·3의 **최종 노드별 공간 통계**도 따로 수집합니다.

입력 상한은 500,000행, 행당 4,096바이트, 원시 payload 합계 512 MiB/노드입니다. 현재 실행 노드의 datadir 파일시스템 여유 공간도 확인하지만 호스트 전체 저장 공간/스냅샷/볼륨별 quota를 완전히 검사하는 것은 아닙니다.

## 5. 노드 장애: 종료와 멈춤 구분

```bash
# 1번 노드를 강제 종료, 목표 20초 관찰 후 재가입
./lab.sh scenario node-failure --node galera1 --hold 20

# 3번 노드 장애: writer가 아닌 노드 장애도 비교
./lab.sh scenario node-failure --node galera3 --hold 30

# 프로세스를 종료하지 않고 컨테이너 전체를 일시 정지
./lab.sh scenario node-hang --node galera1 --hold 30
```

`node-failure`는 해당 랩 컨테이너에 SIGKILL을 보냅니다. `node-hang`은 컨테이너의 프로세스를 pause합니다. 후자는 응답이 사라지는 현상을 관찰하기 위한 것으로 **메모리 부족/OOM이나 특정 NIC 패킷 손실을 정확히 재현하는 기능은 아닙니다.** rootless 환경에서 cgroup/freezer 기능 때문에 pause가 지원되지 않으면 실패로 기록하며 강제 종료로 몰래 대체하지 않습니다.

다른 정상 노드에서 HAProxy writer로 **매 시도마다 새 TCP 연결**을 열어 `incident_lab.fault_probe`에 고유 요청 ID를 넣습니다. 장애 전·중·후의 성공/미확인 응답, 오류 코드, 처리 노드와 상태 표본을 저장합니다. 기존 지속 연결이나 미완료 트랜잭션이 다른 노드로 자동 이동하는 것을 시험하는 코드는 아닙니다.

1번 노드가 선호 writer라는 것은 프록시의 정책이며 Galera 리더 선출이 아닙니다. 최초 writer와 장애 대상이 같은지, 실제 처리 노드 변경이 관찰됐는지도 별도 기록합니다. standby 쪽 장애를 골랐거나 시작 시 프록시 상태가 전환 중이었다면 writer가 바뀌지 않을 수 있습니다.

통신 오류가 났다고 그 쓰기가 반드시 취소되었다고 처리하지 않습니다. 복구 뒤 실제 저장된 요청 ID를 조회하여 **응답을 받은 쓰기가 세 노드에 있는지**, 세 노드의 요청 ID 집합이 같은지 검증합니다. 응답을 못 받은 요청도 `present_after_recovery`로 확인합니다. 측정용 INSERT를 자동 재시도해서 중복 결과를 숨기지 않습니다.

`--hold`는 장애 유지 목표 시간입니다. 각 DB/컨테이너 명령과 timeout 때문에 실제 경과시간은 늘어날 수 있습니다. `first_ack_after_fault_seconds`는 표본 기반으로 처음 관찰한 성공 응답 시점이며, 정밀 RTO나 지속 연결의 무중단을 증명하지 않습니다.

### 다른 터미널에서 관찰

```bash
watch -n 2 './lab.sh status'
./lab.sh logs galera1 --tail 100
./lab.sh scenario inspect
```

웹 대시보드 `http://127.0.0.1:18081`과 HAProxy 통계 `http://127.0.0.1:18084/stats`도 그대로 사용할 수 있습니다. 이번 업데이트는 대시보드에 위험한 장애 주입 버튼이나 런타임 소켓을 추가하지 않습니다.

## 6. 과반수 상실: 명시적으로 선택하는 실습

```bash
./lab.sh scenario quorum --hold 45 --confirm-quorum
```

galera2와 galera3을 pause하고 galera1에서 `wsrep_cluster_status=non-Primary`, readiness 해제, 신규 쓰기 실패를 관찰합니다. 세 노드가 각각 같은 투표 가중치인 기본 구성에서는 두 노드와 연락이 끊긴 한 노드가 쓰기를 계속해선 안 됩니다. 종료 시 **두 노드를 먼저 모두 unpause**한 다음 세 노드 회복을 기다립니다.

이 스크립트는 `pc.bootstrap=true`, quorum 무시, safe_to_bootstrap 임의 변경을 하지 않습니다. 지정된 관찰 기간에 실제 non-Primary를 확인하지 못하면 그 실습을 성공으로 표시하지 않습니다. 일부 파일/노드가 외부 작업으로 손상된 경우에는 자동 복구를 강제하지 않습니다.

과반수 실습 중에는 다른 터미널에서 임의로 bootstrap/rebuild/reset을 하지 마세요.

## 7. 행 락 대기

```bash
./lab.sh scenario lock --hold 20
```

galera1의 세션 A가 `lock_accounts`의 한 행을 갱신하고 커밋하지 않습니다. 같은 노드의 세션 B가 동일 행 갱신을 기다립니다. 관찰 세션은 `INNODB_TRX`, `INNODB_LOCK_WAITS`, `PROCESSLIST`를 수집합니다. 마지막에는 A와 B를 모두 ROLLBACK하여 잔액 1000을 유지합니다.

```bash
# 유지 시간 동안 다른 터미널에서
./lab.sh sql galera1 'SELECT * FROM information_schema.INNODB_LOCK_WAITS\G'
./lab.sh sql galera1 'SELECT trx_id,trx_state,trx_mysql_thread_id,trx_query FROM information_schema.INNODB_TRX;'
```

서로 다른 Galera 노드에서 같은 행을 갱신하는 certification conflict/BF abort와 **같은 노드의 로컬 InnoDB row lock**을 혼동하지 않는 것이 목표입니다. 노드 간 충돌은 기존 `./lab.sh conflict`로 별도 실습할 수 있습니다.

## 8. 중단·복구·정리

```bash
# 가장 최근 실행 결과 보기: DB가 꺼져 있어도 사용 가능
./lab.sh scenario report

# Ctrl+C, SIGTERM, 실행 오류 뒤 복구 기록이 남았다면
./lab.sh scenario repair
./lab.sh status

# leave-broken으로 남긴 논리적인 문제는 별도 개선
./lab.sh scenario fix slow
./lab.sh scenario fix fragmentation

# 합성 incident_lab만 삭제
./lab.sh scenario cleanup --confirm-cleanup
```

기본 동작은 종료/오류/첫 번째 Ctrl+C에서 **노드 재시작 또는 unpause, 슬로우로그 전역 설정 복구를 시도**합니다. `.state/scenario-pending.json`에 바꾸기 전 설정과 대상 노드를 기록합니다. 실제 컨테이너 내부에서 돌던 작업은 고유 토큰·PID를 대조해 종료를 요청하며, 해당 프로세스인지 확인할 수 없으면 다른 프로세스를 죽이지 않습니다.

SIGKILL, 호스트 종료, 복구 중 다시 Ctrl+C, 런타임 고장까지 자동 복구를 보장하지는 않습니다. 복구 실패 시 `recovery-needed`와 기록을 남겨 `scenario repair`로 이어가도록 합니다. `.state`를 지우거나 프로젝트 이름을 바꿔서 경고를 우회하지 마세요.

`repair`는 중단됐던 장애 주입/설정 변경의 복구입니다. 일부 적재된 합성 데이터나 이미 실행된 DDL을 시점 복원하는 기능은 아닙니다. 데이터 시나리오는 상태 확인 후 다시 실행하거나 전용 DB를 정리합니다. `cleanup`도 binlog·backup 파일을 지우는 명령은 아닙니다. 이 랩의 기존 `backup`은 `commerce_lab`/`lab_ops` 대상이며 `incident_lab`은 포함하지 않습니다.

### 주기적 seed simulation

`./lab.sh simulate`은 기존 commerce dataset을 교체하지 않고 API 로그와 주문 조회·상태
변경을 일정 rate로 반복합니다. `--mode api|orders|mixed`, `--seconds`, `--rate`,
`--seed`로 재현 가능한 트래픽을 만들 수 있습니다. `mixed`는 API 로그 65%, 주문 작업
35% 비율입니다. 주문 상태 변경은 감사 trigger를 통과하며, Galera 장애 중 연결 오류와
재연결 결과는 `operations.errors` 및 작업별 카운트로 확인합니다.

같은 프로젝트 폴더에서 동시에 실행되는 시나리오·노드 조작·초기화·적재는 잠금으로 충돌을 방지합니다. 하지만 직접 실행하는 `podman kill`, 외부 DB 클라이언트의 DDL, 서로 다른 폴더에서 같은 프로젝트를 제어하는 작업까지 막을 수는 없습니다.

## 9. 결과 파일

실행할 때마다 다음 경로를 만듭니다.

```text
reports/scenarios/<시각>-<시나리오>-<식별자>/
  summary.json       # 상태, 실제 측정값, 검증 결과
  summary.md         # 사람이 읽는 같은 결과
  events.jsonl       # 단계별 이벤트와 장애 접속 시도
  slow.json          # 해당 시나리오에서 생성
  slow-log.tsv       # 이번 실행의 슬로우로그만
  fragmentation.json # 해당 시나리오에서 생성
  fragmentation.csv  # 삭제/재구축 전후 지표
  node-failure.json  # 해당 시나리오에서 생성
```

실패 결과도 남깁니다. `passed`는 **그 실행에서 수행한 조건이 통과했다는 의미**이며 운영용 HA 인증, 고정 성능 보장, 무손실 백업 보장을 뜻하지 않습니다. 새 코드의 제작 환경 검증 범위는 `SCENARIOS-VALIDATION.md`를 보세요.

## 10. 근거 문서

- MariaDB: [Slow Query Log Overview](https://mariadb.com/docs/server/server-management/server-monitoring-logs/slow-query-log/slow-query-log-overview) — FILE/TABLE 출력, session 기준, SLEEP으로 로깅 확인.
- MariaDB: [Information Schema TABLES](https://mariadb.com/docs/server/reference/system-tables/information-schema/information-schema-tables/information-schema-tables-table) — DATA_FREE와 추정 TABLE_ROWS의 의미.
- MariaDB: [Defragmenting InnoDB Tablespaces](https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/optimizing-tables/defragmenting-innodb-tablespaces) — 삭제 공간, 재구축, 구형 defragment 기능의 제거.
- MariaDB: [ALTER TABLE](https://mariadb.com/docs/server/reference/sql-statements/data-definition/alter/alter-table) — FORCE를 통한 테이블 재구축.
- MariaDB: [Performing Schema Upgrades in Galera Cluster](https://mariadb.com/docs/galera-cluster/galera-management/general-operations/performing-schema-upgrades-in-galera-cluster) — TOI의 클러스터 영향.
- MariaDB: [Understanding Quorum, Monitoring, and Recovery](https://mariadb.com/docs/galera-cluster/high-availability/understanding-quorum-monitoring-and-recovery) — 과반수와 non-Primary 상태.
