# Elasticsearch 장애 주입·진단·복구 실습

대상: 이 프로젝트의 Elasticsearch **7.17.29 / es01~es05 / 시드 인덱스 3개**. 0.0.0.0 포트 바인딩, 기존 시드 생성기와 검색 문서는 유지합니다. 데이터 본문은 ZIP에 없습니다.

**장애를 실제로 일으키는 명령입니다. 운영·고객사 클러스터에 실행하지 마세요.** 데이터 노드 정지, master 변경, 샤드 재배치, 클러스터 설정 변경이 발생합니다. 한 호스트의 컨테이너 5개이므로 호스트 장애나 물리 디스크 장애에 대한 고가용성을 입증하는 환경이 아닙니다.

## 1. 처음 실행할 명령

기존 프로젝트 폴더에 새 파일을 반영해 사용하세요. 기존 `.env`와 `reports/seed-manifest.json`, 실행 중인 장애가 있다면 그 `reports/faults/active.json`을 보존합니다. 다른 폴더를 같은 클러스터에 연결해 동시에 실험하지 마세요.

```bash
chmod +x scripts/*.sh scripts/*.py scenarios/*.sh

# 실제 클러스터를 시작하고 시드를 넣지 않았다면 먼저 실행
./scripts/01-up.sh
./scripts/04-seed-data.sh

# 기존 데이터·샤드·검색의 정상 기준 상태 확인
./scripts/09-verify-seed.sh

# 새 장애 기능 목록
./scripts/13-fault-lab.sh list

# 최초 실습: es03 정상 종료 → 장애 판정 → 자동 재기동/원복
./scripts/13-fault-lab.sh run node-stop --node es03 --yes
```

실행 전에 5개 이상의 데이터 노드, 동일 cluster UUID, 안정적인 전체 green, 세 시드 인덱스의 데이터 존재를 확인합니다. `cluster_uuid=_na_`, 노드 미합류, 기존 yellow/red, 동작 중인 다른 장애 기록이면 시작하지 않습니다. `live-load.sh`나 별도의 writer/수동 변경 작업을 중지하세요. 복구 중 seed 재생성으로 오류를 숨기지 마세요.

Podman을 rootless로 기동했다면 장애 스크립트도 **같은 일반 사용자**로 실행합니다. `sudo`로 다른 Podman 저장소를 조작하지 마세요. Python 3.9+, Bash, curl, 실제 Compose provider가 필요합니다.

## 2. 자동 검증과 수동 관찰

### 한 번에 주입 → 확인 → 원복

```bash
./scripts/13-fault-lab.sh run node-crash --node es03 --yes
./scripts/13-fault-lab.sh run master-failover --yes
./scripts/13-fault-lab.sh run too-many-replicas --yes

# 장애 판정 후 60초 유지해서 Cerebro에서 관찰한 다음 자동 복구
./scripts/13-fault-lab.sh run node-stop --node es03 --hold 60 --yes

# 단계별 대기 제한을 300초로 조정
./scripts/13-fault-lab.sh run node-stop --timeout 300 --yes
```

`node-stop`은 정상 종료 요청(10초 내 종료하지 못하면 Compose가 강제 종료할 수 있음), `node-crash`는 **SIGKILL에 의한 프로세스 강제 종료**입니다. SIGKILL은 전원 차단·디스크 손상과 동일하지 않습니다. `master-failover`는 es01을 하드코딩하지 않고 현재 elected master를 읽어 정지합니다. 실험 준비 중 master가 달라지면 잘못된 노드를 테스트하지 않고 중단·원복합니다.

`--hold`는 검증 후 관찰을 위해 상태를 유지하는 옵션이지 고빈도 연속 모니터링이 아닙니다. 현재 검사는 장애 전, 조건 성립 시점, 복구 후의 표본 검사입니다. 실패 없는 연속 서비스나 정확한 RTO/RPO를 보증하지 않습니다.

### 직접 진단하고 나중에 복구

```bash
# 실제 장애를 만들고 조건을 확인한 뒤, 그 상태를 유지하고 종료
./scripts/13-fault-lab.sh apply allocation-filter --yes

# 저장된 실험 정보만 출력
./scripts/13-fault-lab.sh status

# 실제 ES의 상태·원인 읽기 (설정 변경 없음)
./scripts/13-fault-lab.sh diagnose
./scenarios/04-allocation-explain.sh

# 기대한 장애 상태가 여전히 재현되는지 검사
./scripts/13-fault-lab.sh check

# 원래 설정 복원 → 정상 판정 → 이번 실험용 인덱스만 제거
./scripts/13-fault-lab.sh recover
```

**`fault-check-PASS`는 “의도한 장애가 재현됐다”는 뜻이며 정상 복구라는 뜻이 아닙니다.** 최종 `recovery-PASS`, 보고서의 `result`, `recovery.status`를 구분하세요. 장애 확인이 실패했지만 원복은 성공하면 `result=FAIL`, `recovery.status=PASS`가 됩니다. `recover` 명령의 종료 코드 0은 복구 성공을 의미하며, 앞서 실패한 장애 시험을 소급하여 PASS로 바꾸지 않습니다.

`apply`로 남긴 장애는 명시적으로 `recover`해야 합니다. `run`은 시험 실패와 Ctrl-C/SIGTERM에서도 복구를 시도합니다. 복구 자체가 실패하면 종료 코드는 nonzero이고 `active.json`을 남깁니다. SIGKILL, 호스트 전원 차단, 디스크 고갈 시 즉시 자동 복구를 보장하지 않습니다. 재접속 후 같은 폴더에서 `recover`를 다시 실행하세요.

## 3. 시나리오별 판정

| 이름 | 주입 범위 | 장애 중 확인 | 복구 후 확인 |
|---|---|---|---|
| `node-stop` | 선택 노드 1개 정상 종료 | 실제 노드 이탈, 원래 primary의 생존 노드 승격, 시드 검색·canary 읽기/쓰기 | 원래 노드 이름 목록, 전체 green, 시드 보존 |
| `node-crash` | 선택 노드 1개 SIGKILL | 위와 동일, 종료 명령은 정상 종료와 구분 | 컨테이너 start, 데이터 볼륨 유지 |
| `master-failover` | 현재 elected master 1개 | master 이름 변경, 실제 primary 승격, 시드 검색·canary 읽기/쓰기 | 원래 노드 목록·정상 상태; 이전 master가 다시 master일 필요 없음 |
| `too-many-replicas` | 선택 시드 인덱스 replica 수 | yellow·미할당 replica, `same_shard: NO`, 검색/쓰기 유지 | replica를 실험 전 값으로 복원 |
| `allocation-disabled` | 클러스터 allocation + canary replica 추가 | yellow, `enable: NO`, 기존 primary로 읽기/쓰기 유지 | persistent/transient 원래 값 복원 |
| `allocation-filter` | 새 canary의 존재하지 않는 노드 요구 | canary red, `filter: NO`, 부분 결과를 금지한 검색의 503 오류 | filter 해제 후 canary 실제 쓰기/읽기/검색 성공 |
| `write-block` | canary의 `index.blocks.write=true` | green이어도 쓰기는 403 `cluster_block_exception`, 기존 문서 검색 가능 | 차단 해제 후 쓰기·검색 성공 |
| `drain-node` | 클러스터 노드 제외 필터 | 선택 노드의 shard copy가 실제로 0개가 되는지 확인 | 이전 필터 복원, 전체 green |
| `disk-watermark` | 전체 클러스터 disk allocation 설정 | 현재 여유 공간보다 큰 low/high 요구량 → `disk_threshold: NO`·미할당 | 원래 threshold/interval/활성화 설정 복원 |
| `zone-awareness` | 클러스터 awareness | 세 시드 인덱스의 동일 shard copy가 zone에 분리되고 green인지 | 원래 awareness 값 복원 |
| `rebalance-disabled` | 클러스터 rebalance 설정 | 설정 적용·읽기/쓰기 유지 확인 | 원래 값 복원 |

`all`은 앞의 기본 장애 8개(node-stop부터 drain-node까지)를 순서대로 실행합니다. 매번 원복한 뒤 다음 장애로 넘어가며 첫 실패에서 중단합니다. `disk-watermark`, `zone-awareness`, `rebalance-disabled`는 포함하지 않으며 개별 명령으로 실행합니다.

```bash
./scripts/13-fault-lab.sh run all --yes

# 전체 클러스터 설정을 변경하는 선택 실습
./scripts/13-fault-lab.sh run disk-watermark --yes
./scripts/13-fault-lab.sh run zone-awareness --yes
./scripts/13-fault-lab.sh run rebalance-disabled --yes
```

## 4. green/yellow/red를 해석하는 방법

노드가 내려가면 살아 있는 replica가 primary로 승격되고, 잃어버린 replica를 다시 배치할 수 있습니다. 이 랩의 시드는 node-left delayed timeout 45초를 사용합니다. **45초는 primary 승격을 기다리는 시간이 아니라 replica 재할당 지연 설정**입니다. 남은 노드에 모든 copy를 배치할 수 있으면 정지 노드를 복구하기 전에도 green이 될 수 있습니다. 따라서 노드 장애 시험은 yellow를 반드시 관측해야만 성공한다고 가정하지 않습니다. [공식 delayed allocation 설명][delayed]

green은 모든 shard copy가 할당되었다는 뜻이지, 쓰기 허용·정상 쿼리·충분한 처리량까지 보증하지 않습니다. 이 때문에 쓰기 차단 실습을 별도로 두었습니다. 검색은 `allow_partial_search_results=false`로 요청하고 HTTP 200이어도 `timed_out`와 `_shards.failed`를 검사합니다. [Cluster health][health], [Index blocks][blocks], [Search API][search]

allocation-filter는 기존 시드에 ghost 필터만 설정하는 대신, 새 실험용 인덱스를 처음부터 그 필터로 생성합니다. 기존 shard는 조건이 나빠져도 즉시 사라지는 것이 아니므로, 이 방식으로 미할당 primary와 red를 더 명확하게 관찰할 수 있습니다. 세 시드 인덱스의 검색은 별도로 계속 검사합니다. 클러스터 전체 red와 모든 인덱스의 완전 불능을 같은 뜻으로 해석하지 마세요. [Allocation explain][explain]

## 5. es01 API 진입점 장애와 Cerebro

호스트 9200 포트는 기존처럼 es01에만 연결됩니다. es01이 내려가면 그 주소를 사용하는 일반 curl/기존 시드·검색 스크립트는 실패할 수 있습니다. **다른 ES 노드가 살아 있다는 사실만으로 단일 HTTP 진입점이 자동 복구되지는 않습니다.** 이 랩은 애플리케이션용 로드밸런서를 추가하지 않습니다.

새 장애 도구는 GET 연결 실패 시 같은 Compose 프로젝트의 es02/es04/es05/es03 등에서 `compose exec -T ... curl`로 API에 접근하고 cluster UUID를 확인합니다. 새로운 외부 ES 포트는 열지 않습니다. 전송 오류가 난 쓰기 요청을 다른 노드에 무조건 재전송하지 않습니다. 그 결과 불명확한 쓰기 성공을 은폐하지 않고 실패·원복으로 처리할 수 있습니다.

Cerebro의 연결 목록에는 es01, es02, es04 경로를 추가했습니다. es01 연결 화면이 끊겼으면 홈으로 돌아가 `Lab via es02`를 **수동 선택**하세요. Cerebro 자동 failover 구현을 뜻하지 않습니다. 설정 파일을 갱신한 기존 컨테이너는 다음 명령으로 반영합니다.

```bash
./scripts/compose.sh restart cerebro
```

## 6. 데이터와 설정 보존

실험 시작 전 journal에 cluster UUID, project name, 실제 노드 목록, 수정할 persistent/transient 설정, 선택 인덱스의 원래 replica 값을 기록합니다. 무조건 null이나 replica=1로 되돌리지 않고 **실험 직전 값**을 복원합니다. 다른 설정은 변경하지 않습니다.

쓰기·red·필터 실습에는 고유 이름 `lab-fault-<실험ID>`를 사용합니다. `_mapping._meta`의 실험 소유 표식을 확인한 뒤 해당 인덱스 **한 개만** 제거합니다. 삭제에 `_all`이나 `*`를 사용하지 않습니다. 테스트용 검색·쓰기는 이 canary에만 수행하며 100MiB 시드 내용을 덮어쓰지 않습니다. `too-many-replicas`는 시드의 replica 설정만 잠시 변경합니다.

시드 보존 검사는 인덱스별 문서 수, index UUID, `event_seq` 오름차순으로 선택한 **문서 5개씩**의 `_source` SHA-256을 비교합니다. **모든 문서의 전체 내용 checksum 검사는 아니므로 완전 무결성·무손실의 수학적 증명이 아닙니다.** 일련의 쓰기 부하 중 모든 acknowledged write가 보존되는지 시험하는 부하/내구성 검증도 별도입니다.

한 보고서 디렉터리의 lock/active journal로 중복 실행을 막습니다. 다른 작업 폴더·다른 `--report-dir`·다른 사용자가 같은 클러스터를 조작하는 것까지 분산 잠금으로 막는 도구는 아닙니다. 원복 도중 수동 설정 변경도 하지 마세요.

## 7. 디스크 실습의 정확한 범위

`disk-watermark`는 `dd`, `fallocate` 등으로 실제 디스크를 채우지 않습니다. `_nodes/stats/fs`에서 현재 여유 공간의 최댓값을 읽고 그보다 큰 free-byte low/high 요구량을 설정합니다. 실제 여유 공간이 이미 64MiB 이하이면 시험을 거부합니다. flood_stage는 1바이트 free로 두므로 **의도적으로 flood-stage 쓰기 차단을 일으키는 실습은 아닙니다.** [Disk allocation][allocation]

`write-block` 역시 수동 `index.blocks.write` 실습이지 디스크 사용량에 따라 자동 적용되는 `read_only_allow_delete`의 전체 동작을 재현하지 않습니다. 실제 flood-stage 장애 대응은 디스크 원인을 먼저 해소한 뒤 차단 상태를 확인해야 합니다. 단순히 보호 설정을 끄는 것을 운영 장애 대응으로 습관화하지 마세요.

## 8. 오류가 났을 때

```bash
# 현재 실험 ID, 어떤 노드를 내렸는지, 원래 설정 확인
./scripts/13-fault-lab.sh status

# ES API가 살아 있으면 진단 JSON 수집
./scripts/13-fault-lab.sh diagnose

# 원인 조치 후 기록에 따라 다시 복구
./scripts/13-fault-lab.sh recover
```

`active.json`을 지우거나 `05-reset-cluster-settings.sh`부터 실행하지 마세요. 새 기본 reset/purge 도구는 기본 위치의 active journal이 있으면 중단합니다. 기존 버전에서 이미 적용한 장애에는 원래 값 snapshot이 없으므로 새 recover가 임의로 이전 상태를 추측하지 않습니다. 그 경우 기존 원복 절차로 정상 상태를 먼저 만들고 새 실습을 시작하세요.

컨테이너가 **삭제**되어 `start`가 실패한다면 이 도구는 `up --force-recreate`나 볼륨 삭제로 자동 대체하지 않습니다. 같은 데이터 볼륨으로 노드를 복원하고 같은 cluster UUID인지 확인해야 합니다. `cluster.initial_master_nodes`를 무작정 재설정하거나 데이터 디렉터리를 지우면 다른 클러스터가 만들어질 수 있습니다. 기존 클러스터 재시작에 새 bootstrap 절차를 적용하지 마세요. [Bootstrapping][bootstrap]

`diagnose`는 API가 전혀 응답하지 않으면 수집을 완료할 수 없습니다. 이 경우 먼저 컨테이너 상태와 로그를 확인합니다.

```bash
./scripts/compose.sh ps -a
./scripts/compose.sh logs --tail=150 es01 es02 es03 es04 es05
free -m
sysctl vm.max_map_count
```

## 9. 기존 번호별 스크립트

기존 파일 이름을 유지했습니다. 관리형 장애 wrapper는 최초 적용에 `--yes`가 필요합니다.

```bash
# 기존 이름: 실제 es03 장애를 유지해서 관찰
NODE=es03 ./scenarios/06-node-failure-and-recovery.sh --yes
./scenarios/06-node-failure-and-recovery.sh --recover

# 자동 주입·검증·원복
./scenarios/06-node-failure-and-recovery.sh --test --yes
./scenarios/06-node-failure-and-recovery.sh --crash --test --yes
./scenarios/03-too-many-replicas.sh --test --yes
./scenarios/11-disk-watermark-simulation.sh --test --yes
```

`02/03/05/06/07/08/10/11`은 같은 관리형 검증기를 사용합니다. `04`는 기본적으로 현재 UNASSIGNED shard를 선택하며 미할당이 없으면 이를 명시합니다. `01/09/12/13/14`는 이동·replica 취소·표시·scale-out·쓰기 중 이동 관련 별도 실습입니다. 모든 파일에 대해 실제 ES 실행을 완료했다는 뜻은 아닙니다. 정확한 확인 범위는 [검증 결과](FAULT-VALIDATION.md)를 읽으세요.

## 10. 결과 파일

`reports/faults/active.json`은 진행 중인 실험/재시도 복구용입니다. 완료된 실험은 `reports/faults/<실험ID>.json`, 성공한 기본 전체 suite는 `suite-<ID>.json`, 진단은 `diagnose-<ID>.json`으로 저장합니다. 실패해 원복하지 못한 기록은 active로 남습니다. suite는 첫 실패에서 중단하므로 최종 suite PASS 파일이 없더라도 개별 실험 기록을 확인하세요.

제공 ZIP의 `reports/validation/offline-tests.txt`는 제작 환경에서 실행한 **오프라인/모의 검사** 로그입니다. 사용자 클러스터에서 실행한 보고서와 혼동하지 마세요.

[delayed]: https://www.elastic.co/guide/en/elasticsearch/reference/7.17/delayed-allocation.html
[health]: https://www.elastic.co/guide/en/elasticsearch/reference/7.17/cluster-health.html
[blocks]: https://www.elastic.co/guide/en/elasticsearch/reference/7.17/index-modules-blocks.html
[search]: https://www.elastic.co/guide/en/elasticsearch/reference/7.17/search-search.html
[explain]: https://www.elastic.co/guide/en/elasticsearch/reference/7.17/cluster-allocation-explain.html
[allocation]: https://www.elastic.co/guide/en/elasticsearch/reference/7.17/modules-cluster.html
[bootstrap]: https://www.elastic.co/guide/en/elasticsearch/reference/7.17/modules-discovery-bootstrap-cluster.html
