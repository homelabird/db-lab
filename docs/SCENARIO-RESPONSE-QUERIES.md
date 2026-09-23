# 시나리오별 대응 쿼리: 개발 → 스테이징 → 운영

각 랩의 장애 실습을 실제 대응 절차로 연결하는 조회 중심 학습 자료입니다. **dev에서는 이 저장소의 격리된 lab 명령으로 재현**, **staging에서는 staging 전용 주소/계정으로 진단과 완화 검증**, **prod에서는 아래 읽기 전용 쿼리로 범위를 확인한 뒤 서비스별 승인된 변경 절차로 조치**합니다. 운영 쿼리는 고정 예제 테이블/토픽 이름을 실제 서비스 값으로 바꿔야 하며, 이 파일의 실습 주입 명령은 운영에서 실행하지 않습니다.

운영 환경에서는 명령행 인수·터미널 기록에 자격 증명을 넣지 않습니다. 아래 예시처럼 환경별 비밀 저장소가 준비한 주소와 클라이언트 설정 파일을 사용합니다.

```bash
# 예: 제한된 환경 파일/credential helper가 준비한 값. 비밀번호를 명령에 직접 쓰지 않는다.
export ES_URL='https://es.example.invalid:9200'
export ES_NETRC='/run/secrets/es.netrc'
export KAFKA_BOOTSTRAP='kafka-bootstrap.example.invalid:9093'
export KAFKA_CLIENT_CONFIG='/run/secrets/kafka-client.properties'
export INDEX='orders-v1' TOPIC='orders.v1' GROUP='orders-search-v1'
export REDIS_HOST='redis.example.invalid' REDIS_PORT='6379'
export REDIS_PASSWORD_FILE='/run/secrets/redis-password'
export SENTINEL_HOST='sentinel.example.invalid' SENTINEL_PORT='26379'
export SENTINEL_PASSWORD_FILE='/run/secrets/sentinel-password'
export MYSQL_CNF='/run/secrets/mariadb-client.cnf'
export KUBECONFIG='/run/secrets/staging-kubeconfig' K8S_NAMESPACE='db-staging'
export API_URL='https://api.example.invalid'
```

## Elasticsearch 7 fault scenarios

Dev 명령은 `elasticsearch/`에서 실행하는 실제 장애 실습입니다. Staging/production 쿼리는 설정과 할당 상태를 읽기만 합니다. `ES_URL`/netrc를 그 환경의 관측 계정으로 연결하고 응답에서 인덱스명·노드명을 확인하세요.

| 시나리오 | Dev 재현 / 확인 | Staging 진단 쿼리 | Production 조회와 대응 경계 |
|---|---|---|---|
| `node-stop` | `./lab.sh fault run node-stop --node es03 --yes` | `curl -fsS "$ES_URL/_cluster/health?pretty"; curl -fsS "$ES_URL/_cat/nodes?v&h=name,master,heap.percent,ram.percent,cpu,roles"` | 같은 두 GET을 수행하고 `/_cat/shards?v&h=index,shard,prirep,state,node`로 replica 승격 여부 확인. 복구는 노드 관리자 절차로만; 복구 전 replica 강제 재배치 금지. |
| `node-crash` | `./lab.sh fault run node-crash --node es03 --yes` | `curl -fsS "$ES_URL/_cluster/health?pretty"; curl -fsS "$ES_URL/_cat/recovery?v&active_only=true"` | health/recovery GET으로 재동기화 중인지 확인. 재시작은 오케스트레이터에서 노드 한 대씩, green과 recovery 종료 확인 후 다음 노드 진행. |
| `master-failover` | `./lab.sh fault run master-failover --yes` | `curl -fsS "$ES_URL/_cat/master?v"; curl -fsS "$ES_URL/_cat/nodes?v&h=name,master,ip"` | 같은 GET으로 elected master가 하나인지 및 노드 수 확인. master 재선출만으로 데이터 문제가 해결된 것으로 보지 말고 cluster health와 shard 상태도 확인. |
| `too-many-replicas` | `./lab.sh fault run too-many-replicas --yes` | `curl -fsS "$ES_URL/_cat/indices?v&health=yellow"; curl -fsS "$ES_URL/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason"` | yellow index 범위와 미할당 사유를 GET으로 확인. 운영 replica 수를 낮추는 변경은 내구성/장애 허용도 검토 및 승인 뒤에만 시행. |
| `allocation-disabled` | `./lab.sh fault run allocation-disabled --yes` | `curl -fsS "$ES_URL/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.enable"; curl -fsS "$ES_URL/_cluster/health?pretty"` | allocation 설정과 unassigned shard를 조회. 의도된 maintenance 여부를 먼저 확인하고, 변경이 원인이면 원래 승인 설정으로 복귀한 뒤 allocation explain/recovery를 관찰. |
| `allocation-filter` | `./lab.sh fault run allocation-filter --yes` | `curl -fsS -H 'Content-Type: application/json' "$ES_URL/_cluster/allocation/explain?pretty" -d '{}'` | 실제 affected index/shard를 명시해 allocation explain 조회. 필터 제거/완화는 정책·장애 도메인 조건 확인 후 변경관리로 수행. |
| `write-block` | `./lab.sh fault run write-block --yes` | `curl -fsS "$ES_URL/lab-fault-*/_settings?filter_path=*.settings.index.blocks.write"; curl -fsS "$ES_URL/_cluster/health?pretty"` | `curl --fail-with-body --silent --show-error --netrc-file "$ES_NETRC" "$ES_URL/$INDEX/_settings?filter_path=*.settings.index.blocks.write"`로 설정 GET 후 앱 쓰기 오류율 대조. 운영 write block은 원인 제거·보존 정책 승인 없이 해제하지 않는다. |
| `drain-node` | `./lab.sh fault run drain-node --yes` | `curl -fsS "$ES_URL/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.exclude.*"; curl -fsS "$ES_URL/_cat/shards?v&h=index,shard,prirep,state,node"` | exclude 설정과 해당 노드 shard 수를 조회. 계획된 drain이라면 남은 공간·복제본·복구 부하를 확인하고 승인된 설정만 적용; 원복 뒤 실제 shard 0개/green 확인. |
| `disk-watermark` | `./lab.sh fault run disk-watermark --yes` | `curl -fsS "$ES_URL/_nodes/stats/fs?filter_path=nodes.*.name,nodes.*.fs.total"; curl -fsS "$ES_URL/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.disk.*"` | 같은 읽기 쿼리로 노드별 여유 공간과 effective watermark 확인. 용량 확보/증설을 우선; watermark 완화는 실제 잔여 공간보다 큰 위험을 만들 수 있으므로 일반 완화책으로 사용 금지. |
| `zone-awareness` | `./lab.sh fault run zone-awareness --yes` | `curl -fsS "$ES_URL/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.awareness.*"; curl -fsS "$ES_URL/_cat/shards?v&h=index,shard,prirep,state,node"` | awareness 속성 및 shard/node 배치를 조회. 실제 failure domain label이 맞는지 먼저 확인; replica 재배치는 승인된 토폴로지 변경으로만 진행. |
| `rebalance-disabled` | `./lab.sh fault run rebalance-disabled --yes` | `curl -fsS "$ES_URL/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.rebalance.enable"; curl -fsS "$ES_URL/_cluster/health?pretty"` | effective rebalance 설정, pending tasks, recovery를 함께 관찰. 의도된 maintenance가 끝난 것이 확인되면 변경관리에서 원래 값으로 복귀; 반복 변경으로 recovery를 흔들지 않는다. |

Staging/production은 해당 환경의 `ES_URL`/`ES_NETRC`를 설정합니다. allocation explain은 POST지만 클러스터 설정을 변경하지 않는 조회 API입니다. Dev의 numeric `scenario 01..15` shard 실습은 fault catalog와 별도이며, 운영 대응은 `/_cat/shards`, `/_cluster/allocation/explain`, `/_cluster/health` 조회로 원인을 좁힌 뒤 `FAULT-DRILLS.md`의 주제를 참고합니다.

### Elasticsearch numeric scenario scripts 01–15

아래는 기존 `elasticsearch/scenarios/` 스크립트 전부입니다. staging helper는 현재 환경의 `ES_URL`/`ES_NETRC`를 사용하며, production helper는 authenticated GET만 수행합니다. Dev 명령만 실제 lab 변경을 포함합니다.

```bash
curl_es() { curl --fail-with-body --silent --show-error --netrc-file "$ES_NETRC" "$ES_URL$1"; }
es_get() { curl --fail-with-body --silent --show-error --netrc-file "$ES_NETRC" "$ES_URL$1"; }
```

| 시나리오 | Dev 학습 명령 | Staging 진단 쿼리 | Production 진단/대응 |
|---|---|---|---|
| 01 manual shard move | `./lab.sh scenario 01` | `curl_es '/_cat/shards?v&h=index,shard,prirep,state,node'; curl -fsS -H 'Content-Type: application/json' "$ES_URL/_cluster/allocation/explain?pretty" -d '{}'` | `es_get '/_cat/shards?v&h=index,shard,prirep,state,node'`; 의도치 않은 move는 audit 확인 후 owner 승인. |
| 02 drain node | `./lab.sh scenario 02 --test --yes` | `curl_es '/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.exclude.*'; curl_es '/_cat/shards?v&h=index,shard,prirep,state,node'` | 같은 GET으로 제외 필터와 shard 이동 확인. 공간 여유/recovery 동시성 확인 전 drain을 반복하지 않는다. |
| 03 too many replicas | `./lab.sh scenario 03 --test --yes` | `curl_es '/_cat/indices?v&health=yellow'; curl_es '/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason'` | affected index health와 unassigned reason 조회. 복제 수 축소는 내구성 승인 후. |
| 04 allocation explain | `./lab.sh scenario 04` | `curl --fail-with-body --silent --show-error --netrc-file "$ES_NETRC" -H 'Content-Type: application/json' "$ES_URL/_cluster/allocation/explain?pretty" -d '{}'` | 같은 조회로 운영 decider 확인; decider 결과에 맞춘 승인된 변경만. |
| 05 impossible allocation filter | `./lab.sh scenario 05 --test --yes` | `curl_es '/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.*'; curl -fsS -H 'Content-Type: application/json' "$ES_URL/_cluster/allocation/explain?pretty" -d '{}'` | 설정/explain으로 불가능한 node filter 특정. 유효 노드 배치를 storage/domain owner와 확인. |
| 06 node failure and recovery | `NODE=es03 ./lab.sh scenario 06 --test --yes` | `curl_es '/_cluster/health?pretty'; curl_es '/_cat/recovery?v&active_only=true'; curl_es '/_cat/nodes?v&h=name,master,roles'` | 같은 세 GET으로 cluster, recovery, 노드 수 확인. orchestrator 복구 뒤 recovery 완료·green 확인. |
| 07 disable allocation | `./lab.sh scenario 07 --test --yes` | `curl_es '/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.enable'; curl_es '/_cluster/pending_tasks?pretty'` | allocation 값과 pending task 조회. maintenance 종료/원래 설정 확인 후 승인된 change로 복귀; `all`로 무조건 켜지 않음. |
| 08 zone awareness | `./lab.sh scenario 08 --test --yes` | `curl_es '/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.awareness.*'; curl_es '/_cat/shards?v&h=index,shard,prirep,state,node'` | 같은 GET과 node attributes 확인. failure domain 설정 확인 전 awareness 완화 금지. |
| 09 cancel replica recovery | `./lab.sh scenario 09` | `curl_es '/_cat/recovery?v&active_only=true'; curl_es '/_cluster/pending_tasks?pretty'` | recovery 진행률/elapsed와 pending task 조회. cancel은 shard owner 승인 후; 반복 cancel/retry로 회복을 굶기지 않는다. |
| 10 rebalance control | `./lab.sh scenario 10 --test --yes` | `curl_es '/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.rebalance.enable'; curl_es '/_cat/shards?v&h=index,shard,prirep,state,node'` | 같은 GET으로 설정·분포 확인. 설정 변경 전 shard skew와 workload 확인 및 승인. |
| 11 disk watermark | `./lab.sh scenario 11 --test --yes` | `curl_es '/_nodes/stats/fs?filter_path=nodes.*.name,nodes.*.fs.total'; curl_es '/_cluster/settings?include_defaults=true&filter_path=*.cluster.routing.allocation.disk.*'` | 노드별 free bytes/effective watermark 조회; 공간 확보/증설 우선, watermark 상향/비활성화는 일반 대응으로 사용하지 않음. |
| 12 primary vs replica | `./lab.sh scenario 12` | `curl_es '/_cat/shards/$INDEX?v&s=shard,prirep&h=index,shard,prirep,state,docs,store,node'; curl_es '/_cluster/health/$INDEX?pretty'` | 실제 index health와 primary/replica 위치를 비교. replica가 primary와 같은 물리 failure domain에 있는지 node attribute로 검증. |
| 13 scale out node | `./lab.sh scenario 13` | `curl_es '/_cat/nodes?v&h=name,ip,roles,heap.percent,disk.used_percent'; curl_es '/_cat/shards?v&h=index,shard,prirep,state,node'` | node join/roles/disk와 shard movement 읽기. 실제 증설은 capacity plan·license·deployment 승인 필요, 단순 node count로 수용량 완료 판정 금지. |
| 14 relocation under write load | `./lab.sh scenario 14`와 별도 `./lab.sh load` | `curl_es '/_cat/recovery?v&active_only=true'; curl_es '/_cat/indices/$INDEX?v&h=index,docs.count,store.size'; curl_es '/$INDEX/_count'` | recovery/문서 count를 시간 간격을 두고 비교하고 application write ack/error metric과 대조. 운영에 synthetic writer를 실행하지 말고 기존 traffic telemetry 사용. |
| 15 Kubernetes rolling upgrade | Dev에서는 disposable cluster에서 `./lab.sh k8s doctor && ./lab.sh k8s verify`와 `./lab.sh k8s upgrade <이미지>` | staging context에서 `kubectl rollout status statefulset/<es-statefulset> -n <namespace>`와 ES `/_cat/nodes`, `/_cluster/health` | 먼저 `kubectl get pods -o wide`, `kubectl rollout history`, `kubectl rollout status` 및 ES health/nodes를 읽는다. prod upgrade는 이 lab 명령이 아니라 승인된 단계별 rollout/rollback procedure로 진행하고 shard recovery 끝나기 전 다음 node를 교체하지 않는다. |

numeric scripts의 dev 명령은 lab 전용입니다. 운영에서 shard movement/cancel/allocation 변경 API를 실행하는 예시는 제공하지 않습니다. 먼저 조회로 상태를 확정하고 실제 변경은 운영 변경관리 runbook에 따릅니다.

### 운영 공통 조회 함수

```bash
es_get() { curl --fail-with-body --silent --show-error --netrc-file "$ES_NETRC" "$ES_URL$1"; }
es_get '/_cluster/health?pretty'
es_get '/_cat/nodes?v&h=name,master,heap.percent,cpu,roles'
es_get '/_cat/shards?v&h=index,shard,prirep,state,node,unassigned.reason'
es_get '/_cluster/pending_tasks?pretty'
```

이 함수는 GET만 허용하는 읽기 예시입니다. 대응 변경 API를 실행하기 전에는 실제 shard/index 범위, 기존 설정, 백업/복구 가능성, 변경 승인과 rollback 값을 별도로 확인합니다.

## Kafka failure scenarios

Kafka 명령은 staging/production 전용 bootstrap과 ACL 제한된 client config를 사용합니다. `lab.sh demo/fault`는 lab 자원을 중단하거나 전용 topic을 만들 수 있으므로 dev 격리 lab에서만 실행합니다.

| 시나리오 | Dev 재현 / 확인 | Staging 진단 쿼리 | Production 조회와 대응 경계 |
|---|---|---|---|
| `broker-failover` | `./lab.sh demo broker-failover` | 실행 report의 전용 topic을 `TOPIC`으로 설정하고 `kafka-topics --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --topic "$TOPIC"` | 실제 topic leader/replicas/ISR describe. ISR 복귀와 producer 오류율 확인; leader 수동 고정/재전송 전에 결과 불명 요청을 원본 key로 대조. |
| `controller-failover` | `./lab.sh demo controller-failover` | KRaft: `kafka-metadata-quorum --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" describe --status`; broker: `kafka-broker-api-versions --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG"` | 모드에 맞는 quorum/controller 상태와 broker 로그/변경 시각을 대조. ZooKeeper 모드에서는 ZK quorum/leader를 별도 모니터링으로 확인; broker controller와 ZK leader를 혼동해 재기동하지 않는다. |
| `min-isr` | `./lab.sh demo min-isr` | run report topic을 `$TOPIC`으로 지정해 `kafka-topics --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --topic "$TOPIC"`; `kafka-configs --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --entity-type topics --entity-name "$TOPIC" --describe` | topic ISR/RF와 min.insync.replicas 조회. ISR 복구 우선; min ISR 하향은 내구성 저하를 승인한 경우에만. |
| `zk-one` | `./lab.sh demo zk-one` (ZooKeeper mode only) | ZooKeeper mode staging lab에서 `./lab.sh zk`와 broker health 확인 | ZK quorum/latency/session telemetry와 broker controller 변화 확인; 한 ZK 응답 저하만으로 전체 장애라 단정하지 않는다. |
| `zk-quorum` | `./lab.sh demo zk-quorum` (ZooKeeper mode only) | ZK mode staging에서 세 ensemble member 각각 `./lab.sh zk`의 serving 결과와 broker/controller health를 확인 | quorum 수, ZK latency/연결, broker controller/topic API 실패율 확인. 운영 노드 추가 중단·무조건 재시작 금지; ZK 담당의 recovery 절차 호출. |
| `lag` | `./lab.sh demo lag` | report의 동적 consumer group을 `$GROUP`으로 지정해 `kafka-consumer-groups --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --group "$GROUP"` | partition별 offset/lag 조회. 입력률·처리율·downstream 오류 비교; partition/key skew를 보고 확장, offset reset 금지. |
| `hot-key` | `./lab.sh demo hot-key` | staging 유사 workload의 `$GROUP`을 `kafka-consumer-groups --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --group "$GROUP"`로 확인 | partition별 lag/bytes와 앱 key 분포를 대조. consumer 수는 단일 hot partition을 나누지 않으며 key 변경은 순서/repartition 검토 후 배포. |
| `oversize` | `./lab.sh demo oversize` | report의 전용 `$TOPIC`에 `kafka-configs --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --entity-type topics --entity-name "$TOPIC" --describe`; broker default도 같은 명령에 `--entity-type brokers --entity-default` 사용 | producer error code/record batch와 topic/broker/consumer 제한 조회. 분할/압축 우선; 제한 상향은 메모리·네트워크 영향 검토/변경관리 후. |
| `retention` | `./lab.sh demo retention` | report의 전용 `$TOPIC` 설정을 `kafka-configs --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --entity-type topics --entity-name "$TOPIC" --describe`로 보고 `./lab.sh offsets "$TOPIC"`로 low/high offsets 확인 | topic config와 low/high watermark를 보존 기간과 비교. 삭제된 low offset은 설정 원복으로 복구되지 않으므로 원본/archive 재생성 확인. |
| `pause-broker` | `./lab.sh fault pause-broker 1` → `./lab.sh status` | `kafka-topics --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --topic "$TOPIC"`; `kafka-consumer-groups --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --group "$GROUP"` | 장애 broker ISR/leader 및 client 오류율 조회. broker 프로세스/호스트 상태 확인 후 on-call 절차로 복귀시키고 새 leader/ISR 수렴 검증. |
| `isolate-broker` | `./lab.sh fault isolate-broker 1` → `./lab.sh status` | `kafka-topics --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --topic "$TOPIC"`; `kafka-consumer-groups --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --group "$GROUP"` | partition leader/ISR, 네트워크 drop/latency와 broker 로그를 연계. prod network isolation은 이 lab 명령으로 하지 말고 네트워크 운영팀의 승인된 경로에서만 실행. |

데모 topic/group은 실행별 동적 이름입니다. Staging에서 lab demo를 확인할 때 `reports/run-*/output.log`에서 topic/group을 복사해 `TOPIC`/`GROUP`에 설정합니다. Production에서는 해당 서비스의 실제 안정 topic/group을 지정합니다. 공통 읽기 전용 형식은 다음과 같습니다.

```bash
kafka-topics --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --topic "$TOPIC"
kafka-consumer-groups --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --group "$GROUP"
```

`zk-one/zk-quorum`의 health probe는 ZK 배포별 인증·TLS 설정을 받아야 하므로 무인증 `nc` 명령을 운영 예시로 제공하지 않습니다. hot-key의 key별 분포는 Kafka protocol CLI만으로 정확히 질의할 수 없으며 producer/app telemetry가 필요합니다.

## MariaDB Galera incident scenarios

Dev에서는 `mariadb-ha-lab`의 `incident_lab` 합성 테이블만 사용합니다. Staging/production에서는 실제 schema를 대상으로 조회하되 큰 범위의 `EXPLAIN ANALYZE`, table rebuild, node stop/pause, quorum drill을 실행하지 않습니다. SQL 접속은 인증정보를 옵션에 직접 넣지 않는 defaults-extra-file로 합니다.

```bash
export MYSQL_CNF='/run/secrets/mariadb-client.cnf'
mysql --defaults-extra-file="$MYSQL_CNF" --batch --raw -e 'SHOW GLOBAL STATUS LIKE "wsrep_cluster_status";'
```

환경별로 별도 `MYSQL_CNF` 파일을 제공합니다. production 계정은 `SELECT` 권한만 가진 계정이어야 합니다.

| 시나리오 | Dev 재현 / 확인 | Staging 진단 쿼리 | Production 조회와 대응 경계 |
|---|---|---|---|
| `slow` | `./lab.sh scenario slow --leave-broken`; `./lab.sh sql galera1 'EXPLAIN FORMAT=JSON SELECT SUM(amount) FROM incident_lab.slow_orders WHERE customer_id=42;'` | `SELECT DIGEST_TEXT,COUNT_STAR,ROUND(SUM_TIMER_WAIT/1e12,3) AS total_seconds,ROUND(AVG_TIMER_WAIT/1e9,3) AS avg_ms,SUM_ROWS_EXAMINED FROM performance_schema.events_statements_summary_by_digest ORDER BY SUM_TIMER_WAIT DESC LIMIT 10;` 및 동일한 업무 query의 `EXPLAIN FORMAT=JSON SELECT * FROM orders WHERE status="created" ORDER BY created_at DESC LIMIT 100;` | digest 상위 query와 scan rows를 read-only로 확인. staging에서 인덱스 후보/write 비용 검증; 운영 DDL은 온라인/잠금 영향과 rollback 승인 뒤 별도 수행. |
| `fragmentation` | `./lab.sh scenario fragmentation --leave-broken`; `./lab.sh sql galera1 'SELECT table_name,data_length,index_length,data_free FROM information_schema.tables WHERE table_schema="incident_lab";'` | `SELECT table_name,data_length,index_length,data_free FROM information_schema.tables WHERE table_schema=DATABASE() ORDER BY data_free DESC LIMIT 20; SHOW ENGINE INNODB STATUS\G` | `SELECT table_name,engine,data_length,index_length,data_free FROM information_schema.tables WHERE table_schema=DATABASE() ORDER BY data_free DESC LIMIT 20;`로 후보 파악. `OPTIMIZE TABLE`/`ALTER TABLE FORCE`는 rebuild·공간·metadata lock 검토 및 maintenance 승인 없이 금지. |
| `node-failure` | `./lab.sh scenario node-failure --node galera1 --hold 20` 후 `./lab.sh scenario inspect` | 각 node에서 `SHOW GLOBAL STATUS WHERE Variable_name IN ("wsrep_cluster_status","wsrep_cluster_size","wsrep_ready","wsrep_local_state_comment","wsrep_connected");` 그리고 HAProxy stats 조회 | 살아 있는 각 node에 같은 status 조회를 실행해 UUID/Primary/Synced/membership 합의 여부 확인. node recovery는 Galera/SST 담당 절차로 1대씩; 통신불능 쓰기 결과는 request ID를 조회해 중복 재시도를 막는다. |
| `node-hang` | `./lab.sh scenario node-hang --node galera1 --hold 20` | `SHOW FULL PROCESSLIST; SHOW GLOBAL STATUS LIKE "wsrep_flow_control_paused";` 및 proxy backend health | SQL thread/flow control과 proxy backend 상태를 읽고 정상 노드에 신규 연결 가능한지 확인. 운영 컨테이너를 pause하는 대신 orchestrator·host health로 격리; 살아있는 것처럼 보이는 hang을 kill하기 전 미완료 트랜잭션 영향을 확인. |
| `quorum` | **격리 lab에서만:** `./lab.sh scenario quorum --hold 45 --confirm-quorum` | 세 node 각각 `SHOW GLOBAL STATUS WHERE Variable_name IN ("wsrep_cluster_status","wsrep_cluster_size","wsrep_ready","wsrep_connected");` | node별 Primary/Non-Primary와 size 조회 및 네트워크 분할 확인. 과반수 상실 시 bootstrap 변수·safe_to_bootstrap를 임의 변경하지 말고 공식 Galera recovery runbook/on-call 승인 경로 사용; 쓰기 가능한 partition 하나만 선택. |
| `lock` | `./lab.sh scenario lock --hold 20`; `./lab.sh sql galera1 'SELECT * FROM information_schema.INNODB_LOCK_WAITS;'` | `SELECT * FROM information_schema.INNODB_LOCK_WAITS; SHOW FULL PROCESSLIST; SELECT trx_id,trx_state,trx_started,trx_wait_started,trx_mysql_thread_id,trx_query FROM information_schema.INNODB_TRX ORDER BY trx_started;` | 같은 read-only lock/processlist 조회. blocker transaction과 업무 요청을 식별한 뒤 소유 서비스에 취소/rollback 요청; `KILL`은 소유자·트랜잭션 검토 및 승인 후에만. |
| `demo` | `./lab.sh scenario demo --rows 5000 --hold 20` | 각각 slow digest/EXPLAIN, `information_schema.tables.data_free`, Galera status 순으로 확인 | demo를 운영에서 실행하지 않는다. slow digest·공간·Galera 상태만 개별 read-only 조사하고 각 담당 팀의 별도 변경관리로 조치한다. |

`performance_schema`가 비활성/수집 설정이 없으면 빈 결과를 정상으로 해석하지 말고 proxy/APM slow query telemetry를 사용합니다. Galera quorum 복구는 SQL 한 줄로 해결할 수 있는 상태가 아니므로 read-only status 조회 후 토폴로지와 가장 최신 데이터 상태를 가진 node를 운영 recovery 절차에서 결정합니다.

## Redis Sentinel/replication/backup scenarios

Redis HA lab scenarios are separate from the standalone `redis-perf` scenarios below. Dev commands mutate only the local lab. Staging/production checks use protected Redis and Sentinel ACL credentials; establish the active master from Sentinel rather than assuming a container name is primary.

```bash
export SENTINEL_HOST='sentinel.example.invalid' SENTINEL_PORT='26379'
export SENTINEL_PASSWORD_FILE='/run/secrets/sentinel-password'
scli() { REDISCLI_AUTH="$(<"$SENTINEL_PASSWORD_FILE")" redis-cli --no-auth-warning --tls -h "$SENTINEL_HOST" -p "$SENTINEL_PORT" "$@"; }
scli SENTINEL MASTER mymaster
scli SENTINEL REPLICAS mymaster
scli SENTINEL SENTINELS mymaster
scli SENTINEL CKQUORUM mymaster
scli SENTINEL GET-MASTER-ADDR-BY-NAME mymaster
```

Redis `INFO replication`/`ROLE` must be queried on the Redis endpoint selected by that environment's service discovery; do not assume it is the same host/port as Sentinel.

| 시나리오 | Dev 재현 / 확인 | Staging 진단 쿼리 | Production 조회와 대응 경계 |
|---|---|---|---|
| Master failover | `./lab.sh fault kill-master`; `./lab.sh status`; `./lab.sh recover` | `scli SENTINEL MASTER mymaster; scli SENTINEL CKQUORUM mymaster; rcli INFO replication; rcli ROLE` | Sentinel master/repl/sentinel/quorum and new master `ROLE`, app reconnect/unknown-write telemetry. Existing master를 강제 재승격하지 말고 새 master와 backlog/dataset을 확인. |
| Sentinel one down | `./lab.sh fault stop sentinel-3`; `./lab.sh cli sentinel-1 SENTINEL CKQUORUM mymaster`; then lab master drill | `scli SENTINEL SENTINELS mymaster; scli SENTINEL CKQUORUM mymaster; scli SENTINEL GET-MASTER-ADDR-BY-NAME mymaster` | 남은 Sentinel 수/quorum/known primary와 app failover 결과 조회. 센티널 단일 장애 시 운영 세 번째 멤버를 불필요하게 재시작하지 말고 quorum margin을 확인. |
| Sentinel quorum unavailable | Dev-only sequence: stop sentinel-2 and sentinel-3, `SENTINEL CKQUORUM`, then master failover observation; `./lab.sh recover` | `scli SENTINEL CKQUORUM mymaster; scli SENTINEL MASTER mymaster;` 및 세 Sentinel health 조회 | 다수결 부족과 primary service 여부 분리. quorum을 1로 낮추거나 redis role을 수동 변경하지 말고 Sentinel membership 복구 전까지 writes impact를 담당자에게 알린다. |
| Master pause | `./lab.sh fault pause-master`; observe `./lab.sh status`; `./lab.sh recover` | `scli SENTINEL MASTER mymaster; scli SENTINEL CKQUORUM mymaster;` 및 Redis endpoint `PING/ROLE` | Sentinel 전환 상태와 app timeout/readonly를 대조. 운영 host/container pause는 사용하지 않고 platform health와 승인된 traffic failover 절차 활용. |
| Master network isolation | `./lab.sh fault isolate master`; status and `./lab.sh recover` | Sentinel의 master/repl 조회와 각 Redis endpoint `ROLE`, 연결 오류/replication link 지표 | 비대칭 partition 여부를 network telemetry와 함께 판단. 운영 bridge disconnect/iptables 수동 변경 금지; network owner 승인 하에 경로를 격리. |
| Fixed endpoint vs Sentinel client | `./lab.sh workload --mode fixed --fixed-node redis-2 --seconds 120` 이후 장애; 다음 회차 `--mode sentinel` | `scli SENTINEL GET-MASTER-ADDR-BY-NAME mymaster`; 각 client pool의 연결 target/readonly/error telemetry | 앱이 Sentinel에서 매 재연결 시 master를 재발견하는지 측정. 고정 Redis IP를 운영 설정에서 사용 중이면 client team에 전환 요청; 장애 중 쓰기 결과 불명은 재조회. |
| Replica stop/full resync | `./lab.sh fault stop redis-2`; master에 합성 key 저장; `./lab.sh recover redis-2`; Replica INFO 확인 | `rcli INFO replication; rcli INFO persistence; rcli ROLE` (rcli를 active Redis endpoint로 설정) | role/master_host/master_link_status/sync progress, offset와 resync logs 확인. full resync가 예상 밖이면 replication backlog, disk/load와 network 확인; replica 데이터를 임의 삭제하지 않음. |
| WAIT replication acknowledgement | dev single connection: `./lab.sh cli master`, then `SET lab:practice:wait same-connection` and `WAIT 2 1000` in that same session | same authenticated Redis connection에서 business canary key SET 뒤 `WAIT 1 1000`; 반환 확인 수와 subsequent read 기록 | 기존 업무 write에 WAIT를 붙여 무손실을 주장하지 않는다. 앱의 write+WAIT telemetry와 replica count를 확인하고 WAIT timeout은 write rollback으로 해석하지 않는다. prod canary writes require separate approval. |
| RDB backup and sandbox restore | `./lab.sh backup`; `./lab.sh restore-sandbox <backup-file>`; `./lab.sh sandbox-recover` | `rcli INFO persistence; rcli LASTSAVE`; isolated staging restore에서 `DBSIZE`와 known key/value compare | 백업 job exit/age/size/checksum 및 격리 restore 성공 기록 확인. production restore는 새 sandbox에서 검증 후 승인된 cutover; 운영 원본에 직접 restore하거나 backup을 replication과 혼동하지 않는다. |
| CPU throttle | `./ops.sh cpu --seconds 15 --rate 5000 --yes` | container `cpu.stat` throttled_usec와 Redis latency/probe 시계열을 비교; `rcli INFO commandstats` | cgroup CPU throttling/host saturation과 Redis/app latency를 확인. 운영 CPU limit 상향은 node headroom·neighbor impact 확인 후 승인. |
| Replica resynchronization under load | `./ops.sh replication --yes`; then `./ops.sh results` | `rcli INFO replication; rcli INFO stats`; ops report에서 write backlog와 sync progress를 대조 | replication link/sync, master offset, replication backlog, disk/network headroom 확인. 재동기화를 반복 유발하지 말고 load 원인을 낮춰 primary/replica sync 안정 확인. |
| HA write/recovery validation | `./ops.sh ha-test --yes` | staging dedicated run에서 `./lab.sh workload`, fault/failover, `./lab.sh verify latest`; 결과가 synthetic-only인지 확인 | 운영 `ha-test`를 실행하지 않는다. 기존 workload/error/unknown-write telemetry, current master, replica state, Sentinel quorum 조회 후 장애 대응 승인 절차 사용. |
| Replica auth mismatch | `./lab.sh fault auth-replica redis-2`; `./lab.sh cli redis-2 INFO replication`; `./lab.sh recover redis-2` | replica `INFO replication`, `master_link_status`, logs, Sentinel master 조회; Redis admin auth와 replication auth를 구분 | replica link down/log auth error, secret rotation event를 확인. replication credential을 secret manager에서 맞추고 전체 auth를 끄지 않는다; 재연결/full sync를 확인. |
| Maxmemory write rejection | `./lab.sh sandbox-oom`; `./lab.sh cli redis-sandbox INFO memory`; `./lab.sh sandbox-recover` | disposable sandbox `INFO memory`, `INFO stats`, `CONFIG GET maxmemory maxmemory-policy` | `OOM command not allowed` vs cgroup OOMKilled를 분리해 memory/maxmemory/rejected writes 조회. 운영 policy/limit 변경 전 cache/object ownership과 eviction 영향 확인. |
| RDB persistence failure / MISCONF | `./lab.sh sandbox-persistence`; `./lab.sh cli redis-sandbox INFO persistence`; `./lab.sh sandbox-recover` | `INFO persistence`, `LASTSAVE`, storage mount free bytes/permissions, Redis logs 확인 | `rdb_last_bgsave_status`, save error 및 filesystem capacity/permissions 확인. `stop-writes-on-bgsave-error no`로 우회하지 말고 저장 실패 원인을 고쳐 승인된 save와 write recovery 확인. |

`WAIT` 행은 production의 임의 쓰기를 권하지 않습니다. 실제 업무에 적용할지 정하는 것은 제품의 write durability 계약과 client connection 경계에 달려 있습니다.

### Elasticsearch 9 운영 조회 범위

ES 9 랩에는 ES 7처럼 fault-injection scenario catalog가 없습니다. 이 랩의 snapshot, seed, shard 및 Kibana 설치 검증을 운영 장애 주입으로 오해하지 않도록, staging/production에서는 인증된 읽기 요청만 예시로 둡니다.

```bash
es_get '/_cluster/health?pretty'
es_get '/_cat/nodes?v&h=name,master,roles,heap.percent,cpu,disk.used_percent'
es_get '/_cat/indices?v&health=red,yellow'
es_get '/_cat/shards?v&h=index,shard,prirep,state,node,unassigned.reason'
es_get '/_snapshot/lab-snapshots/_all?pretty'
```

실환경 저장소명·인덱스 이름은 실제 설정에 맞춥니다. snapshot create/restore/delete는 승인된 백업 runbook에서만 수행합니다.

## Redis perf scenario 대응

`redis-lab/ops.sh run`은 독립형 `redis-perf` 실험입니다. Dev에서만 실험을 실행합니다. Staging/production 명령은 `INFO`, `SLOWLOG`, `CLIENT LIST`, `XINFO` 등 관측만 수행합니다. 예시 `rcli`는 TLS/ACL 연결을 승인된 secret provider가 제공한다고 가정합니다.

```bash
export REDIS_HOST='redis.example.invalid' REDIS_PORT='6379'
export REDIS_PASSWORD_FILE='/run/secrets/redis-password'
rcli() { REDISCLI_AUTH="$(<"$REDIS_PASSWORD_FILE")" redis-cli --no-auth-warning --tls -h "$REDIS_HOST" -p "$REDIS_PORT" "$@"; }
```

| 시나리오 | Dev 재현 / 확인 | Staging 진단 쿼리 | Production 조회와 대응 경계 |
|---|---|---|---|
| `bigkey` | `./ops.sh run bigkey --fields 100000 --yes`; `./ops.sh cli SLOWLOG GET 20` | `rcli SLOWLOG GET 20; rcli INFO commandstats; rcli MEMORY USAGE <key>` | 같은 조회를 `rcli`로 수행하고 명령별 latency/CPU를 대조. cache 구조를 쪼개거나 bounded field fetch로 바꾸는 앱 배포를 검증; 운영 key 삭제/UNLINK는 데이터 소유자 승인 전 금지. |
| `fragmentation` | `./ops.sh run fragmentation --mib 32 --yes`; `./ops.sh cli INFO memory` | `rcli INFO memory; rcli INFO stats; rcli MEMORY STATS` | allocator fragmentation bytes/ratio와 RSS, workload를 read-only 비교. `MEMORY PURGE`/active defrag 설정 변경은 latency/CPU 영향 측정과 승인 뒤 수행하며 RSS 즉시 감소를 복구 완료로 보지 않는다. |
| `eviction` | `./ops.sh run eviction --yes`; `./ops.sh cli INFO memory; ./ops.sh cli INFO stats` | `rcli INFO memory; rcli INFO stats; rcli CONFIG GET maxmemory maxmemory-policy` | used/maxmemory, evicted_keys, rejected_connections 및 key TTL 확인. maxmemory/policy 상향·변경보다 cache/object 수명과 원본 부하를 검증; 원본 데이터 Redis라면 eviction을 완화책으로 선택하지 않는다. |
| `connections` | `./ops.sh run connections --yes`; `./ops.sh cli INFO clients; ./ops.sh cli CLIENT LIST` | `rcli INFO clients; rcli INFO stats; rcli CONFIG GET maxclients` | connected_clients/rejected_connections와 client name/age를 조회하되 CLIENT LIST 결과를 안전하게 취급. app connection pool/leak을 고친 뒤 capacity 변경 승인; maxclients만 올려 OS FD 고갈을 악화시키지 않는다. |
| `slow-consumer` | `./ops.sh run slow-consumer --seconds 20 --yes`; `./ops.sh cli CLIENT LIST` | `rcli CLIENT LIST; rcli INFO clients; rcli SLOWLOG GET 20` | Pub/Sub subscriber의 omem/oll/obl 및 disconnect를 조회. 중요 이벤트 유실 여부를 원본/재처리 로그로 확인; Pub/Sub 재접속은 과거 메시지를 되돌리지 않으며 production buffer limit 변경은 메모리 budget 검토 후 수행. |
| `persistence` | `./ops.sh run persistence --mib 48 --yes`; `./ops.sh cli INFO persistence; ./ops.sh cli INFO memory` | `rcli INFO persistence; rcli INFO memory; rcli LASTSAVE` | rdb_bgsave 상태, last save, COW/RSS, 저장 오류를 조회. 쓰기 보호를 끄지 말고 저장 경로/권한/용량을 해결한 다음 승인된 BGSAVE·복구 절차 진행. |
| `latency` | `./ops.sh run latency --seconds 20 --yes`; `./ops.sh results` | `rcli LATENCY LATEST; rcli LATENCY DOCTOR; rcli SLOWLOG GET 20; rcli INFO commandstats` | 같은 명령과 APM p95/p99를 대조해 Redis server time과 network/client wait를 구분. latency monitor 설정 변경은 staging에서 비용을 검증하고 prod change approval 후. |
| `cache-stampede` | `./ops.sh run cache-stampede --workers 24 --yes` | `rcli INFO stats; rcli INFO commandstats; rcli INFO clients` | miss rate/origin request concurrency를 애플리케이션 metric에서 확인하고 Redis는 connection/command baseline 조회. TTL jitter/singleflight를 staging 부하에서 검증; 분산 lock을 prod 만능 해법으로 적용하지 않는다. |
| `stream-pending` | `./ops.sh run stream-pending --yes`; `./ops.sh cli XPENDING <stream> <group>` | `rcli XINFO GROUPS <stream>; rcli XPENDING <stream> <group>; rcli XLEN <stream>` | PEL, idle time, consumer, stream length를 확인하고 downstream side-effect ledger와 message ID를 대조. claim은 처리 idempotency 확인 후 제한 batch로 승인; ACK-only로 업무처리 성공을 단정하지 않는다. |

`<key>`, `<stream>`, `<group>`은 해당 환경의 실제 이름으로 바꿉니다. 성능 실험의 report는 운영 실측이 아니며 `redis-perf`에서 관측한 수치를 production capacity로 환산하지 않습니다.

## Helm chart recovery scenarios

Helm 차트의 두 복구 주제는 Redis Sentinel failover와 MariaDB 전체 중단 후 복구입니다. chart README가 실제 Helm/Kubernetes 기동 검증 대기 상태로 표시하므로 아래는 관측 예시입니다. Dev/staging은 전용 disposable context에서만 실습하고, prod에서는 지정된 context와 namespace를 읽기 전용으로 조회합니다.

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$K8S_NAMESPACE" get pods -o wide
kubectl --kubeconfig "$KUBECONFIG" -n "$K8S_NAMESPACE" get statefulsets,services,pvc
kubectl --kubeconfig "$KUBECONFIG" -n "$K8S_NAMESPACE" get events --sort-by=.lastTimestamp
```

| 시나리오 | Dev/staging 확인 | Production 진단/대응 경계 |
|---|---|---|
| Redis Sentinel failover | 위 조회로 Redis/Sentinel pod와 endpoint 확인. 승인된 Sentinel 연결에서 `SENTINEL MASTER mymaster`, `SENTINEL CKQUORUM mymaster`; Redis endpoint에서 `ROLE`, `INFO replication`을 확인해 새 primary와 replica link가 안정됐는지 본다. | Pod restart/readiness 및 event와 Sentinel quorum/master, Redis `ROLE`/`INFO replication`, 애플리케이션 reconnect/error 지표를 대조. peer headless Service를 쓰기 primary endpoint로 취급하지 말고 PVC 삭제나 강제 승격을 하지 않는다. |
| MariaDB full outage / recovery | Pod/PVC/event 조회 후 각 살아있는 MariaDB pod에서 `SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_cluster_size','wsrep_ready','wsrep_local_state_comment');` 및 `SELECT @@server_uuid,@@datadir;`를 확인. 완전 중단 후 각 PVC의 seqno/recovery 결과를 비교하고 선택 전에 chart README의 수동 승인 조건을 따른다. | 각 pod의 Galera Primary/size/ready/state, `@@server_uuid`, PVC attach/event와 오류 로그를 대조. ordinal 0이라는 이유만으로 bootstrap하지 않는다. `bootstrapOrdinal`/`confirmed` 설정은 데이터 최신성 검증과 서비스 소유자 승인 후 복구 절차에서만 적용. |

`./all.sh k8s up`는 컨텍스트와 PVC를 변경할 수 있으므로 이 읽기 가이드에서 실행하지 않습니다. 실제 chart 실행/복구 보장은 disposable Kubernetes acceptance run을 통과해야 합니다.

## MVP end-to-end scenarios

MVP는 개발/교육용 단일 노드 주문 파이프라인이지 prod 배포물이 아닙니다. **Dev에서만 장애를 주입**하고 staging에서는 동일한 read-only 신호를 확인한 뒤 복구 훈련을 합니다. Prod 열은 실제 서비스의 equivalent query/telemetry를 나타내며 `all.sh mvp simulate/messages/drills`는 prod에서 실행하지 않습니다.

서비스별 staging/prod 공통 조회:

```bash
# API health/diagnostics: loopback URL 또는 환경별 내부 ingress로 지정
curl --fail-with-body --silent --show-error "$API_URL/health/ready"
curl --fail-with-body --silent --show-error "$API_URL/api/diagnostics"
# SQL: MVP 호환 schema인 경우에만 실행 (직접 연결은 read-only 계정)
mysql --defaults-extra-file="$MYSQL_CNF" -NBe 'SELECT COUNT(*) AS pending, COALESCE(TIMESTAMPDIFF(SECOND,MIN(created_at),UTC_TIMESTAMP()),0) AS oldest_s FROM outbox WHERE sent_at IS NULL;'
# Kafka consumer group
kafka-consumer-groups --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --group "$GROUP"
# Elasticsearch search projection count / shard health
curl --fail-with-body --silent --show-error --netrc-file "$ES_NETRC" "$ES_URL/$INDEX/_count?pretty"
curl --fail-with-body --silent --show-error --netrc-file "$ES_NETRC" "$ES_URL/_cluster/health/$INDEX?pretty"
# Redis process signals, not business-data inspection
rcli INFO stats
rcli INFO memory
```

| 시나리오 | Dev에서 학습 | Staging 대응 쿼리/조치 | Production 대응 쿼리/조치 |
|---|---|---|---|
| `baseline` | `bash ./all.sh mvp simulate run baseline --seed 42 --yes`; 결과 `summary.json`/`audits.jsonl` 확인 | `/health/ready`, `/api/diagnostics`, `mvp smoke`로 시작 상태와 주문→검색 수렴 확인 | API health와 위 SQL/Kafka/ES 조회로 정상 기준값 기록. prod에서 baseline 쓰기 workload를 실행하려면 사전 승인·전용 테스트 tenant가 필요. |
| `kafka-outage` | `bash ./all.sh mvp simulate run kafka-outage --seed 42 --yes` | `SELECT COUNT(*),MIN(created_at) FROM outbox WHERE sent_at IS NULL;` + group lag + canary order의 `GET /api/orders/{id}`/`GET /api/search?q={id}`. broker 복구 후 pending 감소와 검색 반영 확인 | outbox oldest age, consumer lag, API 승인/검색 비노출 비율을 상관. broker/topic 복구는 Kafka on-call 절차; timeout 요청은 동일 idempotency key로 SQL 조회 후에만 재시도. |
| `es-outage` | `bash ./all.sh mvp simulate run es-outage --seed 42 --yes` | `GET /api/diagnostics`, ES `/_cluster/health/$INDEX`, consumer group lag, outbox. ES 준비 후 lag 감소 및 대표 문서 검색 대조 | ES cluster/index health, search 5xx, Kafka lag와 source DB count를 읽기. 검색 의존성 복구를 담당 팀에 이관; ES down 중 자동 대량 replay/재색인을 금지하고 backpressure/headroom 확인. |
| `redis-outage` | `bash ./all.sh mvp simulate run redis-outage --seed 42 --yes` | `GET /api/diagnostics`, `rcli PING`, `rcli INFO stats`; API 상세 응답의 `source`가 MariaDB fallback인지 확인 | Redis reachability/connection errors와 API DB QPS/pool saturation을 함께 확인. DB 우회 부하를 감당할지 확인하고 app/Redis 연결 복구; 캐시 key 전체 삭제는 하지 않는다. |
| `db-freeze` | `bash ./all.sh mvp simulate run db-freeze --workload read-heavy --seed 42 --yes` | `GET /health/ready`, `/api/diagnostics`, SQL `SHOW FULL PROCESSLIST; SELECT * FROM information_schema.INNODB_TRX;`, API status/latency 비교 | DB connection wait, lock waits, `Threads_connected`, app pool wait 및 error rate를 읽기. API readiness를 회복 목표로 삼고 blocker/DB owner에 이관; 운영 DB/container pause 명령은 금지. |
| `worker-freeze` | `bash ./all.sh mvp simulate run worker-freeze --seed 42 --yes` | SQL pending+oldest age, Kafka group lag, worker health/logs, canary SQL 주문과 검색. worker 재개 후 lag/outbox drain 및 동일 ID ES 반영 확인 | SQL outbox age, group partition lag/throughput, worker error/restart 지표를 대조. 원인/consumer 상태를 고친 후 제한 병렬도로 catch-up; offset reset 금지. |
| `redis-recreate` | `bash ./all.sh mvp simulate run redis-recreate --seed 42 --yes` | `rcli INFO server`의 run_id, `INFO persistence`, API cache MISS/HIT와 DB 부하 확인; 같은 named volume 여부는 staging orchestrator inspect로 확인 | Redis restart/replacement event, persistence/replication 상태, app reconnect 및 cold-cache DB QPS 확인. 컨테이너 재생성 전 volume identity·복제 역할 확인; Redis 교체만으로 데이터 초기화하지 않는다. |
| `redis-switch` | `bash ./all.sh mvp simulate run redis-switch --seed 42 --yes` | active Redis endpoint/role, API 연결 설정, 대표 key MISS와 상세 원본 fallback을 확인. revert 뒤 기존 cache가 stale인지 version 비교 | 설정 변경 history와 연결 대상, cache hit ratio, DB fallback load를 조회. blue/green endpoint cutover는 승인된 app config 변경으로 하고 기존 캐시의 의미/TTL 검증 후 전환. |
| `row-lock` | `bash ./all.sh mvp simulate run row-lock --seed 42 --yes` | `SELECT * FROM information_schema.INNODB_LOCK_WAITS; SELECT trx_id,trx_state,trx_wait_started,trx_query FROM information_schema.INNODB_TRX;` + API 1205 비율 | `INNODB_LOCK_WAITS`, processlist, transaction age와 endpoint timeout을 읽기. blocker 소유자와 영향 범위를 확인해 취소/rollback 승인; 전체 DB 재시작은 첫 조치가 아님. |
| `duplicate-retry` | `bash ./all.sh mvp simulate run duplicate-retry --seed 42 --yes` | API 결과 주문 ID, SQL `SELECT id,idempotency_key,status,version FROM orders WHERE idempotency_key='<run-key>';`의 단일 행 검증 | duplicate/write rate와 같은 idempotency key의 주문 건수/요청 로그를 조회. 중복 과금/주문은 원장과 결제 provider를 대조한 뒤 보상 절차; 자동 DELETE 금지. |
| `version-race` | `bash ./all.sh mvp simulate run version-race --workers 4 --seed 42 --yes` | 같은 order id의 version/status와 한 요청 200·나머지 409 확인; `SELECT id,status,version FROM orders WHERE id='<canary-id>';` | 409 비율을 5xx와 분리하고 affected order의 현재 version/audit log를 읽기. 클라이언트는 최신 version 재조회 후 business rule 확인; conflict를 무조건 재시도하지 않는다. |
| `db-network-delay` | helper 준비 뒤 `bash ./all.sh mvp simulate run db-network-delay --workload write-heavy --seed 42 --yes` | API/DB latency, `Threads_connected`, SQL pending/outbox age와 container network telemetry를 비교; helper 복원 후 연결·정합성 확인 | app-side SQL pool wait/timeout과 DB latency/flow control/network RTT를 계층별 비교. 운영 host firewall/qdisc는 변경하지 말고 network/DB 담당과 승인된 telemetry로 진단. |
| `db-network-loss` | helper 준비 뒤 `bash ./all.sh mvp simulate run db-network-loss --workload write-heavy --seed 42 --yes` | netem helper 전용 통계, SQL reconnect/timeout, unresolved HTTP 요청을 같이 확인; 키 조회로 결과를 확정 | packet retransmit/drop, SQL connection resets, client unknown 결과를 대조. 재시도는 idempotency key 유지; 네트워크 장비 rule 변경은 해당 팀 변경관리로 수행. |

실제 주문 ID/key는 보고서에서 가져옵니다. prod SQL에서 `<run-key>` 같은 문구를 그대로 실행하지 말고, 허가된 안전한 조회 도구로 해당 key를 바인딩합니다. 사용 가능한 MVP SQL helper는 `orders`, `outbox`, `counts`, `ping` 조회로 제한되므로 prod 전용 schema 조회와 혼동하지 않습니다.

### 메시지/정합성 및 disposable DB drills

이 드릴들은 잘못된 입력·offset·중복·원자성 경계를 재현하므로 개발 격리 lab 전용입니다. Staging/production은 아래 조회로 증상을 관측할 뿐, 시뮬레이터/임시 테이블을 실행하지 않습니다.

| 시나리오 | Dev 실행 | Staging 관측/대응 쿼리 | Production 관측/대응 쿼리 |
|---|---|---|---|
| `poison-schema` | `bash ./all.sh mvp messages run poison-schema --yes` | `kafka-consumer-groups --bootstrap-server "$KAFKA_BOOTSTRAP" --command-config "$KAFKA_CLIENT_CONFIG" --describe --group "$GROUP"`; 전용 DLQ topic offset/error metric, SQL 주문과 ES 문서 version 대조 | 같은 group lag/commit, DLQ publish/ACK, schema rejection telemetry 확인. schema owner가 호환 event와 승인된 replay tool을 사용; source offset 직접 seek 금지. |
| `mapping-reject` | `bash ./all.sh mvp messages run mapping-reject --yes` | ES `GET /$INDEX/_mapping`, `GET /_cluster/health/$INDEX`; consumer error/DLQ offset. mapping 변경은 additive staging test 후 replay 확인 | mapping rejection과 index UUID/version, DLQ를 읽기. 기존 field 의미를 바꾸는 live mapping 변경을 즉흥 적용하지 말고 versioned index migration 및 source-based reindex plan 사용. |
| `projection-commit-gap` | `bash ./all.sh mvp messages run projection-commit-gap --yes` | consumer group committed offset과 ES document realtime GET/version를 같은 run event id 기준으로 비교; 중복 전송이 같은 문서로 수렴하는지 확인 | event ID, partition/offset, ES doc version, group commit을 연계. offset 재설정 없이 idempotent replay 경로 검증; “ES에 있다”만으로 offset 완료를 가정하지 않는다. |
| `dlq-commit-gap` | `bash ./all.sh mvp messages run dlq-commit-gap --yes` | DLQ에서 동일 `dlq_id` 개수와 source committed offset 확인; 중복 quarantine는 하나의 business repair만 만들어야 함 | DLQ ACK/commit 실패 metric, duplicate DLQ ID와 원본 상태를 조회. 승인된 replay consumer의 idempotency를 확인하고 원본 offset을 운영자가 직접 되감지 않는다. |
| `replay-ordering` | `bash ./all.sh mvp messages run replay-ordering --yes` | SQL current version 및 ES `_source.version`/document version을 비교하고 오래된 메시지가 최신 문서를 덮지 않았는지 확인 | source row version, event version, projection version/lag를 대조. stale event drop count를 관측; 수동 version 올리기나 오래된 snapshot overwrite 금지. |
| `version-collision` | `bash ./all.sh mvp messages run version-collision --yes` | event ID/version와 ES stored doc을 비교; conflict가 DLQ에 격리되고 SQL 현재 내용으로만 복구됐는지 확인 | 동일 ID+version payload hash conflict metric과 원본 row를 조회. 원본 사실 확인·producer bug 수정 후 versioned repair; ES 문서를 임의 덮어쓰지 않는다. |
| `stock-race` | `bash ./all.sh mvp drills run stock-race --clients 4 --yes` | staging 주문/재고 ledger의 합계를 repeatable-read read-only transaction으로 확인; available+paid qty=initial | 실제 재고 row와 paid 주문 합계를 같은 consistent snapshot으로 비교. oversell이면 신규 checkout 제한/업무 보정 승인 후 재고 원장 대사; 재고 숫자만 UPDATE 금지. |
| `duplicate-checkout` | `bash ./all.sh mvp drills run duplicate-checkout --clients 4 --yes` | idempotency key별 주문/요청/원장 count를 read-only 집계하고 결제 provider transaction ID와 대조 | 같은 provider idempotency key와 주문/원장 건수, settlement를 비교. 중복 청구는 provider 승인된 void/refund 업무 절차; DB row 삭제 금지. |
| `checkout-rollback` | `bash ./all.sh mvp drills run checkout-rollback --yes` | 같은 case의 inventory/wallets/requests/orders/ledger/events를 snapshot 비교해 실패 경계 후 partial commit 확인 | 결제 요청 ID별 주문·차변/대변·outbox를 read-only join/집계하고 provider 결과 확인. orphan 발견 시 transaction owner/결제 운영 절차로 보상, 수동 잔액 조정 금지. |
| `commit-ambiguity` | `bash ./all.sh mvp drills run commit-ambiguity --yes` | timeout request key를 SELECT로 찾아 commit 여부를 확정한 후 같은 키에서만 재시도; 새 key 생성 금지 | API timeout/결제 provider 결과 불명 요청을 동일 idempotency key로 조회. DB/PG 둘 다 미확정이면 자동 재청구를 멈추고 정산 대사 queue로 이관. |
| `idempotency-conflict` | `bash ./all.sh mvp drills run idempotency-conflict --yes` | 동일 key의 저장 fingerprint와 retry payload를 비교, 두 번째 업무 변경이 없는지 확인 | key 재사용 conflict metric, 원 요청 fingerprint 및 호출자 로그를 조회. 같은 key로 다른 내용은 409/업무 오류 처리; fingerprint를 임의 덮어쓰지 않는다. |
| `refund-race` | `bash ./all.sh mvp drills run refund-race --clients 4 --yes` | 주문 상태·환불 원장·inventory event를 주문 ID 기준 조회해 refund 1회만 존재하는지 확인 | 환불 요청 key별 order transition, PG refund ID, ledger를 대조. 중복 환불은 provider 정정 절차와 감사 로그로 처리; `paid` 상태를 강제로 되돌리지 않는다. |
| `backup-restore` | `bash ./all.sh mvp drills run backup-restore --yes` | 원본/복원 snapshot의 orders/outbox row count·checksum과 canary write/read 결과 확인 | 기존 백업 검증 기록, row count/checksum, restore test 시각을 조회. 운영 복원은 승인된 새 격리 DB에서 검증한 뒤 cutover 계획으로만 수행; live schema 덮어쓰기 금지. |
| `upgrade-restore` | 미리 pull한 버전으로 `bash ./all.sh mvp drills run upgrade-restore --candidate-image "$CANDIDATE" --yes` | 원본/candidate MariaDB `SELECT VERSION()`, logical snapshot checksum, canary schema/business query 비교 | candidate replica/clone의 error log·schema check·대표 read/write 결과를 읽기. prod datadir in-place upgrade 전에 백업/rollback rehearsal 승인 필수. |
| `redis-disk-full` | `bash ./all.sh mvp drills run redis-disk-full --yes` | disposable Redis `INFO persistence`, `INFO stats`, AOF 오류와 ENOSPC 기록 확인 | Redis persistence error, `aof_last_write_status`, container OOM/disk alerts, app write failures 조회. prod 파일시스템을 채우거나 AOF 오류 쓰기보호를 끄지 말고 저장 경로/용량 담당에게 이관. |
| `redis-oom` | `bash ./all.sh mvp drills run redis-oom --yes` | disposable container inspect `OOMKilled/ExitCode`, Redis logs, 원본 Redis 상태 불변 확인 | container runtime OOM event, cgroup memory, Redis `INFO memory`, host pressure 조회. cache/process OOM과 Redis maxmemory rejection 구분; host 전체 메모리 한도를 급히 높이지 않는다. |
| `deadlock` | `bash ./all.sh mvp drills run deadlock --yes` | `SHOW ENGINE INNODB STATUS\G; SELECT * FROM information_schema.INNODB_LOCK_WAITS;` 및 helper의 실제 1213 victim | deadlock section의 latest victim/transaction SQL digest와 retry count를 확인. 짧은 transaction·일관된 lock ordering을 staging 검증하고 deadlock retry는 idempotent transaction 경계로 제한. |

임시 `txlab` 이름과 MVP의 production 대응 대상은 같은 데이터베이스가 아닙니다. prod business schema에는 소유 서비스가 승인한 동등한 read-only query를 준비해야 합니다. staging에서 Kafka를 조회할 때는 위의 전체 `--bootstrap-server` 및 `--command-config`를 포함한 helper 형식을 사용하세요.
