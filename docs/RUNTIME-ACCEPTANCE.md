# 실제 런타임 인수 시험 — 2026-09-24 결과와 남은 항목

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

ES 9의 실제 bounded write 경로와 cleanup은 확인했습니다. ES 7의 runtime run, search latency workload, image digest 기록, contention 격리/계측은 남아 있습니다.

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

선택 이미지의 digest와 실제 UID, `SELECT @@datadir`, 컨테이너 mount/PVC를 기록합니다. 시험 행 삽입 후 Pod를 하나씩 재생성하여 데이터가 남고 모든 노드의 `wsrep_cluster_status=Primary`, `wsrep_local_state_comment=Synced`, `wsrep_cluster_size=3`, cluster UUID가 일치하는지 확인합니다.

전체 중단 시험은 모든 노드 상태를 모아 UUID/seqno/recover 결과와 `safe_to_bootstrap`를 검토합니다. 최신 상태를 확인하기 전에는 `recovery.confirmed`를 켜지 않습니다. 후보 선정/복구를 자동 완성한 기능은 없으며, 현장 검토 후 허용한 후보만 bootstrap하도록 보호했습니다. 전체 복구 후 승인된 쓰기 집합을 대조하고 bootstrap/recovery 플래그가 꺼졌는지 확인합니다.

## 6. 아직 구현하지 않은 확장

다중 호스트/가용영역 실험, 비대칭 단절·지연·손실·느린 디스크를 통합 제어하는 공통 harness, 복원 데이터 정합성 기반 RTO/RPO 자동 산출, MariaDB→Kafka→Elasticsearch/Redis end-to-end 업무 파이프라인은 이번 수정 범위에 들어 있지 않습니다. 기존 개별 장애 도구는 유지했습니다. 이 단계의 지표나 HA 보장을 이번 오프라인 테스트 수로 대신하지 않습니다.
