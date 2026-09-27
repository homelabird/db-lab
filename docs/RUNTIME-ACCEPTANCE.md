# 실제 런타임 인수 시험 — 2026-09-27까지의 결과와 남은 항목

이 문서는 실제 실행 증거와 미수행 항목을 분리합니다. 아래 kind 결과는 단일 노드 disposable 클러스터에서 수행한 Redis 프로필의 범위이며, 다른 DB/provider나 production HA를 보증하지 않습니다.

## 2026-09-24: kind + Helm 4.1.1 Redis acceptance

| 항목 | 결과 |
|---|---|
| 격리 | `db-lab-accept2`, 전용 `/tmp/db-lab-kind-kubeconfig-2`, namespace/release `db-lab`; 기존 `k3d-board-msa` context는 사용하지 않음 |
| 런타임 | kind Kubernetes v1.31.2, Docker Engine 29.6.2, Helm v4.1.1, 단일 kind worker, 기본 `standard` StorageClass |
| 설치/준비 | chart 설치 및 2차 bootstrap seal 완료; Redis 3/3, Sentinel 3/3, PVC 6개 Bound, EndpointSlice 연결 확인 |
| 복제/데이터 | Sentinel `CKQUORUM` 통과; 합성 키 쓰기 후 `WAIT 2 5000`에서 2 replica ACK; replica 재생성 뒤 동일 PVC와 키 보존 확인 |
| 장애 전환 | primary Pod 삭제 후 Sentinel이 Redis 2를 primary로 선출; 세 Sentinel 동의, Redis 0 재합류해 replica로 복제; 합성 키 유지 확인 |
| NetworkPolicy | 같은 release label을 가진 probe는 Redis에 도달해 예상 `NOAUTH`; unlabeled probe는 연결 timeout. kind CNI에서 해당 정책이 실제 적용됨 |
| 정리 | probe Pod 제거. acceptance 완료 후 전용 Helm release와 kind cluster를 삭제하고 전용 kubeconfig를 제거함; 기존 Docker `kind` 네트워크는 건드리지 않음 |

이 결과는 Redis 일반 Pod 재생성 및 primary Pod 장애 전환의 실제 런타임 근거입니다. Redis 전체 정전, Sentinel/PVC 전체 손실, PVC 삭제 복구, 두 번째 release 격리, Helm upgrade/uninstall 데이터 동작, MariaDB/Kafka/Elasticsearch 기동은 포함하지 않습니다. acceptance 클러스터는 보고서 작성 뒤 제거했으므로 재현 시 아래 절차로 새 격리 클러스터를 만듭니다.

## 2026-09-24: Elasticsearch 9 bounded benchmark

기존 `es9-lab` 다섯 노드 클러스터를 먼저 읽기 전용 status로 확인했습니다. Docker Engine 29.6.2, Elasticsearch 9.5.3, 285/285 shard copies STARTED, health green이었습니다. `./elasticsearch-9/lab.sh benchmark --seconds 5 --rate 5 --batch 5 --seed 42`를 세 번 실행했고, 각 run은 25건을 bulk ack, refresh/count 검증 후 전용 `lab-benchmark-v1` 인덱스를 삭제했습니다. 각 JSON report는 `elasticsearch-9/reports/benchmarks/`에 남았습니다.

공통 comparator에서 세 run은 workload와 host/컨테이너 자원 설정이 일치했습니다. p95 batch latency는 60.754, 45.242, 19.619ms였고 CV는 49.61%였습니다. 따라서 이 세 번은 repeat 산포를 보여주는 데는 쓸 수 있지만 성능 기준선으로 안정적이지 않습니다. 실행 전후 ES node CPU는 45%에서 97%까지, node load average는 24.73에서 51.48까지 관측됐고 백그라운드 경쟁 부하가 컸습니다. comparator는 `performance_comparison_ready: false`로 contention/dataset 상태의 전체 실행 계측 부족을 표시합니다. 이 값으로 ES 7, 다른 DB, production 처리량을 추론하지 않습니다.

ES 9의 실제 bounded write 경로와 cleanup은 확인했습니다. ES 7의 search latency workload, image digest 검증, contention 격리/계측은 남아 있습니다.

### 2026-09-27: isolated ES9 repeat benchmark

새 Compose project `db-lab-bench-es9-20260927`와 전용 container prefix/volumes를 사용해 Podman 5.8.7, kind/provider 외 Docker 자원과 분리된 5노드 ES 9.5.3 green cluster에서 실행했습니다. 고정 image, 8 CPU/16 GiB host, 30초·100 documents/s·100개 batch·seed 42 workload 조건으로 세 report `6df5e581439e4740`, `44a7407db6aa4163`, `f7c74853215d4d6f`를 만들었습니다. 각 run은 3,000건 전부 색인·refresh/count 확인에 성공했고 실패·불확실 요청은 0, 임시 index cleanup과 dataset postcondition도 통과했습니다. 공통 비교기는 `environment_confirmed=true`, `performance_comparison_ready=true`, blocker 없음으로 판정했습니다. p95 batch latency는 84.215/37.301/38.354ms, median 38.354ms, CV 50.27%였습니다. 전체 실행 구간 host sampling은 세 run 모두 약 30초를 덮었고 CPU busy 평균은 17.08–18.55%, load peak는 2.40–6.36이었습니다. comparator의 준비 완료는 비교 가능한 관측자료가 있다는 뜻이며, 큰 p95 산포 때문에 이 run set을 안정된 성능 기준선이나 capacity/SLO 주장으로 쓰지 않습니다. 전용 프로젝트 서비스는 down으로 멈췄고, 테스트용 named volumes는 보존했습니다.

### 2026-09-27: isolated ES7 repeat benchmark

별도 source archive와 고유 project/prefix `db-lab-bench-es7-20260927`, loopback ports로 Podman 5.8.7 / Elasticsearch 7.17.29 5노드 cluster를 실행했습니다. 설치 확인에서 cluster green, 14/14 shard copies, snapshot repository, Kibana와 Cerebro가 준비됐습니다. 30초·100 documents/s·100개 batch·seed 42 workload 세 run `0dd977ba93994015`, `baf3c5b1eecd40d7`, `19c9a6fc982b4c72`는 각각 3,000건을 색인하고 refresh/count 확인 뒤 임시 index를 삭제했습니다. 실패·불확실·scheduler-skipped 요청은 모두 0이었습니다. comparator는 environment/configuration과 full-window observation, dataset gates를 통과했습니다. p95 batch latency는 163.414/45.470/45.776ms (median 45.776ms, CV 80.11%), p99 CV는 84.03%였습니다. host window sampling은 약 30초, 평균 CPU busy 19.01–22.55%, load peak 4.42–7.29였고 측정 중 available memory는 약 7.84GB였습니다. source archive에 `.git`이 없어 source revision은 `null`, container CPU/memory limits는 수집되지 않았습니다. 따라서 이 결과는 bounded bulk write/cleanup만 입증하며 ES7 search latency, 장애 복구, capacity/SLO는 검증하지 않습니다. raw reports와 comparator는 로컬 git-ignored `elasticsearch/reports/benchmarks/` 및 `reports/benchmarks/es7-comparison-2cfcb35b497a.json`에 있습니다. 격리 cluster와 전용 volumes를 purge했고 기존 ES9 volumes는 남겼습니다.

### 2026-09-27: isolated Redis repeat benchmark

소스 archive 복사본에서 새 lab identity `db-lab-bench-redis-20260927`와 별도 Podman volumes를 사용했습니다. Podman 5.8.7 / Redis 7.4.11의 3 Redis + 3 Sentinel topology에서 authenticated write/read와 `WAIT 2` 복제 ACK가 통과한 뒤 `perf` target에 30초·300 offered requests/s·8 workers·2,000 keys·256-byte payload·80% reads·70% hot-key workload를 세 번 실행했습니다. 세 run 모두 `PASS`, Redis command failures 0, queue drops 0, dataset cleanup/postcondition 통과였습니다. 원시 export와 공통 비교 JSON은 각각 로컬 git-ignored 경로 `redis-lab/output/ops-20260927T133838-44638/`와 `reports/benchmarks/redis-comparison-5fea6a8db28d.json`에 보존했습니다.

Comparator는 `configuration_comparable=true`, `environment_confirmed=true`, `performance_comparison_ready=true`, blocker 없음으로 판정했습니다. p50 median은 0.4045ms (CV 4.65%), p95 median 0.9383ms (CV 10.97%), p99 median 1.7541ms (CV 36.9%)였습니다. 한 run에서 부하 스케줄러가 77개 요청 시작 시각을 건너뛰었고 나머지 두 run은 2개와 3개를 건너뛰었습니다. host sampling은 run마다 31.85–32.48초를 덮었습니다. 결과는 반복 workload와 관측 조건이 비교 가능하다는 뜻이며, p99 산포와 skipped scheduling 때문에 안정된 성능 기준선·capacity·SLO로 사용하지 않습니다. 내보낸 report를 보존한 뒤 격리 lab의 컨테이너와 전용 volumes를 reset했고 기존 `rslab` volumes는 남겼습니다. 이 benchmark는 Redis failover/PVC 복구 시험을 포함하지 않습니다.

### 2026-09-27: isolated Kafka repeat benchmark

별도 source archive와 `db-lab-bench-kafka-20260927` identity로 Podman 5.8.7 / Confluent Platform 7.9.0 ZooKeeper mode의 3 ZK + 3 broker lab을 기동했습니다. broker 3개, full ISR, 샘플 topic write/read 준비 검사가 통과한 뒤 payments producer를 30초·1,000 messages/s·256-byte payload·seed 42로 세 번 실행했습니다. run `42b5a7ff44f2`, `7ce789b34cc2`, `ab86450cf4bc`는 각 30,000건을 전부 전달했고 오류·pending은 0이었습니다. 매 run 새 benchmark topic의 offset span이 전달량과 일치했고 삭제 postcondition도 통과했습니다. container image digest는 세 broker에서 `sha256:02170e46da5c36b1581cd42ebfb2f55edae6348c62bc772bc2166841db745b2` 계열로 일치했습니다. raw run JSON은 local git-ignored `kafka-lab/reports/benchmarks/`, comparison은 `reports/benchmarks/kafka-comparison-2e7ac2d246f5.json`에 있습니다.

Comparator는 workload/environment 일치, full-window host observation, dataset 검증을 확인해 `performance_comparison_ready=true`로 판정했습니다. p95 ack latency는 30.870/23.635/31.507ms (median 30.870ms, CV 15.25%), 처리율은 999.0/999.9/999.9 messages/s였습니다. 실행 중 host CPU busy 평균은 52.71–57.45%, peak는 61.66–81.94%였고 host load peak는 3.81–5.60이었습니다. 따라서 결과는 실제 producer·offset·topic cleanup 경로와 이 호스트의 반복 관측만 입증하며 capacity/SLO 기준선은 아닙니다. archive에는 `.git`이 없어 reports의 source revision은 `null`이고 broker cgroup limits도 수집되지 않았습니다. fresh ZK에서 `/brokers/ids`가 아직 없는 경우 pipefail로 종료하던 startup guard와 benchmark topic delete 시 AdminClient handle이 먼저 파괴되던 문제를 고친 뒤 통과했습니다. 격리 topic은 모두 제거됐고 전용 컨테이너/volumes를 reset했으며 기존 `kzk-lab` volumes와 Docker kind `cilium` cluster는 남겼습니다.

### 2026-09-27: isolated MariaDB repeat benchmark

별도 source archive와 `db-lab-bench-mariadb-20260927` project/ports/volumes로 Podman 5.8.7에서 기동했습니다. MariaDB Galera 11.8.9의 세 노드가 모두 `Primary/Synced`, `cluster_size=3`이었고 writer endpoint의 synthetic-transfer benchmark를 30초·4 workers로 세 번 실행했습니다. run `20260927T142310Z-920eaa58`, `20260927T142401Z-28ceda47`, `20260927T142450Z-dec9dee1`은 각각 2,475 / 2,471 / 2,218 committed transactions, retry·unresolved·SQL errors 0으로 끝났습니다. 각 실행에서 임시 계정 row count와 총 잔액이 보존됐고 원본 계정 fingerprint가 그대로였으며 run-scoped 계정/송금 테이블 제거도 확인했습니다. 세 노드는 모두 같은 local node image digest `sha256:e6c066a722649e1df0eea0ad0b370c19c1f91bb931301ffa2132d2efd8836d6e`를 사용했습니다. raw JSON은 local git-ignored `mariadb-ha-lab/reports/benchmarks/`, 공통 비교는 `reports/benchmarks/mariadb-comparison-ab22106501dd.json`에 있습니다.

Comparator는 동일 configuration/environment와 host sampling, dataset postcondition을 확인해 `performance_comparison_ready=true` 및 blocker 없음으로 판정했습니다. committed transactions/s median은 82.254 (CV 6.13%), p50 latency median 26.11ms (CV 5.16%), p95 median 49.68ms (CV 18.49%)였습니다. host CPU busy 평균은 run별 39.44–40.75%였고, source archive에 `.git`이 없어 report revision은 `null`, container CPU/memory limits도 미설정/미수집입니다. 따라서 이 수치는 격리된 단일 호스트의 synthetic workload 관측이며 capacity/SLO 기준선이나 장애 복구 증거가 아닙니다. 진행 중 Podman에서 거부된 Docker 전용 `enable_icc` option 제거, benchmark SQL의 `lab_ops` schema 명시, workload 뒤 변경되는 임시 계정 fingerprint 대신 잔액 보존과 원본 미변경 확인을 적용했습니다. 최종 report를 복사한 뒤 전용 project/volumes를 reset했습니다.

### 공통 비교기 재검증 및 이후 호스트 상태 확인

저장된 ES9 report 5개 모두 같은 workload(5초, 5 docs/s, 5문서 batch, seed 42)와 PASS 상태였습니다. 공통 comparator는 p95 batch latency median 45.242ms, mean 38.776ms, sample stdev 19.344ms, CV 49.89%를 계산했습니다. 두 report에 host/container metadata가 없어 `environment_confirmed=false`였고 `performance_comparison_ready=false`를 유지했습니다. 기존 report 파일만 읽었으며 DB 데이터는 변경하지 않았습니다.

이후 읽기 전용 상태 확인에서 cluster는 green, 285/285 shard copies STARTED, pending task 0이었습니다. 같은 시점 host load average는 9.92/12.94/23.13, ES node CPU는 각 49%, node RAM 92%, disk 82.89%, host swap은 8GiB 중 8GiB 사용 중이었습니다. 이 상태 때문에 추가 실부하는 실행하지 않았습니다. 이는 기존 report의 측정 당시 상태를 설명하는 자료가 아니며 이번 시점의 안전 판단에만 사용합니다.

새 공통 메타데이터 collector를 적용한 뒤 ES9의 기존 실행 컨테이너 다섯 개에 Docker inspect/image inspect를 읽기 전용으로 실행했습니다. 다섯 개 모두 container limit 정보와 immutable image ID를 읽었고, `docker.elastic.co/elasticsearch/elasticsearch@sha256:f456578fc2a620a8a4f4c21d070fff1f6070345adb2be5e5626b65be72aea350` registry digest로 일치했습니다. 이 확인은 Docker inspect 형식과 이미지 identity 수집만 검증하며 DB API 호출이나 부하 실행은 하지 않았습니다.

같은 기존 report 5개를 새 comparator로 다시 읽었을 때 기존 파일의 `container_images`가 없고 host/resource metadata 일부도 누락·불일치하여 `environment_confirmed=false` 및 `performance_comparison_ready=false`를 유지했습니다. 이 report들을 새 환경 증거가 있는 반복 측정으로 간주하지 않습니다.

## 시험 경계

폐기 가능한 전용 VM/클러스터를 사용합니다. 실제 업무 데이터·운영 Secret·운영 kubeconfig를 넣지 않습니다. 기존 데이터를 유지하는 복구 시험은 먼저 백업/복원 가능성을 확인합니다. `reset`, PVC 삭제, 강제 bootstrap, `prune`를 자동 사전 정리로 사용하지 않습니다. 장애 시험은 합의된 대상과 복구 방법을 확인하고 수행합니다.

각 실행에 코드 SHA, 실행 시각, OS/CPU 아키텍처, 엔진/Compose/Helm/Kubernetes 버전, 이미지 digest, 실습 seed, 토폴로지, 설정 hash, 장애 계획을 남깁니다. 환경 파일과 Secret 원문은 증거에 넣지 않습니다. `reports/all-*.json`은 명령 결과의 일부만 기록하므로 위 항목 전체를 자동 수집했다고 간주하면 안 됩니다.

## 1. 정적·생성 검증

```bash
# 전용 Python 환경에서 requirements 설치 후 실행
bash scripts/test-offline.sh
bash scripts/test-helm.sh
```

두 명령을 분리합니다. 첫 명령의 Go renderer는 차트에 필요한 일부 함수만 구현한 계약 검사이며 Helm 대체물이 아닙니다. 두 번째 명령은 실제 Helm이 없으면 127로 종료합니다. 2026-09-24에는 Helm v4.1.1 lint/render가 통과했습니다. 실제 Helm 렌더링만으로 admission, 이미지 pull, 준비 상태, 데이터 보존은 증명되지 않으며, 위 표의 Redis runtime acceptance가 해당 범위만 추가로 입증합니다.

## 2. Compose 신규·기존 볼륨 시험

```bash
./all.sh preflight --json kafka mariadb
./all.sh --dry-run up kafka mariadb
./all.sh up kafka mariadb
./all.sh health --json kafka mariadb
./all.sh status kafka mariadb
```

기본 `all.sh up`은 네 HA 실습과 MVP를 차례로 기동합니다. 첫 실기동 검증은 프로젝트 대상을 좁혀 한 스택씩 수행한 뒤, 기본 전체 배치의 누적 자원과 포트 상태를 확인합니다. Kafka/Redis는 Podman 중심이며 이름만 바꾸어 Docker 지원을 주장하지 않습니다. Elasticsearch와 MariaDB는 각 프로젝트의 provider 선택에 맞추어 실제 지원 조합별로 검증합니다.

| 시험 | 합격 기준 |
|---|---|
| ES 비공개 기본값 | 새 `.env`와 기존 공개 `.env`를 각각 확인. 동의 없는 원격 바인딩은 기동 전에 실패. up/down 전후 호스트 방화벽 불변. |
| ES 정상 준비 | 모든 실습 노드/필수 UI가 준비되고 예상 cluster 이름과 노드 수가 일치. 외부에서 loopback 서비스에 직접 접근 불가. |
| Kafka clean build | 캐시 없는 tools 이미지 빌드. 다른 작업 디렉터리에서도 생성 build.context가 올바름. |
| Kafka zk/kraft | 각각 다른 전용 프로젝트/볼륨으로 토픽 생성, 생산, 소비. KRaft ID 저장/재사용 확인. |
| Kafka 설정 | 포트/heap 변경이 생성 파일, 컨테이너 env, broker advertised 주소에 반영됨. |
| 재시작 | 시험 레코드를 쓴 뒤 일반 down/up. 동일 데이터 및 cluster ID 유지. 삭제 reset을 사용하지 않음. |
| 부하 제어 | 실제 요청률/경과/실패/재접속 분포 확인. 합성 API 지연을 실측으로 사용하지 않음. |
| 부분 실패 | 실패 프로젝트, 잔존 자원, 재개 방법 확인. 다른 데이터 볼륨을 임의 삭제하지 않음. |

MariaDB의 기존 `tests/run-real.sh`는 명시적 `RUN_DISRUPTIVE_TESTS=1`을 요구하는 실제 변경 시험입니다. 스크립트 전체를 먼저 검토한 후 폐기 가능한 환경에서 실행합니다. Redis와 Kafka의 기존 live 시험도 동일하게 read/write/장애 주입 범위를 먼저 확인하세요.

## 3. Helm 신규 배포와 격리

`helmchart/README.md`의 전용 context/Secret/namespace/프로필 절차를 따릅니다. 설치에 사용한 `helm template`, `kubectl get events`, Pod/PVC 상태를 남깁니다. 암호가 포함된 `kubectl get secret -o yaml`은 리포트에 저장하지 않습니다.

같은 namespace에 서로 다른 두 release를 생성하여 각 Service EndpointSlice의 Pod UID 집합이 다른 release와 겹치지 않는지 확인합니다. NetworkPolicy는 허용 label이 있는 클라이언트와 없는 클라이언트의 실제 TCP 접속을 비교하여 검사합니다. CNI가 정책을 지원하지 않으면 보호 기능은 미검증/미적용으로 표시합니다.

노드 안에서 Kafka advertised listener와 POD_NAME, ZooKeeper peer FQDN과 server ID를 확인합니다. 모든 브로커에 접속해 생산/소비하고 ZooKeeper leader/follower 상태를 확인합니다. CPU/RAM/PVC Pending을 애플리케이션 쿼럼 장애와 구분합니다.

## 4. Helm Redis 복제·전환

각 Redis의 `ROLE`, `INFO replication`과 Sentinel 세 개의 `get-master-addr-by-name`, `CKQUORUM`을 확인합니다. 정상 토폴로지는 primary 1 + replica 2, 모든 Sentinel이 같은 primary를 가리키는 상태입니다. headless Redis Service에 무작위 쓰기하지 말고 Sentinel 지원 클라이언트를 사용합니다.

고유 실행 ID를 붙인 데이터를 쓰고 복제 후 비교합니다. primary 프로세스 중지 → Sentinel 선출 → 클라이언트 재연결 → 기존 primary 복귀 → Sentinel 순차/전체 재시작을 수행합니다. 마지막에도 역할/데이터/감시 대상이 일치해야 합니다. 승인된 쓰기 목록과 복구 후 실제 키 집합을 비교하여 누락을 집계합니다. AOF everysec와 비동기 복제의 손실 가능성을 숨기지 않습니다. **PVC를 삭제해 primary를 비우는 것은 정상 재시작 시험이 아닙니다.** 별도 백업/복원 시나리오로 취급합니다.

## 5. Helm MariaDB 보존·전체 복구

2026-09-24에 새 kind v1.31.2 클러스터로 설치를 시도했으나 **미통과**입니다. 기본 `bitnami/mariadb-galera:11.4.5`는 pull되지 않았고, pull되는 legacy digest를 고정했습니다. 발견한 결함은 두 가지입니다. `k8s init`의 43자 암호가 MariaDB 32자 상한을 넘었고, Pod security context의 `runAsGroup: 1001`은 이미지의 `1001:0` 기본 실행 계약을 덮어써 `my.cnf`를 쓸 수 없게 했습니다. 암호 길이를 제한하고 `runAsGroup`을 제거하자 node 0은 설정 파일을 갱신하고 Galera Primary/size 1로 준비됐습니다. 세 Pod의 DNS 해석과 Primary의 TCP 4567 연결은 확인했지만 node 1/2는 `failed to reach primary view`로 종료해 세 노드 quorum을 만들지 못했습니다. 이후 node address를 이미지 기본 IP autodetect로 돌렸지만 재시험에서도 동일한 증상이 남았습니다. 쓰기·PVC 보존·전체복구는 수행하지 않았으며 시험용 kind 클러스터, namespace, PVC와 Secret을 클러스터 삭제로 정리했습니다. 런타임 지원은 미검증 상태입니다. 현재 readiness는 선언된 replica 수만큼 `wsrep_cluster_size`가 일치해야 통과하도록 보강했습니다.

추가로 `scripts/test-helm-mariadb-runtime.sh`에 신선 설치, 3노드 복제, 단일 Pod/PVC 재합류 검증을 자동화했다. 2026-09-24의 격리 4-node kind 실행에서는 node 0이 `Primary/Synced`, cluster_size 1에 머물고 node 1/2가 `failed to reach primary view`로 종료했다. 같은 release label의 probe는 seed TCP 4567/3306 연결에 성공했고, node 1의 로그에는 세 seed 주소와 자기 Pod endpoint가 나타났다. 이는 Pod network 전체 차단 증거는 아니며 Galera peer admission/주소 교환은 여전히 미확인이다. 복제·쓰기는 시도하지 않았고 정확한 acceptance kind cluster와 kubeconfig를 제거했다. 이 acceptance는 아직 **실패** 상태다.

2026-09-24에 보강된 acceptance 스크립트를 다시 실행했다. Helm readiness deadline에서 StatefulSet `Ready: 0/3`으로 실패했고, 자동 진단에서 node 0은 계속 Running, node 1/2는 재시작 중인 것을 확인했다. DNS는 각 ordinal의 고유 Pod IP를 반환했고, node 1에서 node 0의 3306 및 4567/TCP 접속이 성공했다. joiner 로그에는 `Connecting with bootstrap option: 0`, 전체 gcomm seed 목록, 자기 주소 blacklisting 뒤 `Connection refused`와 NON_PRIM view가 기록됐다. 이후 격리 시험에서 fresh/recovery joiner seed를 선택된 bootstrap FQDN 하나로 줄였지만 같은 primary-view 실패가 재현되어 해당 소스 변경은 되돌렸다. 시험 namespace에서만 NetworkPolicy를 삭제한 뒤에도 cluster size는 1로 남았다. 정책 삭제 후 별도 socat probe는 Pod 간 UDP/4567 payload 전달에 성공했다. 따라서 이 결과는 NetworkPolicy나 일반 TCP/UDP Pod 경로가 원인임을 뒷받침하지 않으며, 실제 Galera protocol exchange/admission 실패 지점은 여전히 미확인이다. 이번 시험에서는 데이터 쓰기/PVC 재합류를 하지 않았고 정확한 kind 클러스터와 임시 kubeconfig를 삭제했다.

5분 제한의 두 번째 fresh-install 시험도 `Ready: 0/3` 및 Galera `failed to reach primary view`로 종료됐다. 진단 중 NetworkPolicy를 삭제했기 때문에 release guard도 `NetworkPolicy ... NotFound`를 보고했다. 해당 변경은 전용 namespace에만 있었고 acceptance cleanup이 클러스터 전체를 제거했다. 이 시험은 설치·복제·재합류 acceptance를 통과하지 못했다.

마지막 격리 재시도는 새 kind v1.31.2 4-node cluster, 전용 namespace/release와 임시 kubeconfig로 실행했다. Helm 설치 후 세 PVC(각 5Gi)는 모두 Bound였지만 StatefulSet은 deadline까지 `Ready: 0/3`이었다. node 0은 `Primary/Synced`, `wsrep_cluster_size=1`을 유지했고 node 1/2는 Primary view를 얻지 못해 재시작했다. Pod DNS는 서로 다른 주소를 반환했고 joiner init gate의 seed 3306 연결은 성공했다. kindnet은 NetworkPolicy를 구현/집행하지 않으므로 이 실행에서 firewall 또는 NetworkPolicy를 원인으로 단정할 수 없다. 복제 쓰기나 PVC 재합류 검증은 실행하지 않았다. 스크립트가 해당 kind cluster와 임시 kubeconfig를 정리했으며 사후 `kind get clusters`도 비어 있었다.

2026-09-27 Docker provider acceptance 두 번은 MariaDB 배포 전에 kind worker kubelet이 등록되지 않아 끝났다. `--retain` 재현에서 kubelet이 inotify watcher 생성 중 `inotify_init: too many open files`로 종료되는 것을 확인했다. 호스트 `fs.inotify.max_user_instances`는 128이었지만 어느 limit이 직접 소진됐는지는 분리하지 않았다. 이 결과는 Galera 실패 증거가 아니다. 첫 acceptance 정리 코드가 아직 생성되지 않은 kubeconfig를 조회해 stat 오류를 냈고, 스크립트는 kubeconfig가 없으면 진단 조회를 건너뛰도록 보강했다.

같은 날 kind v0.31.0 / rootless Podman 5.8.7 provider로 acceptance를 실행해 4개 kind node가 Ready인 환경에서 Helm 배포까지 진행했다. PVC 세 개는 Bound였지만 StatefulSet이 10분 내 `Ready: 0/3`으로 끝났다. node 0은 `Primary/Synced`, `wsrep_cluster_size=1`이었고 node 1/2는 `failed to reach primary view`로 재시작했다. 진단에서는 peer TCP/4567 접속이 가능했지만 Galera join은 성공하지 않았다. 복제 쓰기와 PVC 재합류 단계는 실행하지 않았다. 스크립트가 해당 Podman kind cluster, namespace, PVC, Secret 및 임시 kubeconfig를 삭제했고 Docker provider의 기존 `cilium` cluster는 남아 있었다. 당시에는 acceptance 미통과였고 peer protocol/admission 원인은 미확인 상태였다. 이후 기록된 32-byte group-name 원인 수정과 성공적인 수용 결과는 아래 항목을 참고한다.

2026-09-27 Docker 재시도에서는 host `fs.inotify.max_user_instances`를 실행 중에만 128에서 1024로 올려 새 kind v1.31.2 4-node cluster를 Ready까지 기동하고, 종료 뒤 원래 값 128로 복구했다. Helm이 MariaDB 11.4.5 image digest `sha256:c860f10be93cbb180d316e921eae40b9e191822d347e57fcfc2e182f52ce710d`를 배포했고 PVC 3개도 Bound였다. node 0은 `Primary/Synced`, cluster_size 1에 도달했지만 node 1/2는 `handling gmcast protocol message failed`, `Connection refused`, `failed to reach primary view`로 종료했다. 종료 진단에서 node 1은 bootstrap node의 TCP/4567에 연결할 수 있었으나 Galera membership은 형성되지 않았다. init receipt `reports/all-20260927T144821Z-685131.json`은 completed, up receipt `reports/all-20260927T144823Z-685316.json`은 failed다. 쓰기·복제·PVC 재합류는 수행하지 않았다. 스크립트가 전용 cluster와 kubeconfig를 제거했고 기존 `cilium` cluster는 유지됐다. 이 재현은 kind worker 생성 제한을 우회했지만 Galera handshake 원인을 밝히지는 못했다.

후속 분석에서 실제 join 실패 원인을 찾았다. 생성된 Galera group name `maria-accept-788027-db-lab-mariadb`는 34 bytes였지만 upstream GMCAST message의 `group_name`은 [`gcomm::String<32>`](https://github.com/mariadb-corporation/galera/blob/4.x/gcomm/src/gmcast_message.hpp) 필드이며, [문자열 생성자](https://github.com/mariadb-corporation/galera/blob/4.x/gcomm/src/gcomm/types.hpp)는 초과 길이에 `EMSGSIZE`를 던진다. `helmchart/templates/_helpers.tpl`에 최대 32자 이름과 해시 suffix를 만드는 helper를 추가하고 MariaDB chart에서 사용했다. 새 격리 4-node kind v1.31.2 실행은 임시로 올린 inotify limit을 끝에 128로 되돌렸으며, 세 노드가 `Primary/Synced`, cluster_size 3에 도달했다. 합성 행이 세 노드에서 확인됐고, node 2 Pod 재생성 뒤 같은 PVC identity와 행을 보존하며 재합류했다. init receipt `reports/all-20260927T151943Z-833731.json`, up/seal receipt `reports/all-20260927T151944Z-833909.json`은 모두 completed다. 이로써 fresh bootstrap·복제·단일 Pod/PVC 재합류 범위는 통과했으며 앞선 peer-view 실패 기록은 이 원인 수정으로 superseded 됐다. Kubernetes node failure는 시험하지 않았다.

이후 `scripts/test-helm-mariadb-runtime.sh`에 순차 전체 종료, PVC의 UUID/seqno/`safe_to_bootstrap` 읽기 검토, 확인된 후보 복구와 UUID·행·PVC 재검증을 추가했다. 첫 두 Podman 재현은 acceptance parser의 조기 종료와 기본 kubeconfig 사용 문제를 찾아냈고 이를 수정했다. 최종 2026-09-27 rootless Podman 5.8.7 / kind v1.31.2 4-node 실행은 host `fs.inotify.max_user_instances=128`을 바꾸지 않은 채 전체 acceptance를 통과했다. fresh bootstrap 후 세 노드가 Primary/Synced·cluster_size 3이었고, 합성 행 복제와 node 2의 같은 PVC 재합류가 확인됐다. 순차 3→2→1→0 종료 뒤 PVC UUID는 모두 `04a66c1d-ba8f-11f1-84a6-27e2716aa879`로 같았으며 ordinal 0만 `safe_to_bootstrap: 1`이었다(seqno 27; ordinal 1은 26, ordinal 2는 25). 선택한 ordinal 0으로 복구한 뒤 세 노드 readiness, 기존 UUID·행·PVC identity 보존, bootstrap/recovery flag 봉인을 확인했다. receipts: init `reports/all-20260927T161724Z-1117725.json`, fresh up `reports/all-20260927T161725Z-1117839.json`, full recovery `reports/all-20260927T162235Z-1148191.json`; 모두 completed다. Podman kind cluster와 kubeconfig는 정리했고 Docker kind의 `cilium`은 보존했다. 이 결과는 해당 disposable kind/storage path의 전체 정전 복구를 증명하며 Kubernetes node failure, 다른 provider/StorageClass, production 복구는 포함하지 않는다.

선택 이미지의 digest와 실제 UID, `SELECT @@datadir`, 컨테이너 mount/PVC를 기록합니다. 시험 행 삽입 후 Pod를 하나씩 재생성하여 데이터가 남고 모든 노드의 `wsrep_cluster_status=Primary`, `wsrep_local_state_comment=Synced`, `wsrep_cluster_size=3`, cluster UUID가 일치하는지 확인합니다.

전체 중단 시험은 모든 노드 상태를 모아 UUID/seqno/recover 결과와 `safe_to_bootstrap`를 검토합니다. 최신 상태를 확인하기 전에는 `recovery.confirmed`를 켜지 않습니다. 후보 선정/복구를 자동 완성한 기능은 없으며, 현장 검토 후 허용한 후보만 bootstrap하도록 보호했습니다. 전체 복구 후 승인된 쓰기 집합을 대조하고 bootstrap/recovery 플래그가 꺼졌는지 확인합니다.

## 6. 아직 구현하지 않은 확장

다중 호스트/가용영역 실험, 비대칭 단절·지연·손실·느린 디스크를 통합 제어하는 공통 harness, 복원 데이터 정합성 기반 RTO/RPO 자동 산출, MariaDB→Kafka→Elasticsearch/Redis end-to-end 업무 파이프라인은 이번 수정 범위에 들어 있지 않습니다. 기존 개별 장애 도구는 유지했습니다. 이 단계의 지표나 HA 보장을 이번 오프라인 테스트 수로 대신하지 않습니다.
