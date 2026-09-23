# DB Lab 로컬 운영 자동화: 현황 분석과 목표

작성일: 2026-09-23

## 목적

DB Lab을 로컬에서 실제 운영 작업의 흐름을 연습하고 검증하는 도구로 발전시킨다. 핵심은 DB별 실습을 더 많이 만드는 일이 아니라 **같은 안전한 작업 흐름으로 배포하고, 준비 상태를 확인하고, 부하·장애를 관찰하고, 복구를 증거로 판정하는 것**이다.

목표 사용자는 개발자·DBA·SRE가 노트북 또는 폐기 가능한 로컬/원격 Linux 실습 서버에서 다음을 할 수 있는 상태다.

1. 지원되는 런타임과 리소스를 미리 점검한다.
2. 선택한 DB 조합을 Compose, Helm 또는 Ansible로 계획·배포·관리한다.
3. 현실적인 합성 부하를 같은 조건으로 반복하고 결과를 비교한다.
4. 장애·업그레이드·백업 복구를 연습하고 서비스 및 데이터 상태를 확인한다.
5. 자격 증명과 원시 데이터를 불필요하게 노출하지 않는 실행 증거를 보관·공유한다.

이는 운영 서비스에 직접 적용하는 배포 제품이나 production SLO 인증기가 아니다. 관리 대상은 명시적으로 등록·허용한 로컬 실습 환경이며, 운영 대응 학습 쿼리는 읽기 전용 기본값으로 유지한다.

## 현재 코드에서 확인한 것

| 영역 | 이미 있는 기능 | 현재 한계 |
|---|---|---|
| 프로젝트 진입점 | `all.sh`가 개별 랩, MVP, Helm 경로와 `doctor`, `preflight`, `health`, `status`, `down` 등 공통 명령 일부를 제공 | DB별 인수·환경·검증 차이가 남아 있어 공통 명령의 의미와 지원 조합을 계속 문서화해야 함 |
| 시나리오·성능 | Redis/MariaDB/Kafka/ES7/ES9의 versioned report, 공통 반복 비교기와 DB별 bounded workload를 호출하는 `scripts/benchmarks.py run <db>`가 있음 | runner는 이미 준비된 lab에만 workload를 보내며 lab 기동/정리는 하지 않음. 실제 Redis/MariaDB/Kafka/ES7 반복 실측, 전체 실행 구간 contention 계측, image digest 및 dataset 동일성 확인이 남아 있음. MVP comparative study는 실제 DB benchmark가 아님 |
| Ansible | SSH/local inventory, 허용 명령 정책, check mode, 명시적 동의, 단일 호스트 제한, 결과 수집, 새 content-addressed source release 배치, 전송 SHA-256 검증, 제한된 Ubuntu/Debian package bootstrap을 제공 | Debian 12 disposable SSH 컨테이너에서 비루트 `labops`의 task-scoped become, apt bootstrap 멱등성, source 배치 및 Redis `init/doctor/up/health/status/down`이 통과. Ubuntu 조합과 별도 원격 Linux 호스트의 runtime·DB 준비 및 복구는 미검증. 선언형 Ansible DB role이 아니라 기존 도구용 orchestration 계층임. |
| Kubernetes/Helm | 통합 Helm chart와 ES 전용 Helm 경로, context 제한·preflight·Secret/PVC/bootstrap 보호가 있음 | 전용 kind 환경에서 Redis/Sentinel, PVC 보존, CNI NetworkPolicy, primary failover가 통과. MariaDB 전체 복구와 검증하지 않은 CNI/StorageClass 조합은 남아 있음. MariaDB 안전 후보 자동 선택은 의도적으로 미구현 |
| 실행 증거 | MVP acceptance 및 study report, root quality/static/offline/tools 경로, Ansible 결과 수집 경계가 있음 | 프로젝트별 결과 스키마와 수집 수준이 다름. 일부 “pass”는 host/mock/static이고 실제 엔진, HA, 원격 실행과 분명히 구분해야 함 |

세부 근거: [Ansible 구현 범위](ANSIBLE-IMPLEMENTATION.md), [Ansible 사용법](../ansible/README.md), [런타임 인수 기준](RUNTIME-ACCEPTANCE.md), [MVP 비교 실험의 측정 한계](../mvp-lab/docs/COMPARATIVE-STUDIES.md), [품질 검사](QUALITY-GUIDE.md).

## 목표 사용 흐름

```text
사용자 / CI
    │
    ├─ db-lab doctor, list, plan, status
    ├─ db-lab deploy / verify / benchmark / drill / recover / collect
    │
    ▼
공통 실행 계약
입력 검증 · 소유권/context 확인 · 변경 동의 · lock · timeout · receipt
    │
    ├─ Compose adapter ── 기존 각 lab.sh / all.sh
    ├─ Helm adapter ───── 기존 chart 및 guarded all.sh k8s
    └─ Ansible adapter ── 선택한 관리 노드에서 같은 adapter 호출
                             │
                             ▼
                    DB별 workload와 verifier
                             │
                             ▼
            표준화한 비밀 제외 run report / comparison
```

공통 계층은 DB 동작을 다시 구현하지 않는다. 파라미터와 실행 계약을 표준화하고 실제 상태 확인은 기존 랩 도구에 맡긴다. Ansible은 원격 실행 경계, Helm은 Kubernetes 리소스 경계, Compose는 로컬 단일 호스트 경계를 담당한다.

## 권장 진행 순서

### 1. 실행 계약과 지원 행렬 정리

각 대상에 대해 `doctor → plan → apply → verify → status → collect`의 의미, 지원 런타임/버전, 데이터 영향, 실행시간, 증거 종류를 기계 판독 가능한 목록으로 정리한다. 지금의 `all.sh`/Ansible action을 바꾸기 전에 이 행렬에서 명령 별칭·기능 누락·불일치를 찾아 고친다.

**완료 기준:** 모든 public command가 대상, 변경 여부, 동의 인자, 성공 검증기, report 경로를 명시한다. help와 문서, capability listing이 같은 원천에서 나온다. 미지원 조합은 조용히 건너뛰지 않고 blocked로 나온다.

### 2. 로컬 설치·배포와 상태 관리

먼저 깨끗한 임시 checkout/context에서 환경 초기화, 사전 점검, 배포, 준비 확인, 중지/보존, 명시적 purge를 끝까지 검증한다. 현재 사용자의 데이터/volumes에서 시험하지 않는다. OS/runtime 설치는 제한된 Ubuntu/Debian 조합에 opt-in Ansible bootstrap으로 구현했다. Debian 12 비루트 SSH 컨테이너에서 Podman bootstrap과 become을 검증했다. Ubuntu 조합과 별도 원격 Linux 호스트 인수는 남아 있다. 기본 동작은 설치나 방화벽 변경을 하지 않는다.

Ansible을 다음 순서로 넓힌다.

1. 현재와 같이 로컬/SSH에서 검증된 프로젝트 제어
2. 명시적 inventory group, 버전/engine preflight, check mode/plan
3. 사용자가 승인한 OS prerequisite 설치 및 프로젝트 artifact 배치
4. serial rollout, readiness gate, 실패 시 중단 및 수동 재개 receipt
5. 지원되는 `upgrade`, `backup`, `restore rehearsal`, `recover` wrapper

비밀번호나 kubeconfig는 inventory/CLI에 쓰지 않는다. Ansible Vault/external secret source를 통한 전달을 별도 설계하고, report와 stdout에는 secret을 기록하지 않는다. 자동 rollback은 되돌릴 수 있음이 입증된 작업에만 제공한다. DB 데이터 복구나 위험한 schema 변경을 일반적인 `rescue`로 자동 취소하지 않는다.

**완료 기준:** 실제 `ansible-playbook`이 설치된 깨끗한 control VM에서 syntax, check, local fixture를 실행하고, disposable 원격 Linux 호스트에서 SSH·설치·배포·재실행·실패 중단·결과 회수를 검증한다. 멱등성을 주장하는 단계는 두 번 실행해 실제 변경 없음과 서비스 상태를 확인한다.

### 3. DB별 benchmark를 하나의 학습 체계로 연결

기존 벤치마크 코드를 우선 재사용하고, 공통 runner는 실행·메타데이터·결과 판정만 맡는다. DB마다 같은 종류가 아닌 지표를 억지로 TPS 하나에 합치지 않는다.

초기 공통 시나리오:

- `smoke`: 짧은 기능/연결 확인, 성능 비교에 사용하지 않음
- `steady`: warm-up 후 고정 시간·부하의 정상 상태
- `ramp`: 부하를 점진적으로 증가해 오류/latency가 변하는 지점 탐색
- `mixed-read-write`: 실제 workload mix를 명시한 비교
- `recovery-under-load`: 승인된 격리 lab에서만 장애와 복구 동시 관찰

각 결과는 실행 ID, git revision, 이미지 digest, DB 버전/설정, 런타임과 host 자원, 데이터 크기·seed, workload 비율·동시성·속도·warm-up·측정 시간, 수용/실패/timeout 수, p50/p95/p99, 관측 source, 시계 구간, verifier 결과를 기록한다. 민감정보·업무 원문은 제외한다. 최소 반복 횟수, 오차/분산 표시, 안정 구간 판정 전에는 승패를 선언하지 않는다. 자원이 달라진 run은 비교 불가로 표시한다.

**완료 기준:** 같은 환경에서 baseline을 반복해 분산을 보고, 동일 설정 재실행과 workload/버전 변경 비교를 증명한다. 오류 증가나 완료 workload 감소가 latency 개선을 가리지 못한다. 실 DB 결과와 mock/host-only 결과는 다른 evidence kind로 분리된다.

### 4. Kubernetes를 별도 운영 backend로 검증

먼저 현재 Helm chart acceptance를 disposable kind/k3d/minikube 중 실제 지원 대상으로 통과시킨다. resource, PVC, DNS, service discovery, NetworkPolicy enforcement, readiness, restart, clean install/upgrade/uninstall을 기록한다. 이후 control interface에서 Compose와 Helm을 같은 환경으로 가장하지 않고 backend/profile로 명시한다.

**완료 기준:** 최소 한 disposable cluster에서 chart 설치/검증/제거, Redis failover 및 MariaDB 복구 acceptance를 실제 수행. Redis failover/PVC/CNI acceptance는 통과했으며 MariaDB 복구가 남았다. PVC 보존/삭제와 bootstrap 확인을 시나리오마다 검증. 검증되지 않은 CNI/StorageClass/provider 조합은 지원이라고 표시하지 않는다.

### 5. 시나리오형 운영 훈련과 결과 공유

incident drill은 `plan → precondition → inject → observe → recover → postcondition` 형태로 공통 lifecycle을 따른다. 시나리오마다 예상되는 업무 영향과 데이터 invariant, 중단 조건, 수동 개입 경계를 둔다. report는 action/관측값/기대·실제 결과와 evidence level을 기록하고, 증거가 없으면 pass로 추론하지 않는다.

**완료 기준:** 격리한 실제 엔진에서 장애를 주입하고 복구·데이터 불변조건까지 통과한 end-to-end report를 남긴다. 이전 report 재사용, failed를 passed로 변환, 무단 운영 target 연결을 거부한다.

## 최종 목표와 release gate

DB Lab의 최종 목표는 **개발 PC 한 대에서 시작할 수 있고, 필요하면 Ansible로 소유한 격리 서버를 준비·운영하며, Compose/Helm 환경의 DB를 같은 안전 계약 아래 관측·부하·장애·복구 시험하고, 재현 가능한 실행 증거를 비교·공유하는 로컬 운영 자동화 학습 플랫폼**이다.

다음을 모두 증거로 통과하기 전에는 “현장형 자동화 완료”로 부르지 않는다.

- 지원 행렬과 command contract가 전체 랩 및 runtime별로 완성됨
- 새 환경에서 doctor/plan/deploy/verify/down을 재현하고, 데이터 보존과 purge 경계를 검증함
- Ansible real local + loopback SSH source artifact 배치/반복 실행 및 Debian 12 비루트 SSH 컨테이너의 실제 apt bootstrap/멱등 재실행 검증 완료; 별도 원격 Linux target의 DB 준비·rollout은 추가 검증 필요
- 최소 하나의 실제 Kubernetes cluster acceptance 및 CNI/PVC 범위 기록
- 실제 DB benchmark 반복/비교가 장비·설정·데이터가 맞는 경우에만 수행됨
- 운영 service/database에는 기본 read-only, secret 원문 및 업무 payload는 공유 report에서 제외
- 모든 PASS가 실제 새 실행의 identity, 결과, postcondition을 가리킴; 미실행·mock·부분 검증은 blocked/not_run/test_only로 유지
- 문서/help, executable quality checks, runtime acceptance 결과가 같은 지원 범위를 말함

## 지금 착수할 우선순위

1. **가장 먼저:** 공통 benchmark adapter가 연결된 Redis/MariaDB/Kafka/ES7에서 동일 조건 반복 측정을 하고 산포·데이터 상태·실행 중 host load를 같이 남긴다. ES9의 시끄러운 3회 결과만으로 성능 비교를 승인하지 않는다.
2. **두 번째:** disposable 별도 Linux SSH 대상에서 source 배포 후 DB `init/doctor/up/verify/collect`, 실패 중단과 재개 경계를 검증한다.
3. **세 번째:** disposable Kubernetes에서 MariaDB 복구/보존 경로를 acceptance하고 Redis acceptance와 같은 수준으로 CNI/PVC/정리 경계를 기록한다.
4. **네 번째:** 확인된 command/runtime capability와 executable gates를 문서 및 benchmark report 수준과 동기화한다.
5. **마지막:** 여러 호스트/cluster orchestration, 자동 capacity recommendation, 공통 dashboard는 실제 반복 사용 사례와 데이터가 생긴 뒤 결정한다.

실제 현장성은 기능 개수보다 재실행 가능성, 데이터 보존, 대상 식별, 실패 전달, 증거의 정직성, 실행 가능한 복구 절차로 판단한다.

## 진행 기록: 2026-09-24

- Ansible target allowlist에 opt-in `elasticsearch9`를 추가했다. 기본 `all`은 기존처럼 네 개 랩만 포함한다. policy, 실제 module argument spec, 경로 검증, env fingerprint, 사용자 문서를 함께 갱신했다.
- `./all.sh --dry-run up es9`에서 ES9 plan만 생성했다. 고정 `ansible-core==2.21.4`를 임시 virtualenv에 설치해 실제 Ansible playbook fixture 10개와 ES9 `health`의 `--check` 계획 경로를 통과했다. `tests/test_ansible_control.py` 87개도 통과했다. SSH 대상·DB·HA 실기동은 증명하지 않는다.
- `scripts/test-helm.sh`는 Helm 4.1.1에서 7개 프로필 lint/render, beta 별도 release contract, 6개 unsafe value 거부를 통과했다. Helm 4에서 제거된 `helm list --all` 인수는 Helm 3 help를 확인해 조건부로 넣도록 고쳤다.
- 기존 업무 context `k3d-board-msa`를 건드리지 않고 전용 kind v1.31.2 단일 노드 클러스터에서 Redis+Sentinel 설치, 6 PVC, CNI NetworkPolicy, 복제 ACK, PVC 유지 재생성, primary 장애 전환과 복귀를 실제 확인했다. 상세 환경과 명시적 제외 범위는 [런타임 수용 결과](RUNTIME-ACCEPTANCE.md)에 기록했다.
- MariaDB Helm 이미지 경로가 pull 불가하고 init Secret이 MariaDB 32자 제한보다 길게 생성되는 문제를 수정했다. 추가 실기동에서 `runAsGroup: 1001`이 이미지의 설정 파일 수정을 막는 점도 재현해 제거했다. 이제 Pod 준비 검사는 configured replica 수와 실제 `wsrep_cluster_size` 일치를 요구한다. node 0은 Primary/ready에 도달했지만 다른 두 노드가 primary view에 합류하지 않아 acceptance는 계속 미통과다. 시험 클러스터/PVC는 삭제했다. 자세한 한계는 [런타임 수용 결과](RUNTIME-ACCEPTANCE.md)에 기록했다.

공통 benchmark report/compare와 반복 분산 계산, Ansible opt-in bootstrap/artifact 배치, 격리 Redis Helm acceptance가 구현·검증됐다. 남은 핵심은 나머지 DB의 실제 반복 실행/산포 근거, 별도 Linux 대상의 Ansible lifecycle acceptance, MariaDB Kubernetes 복구 acceptance다.

### 공통 benchmark 실행 명령: 2026-09-24

- `./all.sh benchmark <es7|es9|mariadb|kafka|redis>`가 공통 adapter를 통해 각 lab의 기존 bounded workload를 호출하고 일반 모드에서는 root run receipt를 남긴다. `--dry-run`은 실제 하위 스크립트나 DB를 실행하지 않고 명령만 표시한다. DB별로 무관한 rate/batch/worker/key/payload 인수는 거부하며 선택 lab을 자동 기동·초기화·종료하지 않는다.
- `--repeat 3..10`은 각 실행 뒤 새 `live_database` PASS report를 확인하고, 세 번 이상 성공한 경우에만 공통 비교 JSON을 저장한다. Redis는 기존 ops volume을 `ops results`로 내보내고 최신 run ID가 바뀌었는지 확인한다. workload는 30초 미만 반복을 거부하며, readiness gate가 미통과면 통계와 repo-relative report paths를 보존하되 종료 코드 2를 반환한다. Benchmark/comparator/environment/ES fixtures 21개, root `all.sh` 80개, dry-run 계획 및 help의 receipt 비생성 검증이 통과했다. 실제 repeat benchmark는 host 부하 때문에 실행하지 않았다.
- 실제 저장된 ES9 동일 workload 보고서 5개를 공통 comparator로 처리했다. p95 batch latency CV 49.89%, 불완전한 resource metadata, full-run contention 미계측 때문에 결과는 기술 통계로만 유지했다. comparator가 모든 repeat의 missing field를 모으도록 고치고 회귀 검증했다.
- `tests/test_benchmarks.py`의 dispatch/route/인수/비교기 7개 테스트와 `tests/test_all.py`의 root routing/receipt/dry-run 57개 테스트가 통과했다. 현재 host snapshot은 load average 9.92/12.94/23.13, swap full이므로 이번 turn에는 새 DB benchmark를 실행하지 않았다.

### Redis benchmark comparison slice: 2026-09-24

- `redis-lab/ops.sh compare <baseline/report.json> <candidate/report.json>`가 exported load report 두 개를 읽어 성공/오류 수, 성공 RPS, RTT 분포와 차이를 보여준다. 두 실행의 workload 인수와 관측 Redis 버전이 모두 같아야 comparable이며, 실패 run이나 누락 버전은 거부한다.
- 비교기는 호스트, container limit, 데이터 상태, 경쟁 부하를 측정/검증하지 않는다. 같은 머신과 설정을 사용자가 맞춰야 하며 결과를 capacity/SLO 판정으로 사용하지 않도록 명령 도움말과 문서에 기록했다.
- `redis-lab/tests/check.sh`에서 212개 테스트 통과, root static 검사 통과. 실제 Redis benchmark 실행과 비교는 이번 검증에서 수행하지 않았다.
- 이건 Redis 전용 comparison slice다. MariaDB/Kafka/Elasticsearch paired comparison, 공통 runner, 반복 분산 계산은 여전히 미구현이다. Ansible opt-in host deployment와 disposable SSH validation 역시 미완료다.

### MariaDB benchmark report slice: 2026-09-24

- `./lab.sh load` now emits a machine-readable native summary and saves a versioned JSON report under `mariadb-ha-lab/reports/benchmarks/` with live MariaDB version, runtime/version, revision, workload parameters, duration, transaction/latency counters, and invariant result. Secrets are excluded. Missing DB version yields `UNVERIFIED`; failed workload yields `FAIL` while retaining the report.
- `bash mariadb-ha-lab/tests/validate.sh` passed 203 host-only checks, including a fake-engine regression covering the saved schema and secret exclusion. No MariaDB engine was started by validation.
- Redis has same-workload comparison; MariaDB has normalized run capture but no paired-comparison command. Kafka now saves normalized producer delivery reports. Elasticsearch comparison adapters and variance-aware repeats remain open.
- Disposable Kubernetes acceptance was subsequently completed; see 2026-09-24 entry in `docs/RUNTIME-ACCEPTANCE.md`. The dedicated cluster and temporary kubeconfig were removed after evidence collection; existing `k3d-board-msa` and the pre-existing Docker `kind` network were left untouched.

### Kafka benchmark report slice: 2026-09-24

- Kafka bounded producer reports are stored under `kafka-lab/reports/benchmarks/` with producer counters, workload settings, configured Kafka image/mode/broker count, runtime and client library version, revision, and delivery verification. The raw payload and credentials are not included.
- The first full static run exposed a wrapper fixture missing the report helper and local producer tests depending on an optional installed Kafka client just to read its version. Both were corrected: the fixture now includes the helper and version lookup uses Python package metadata.
- `bash kafka-lab/scripts/test-static.sh` passed 111 tests. No Kafka broker benchmark was run. Redis remains the only paired comparison; MariaDB/Kafka/Elasticsearch comparison, a single runner, and variance-aware repeats remain open.
- Root quality static passed; Helm 4.1.1 lint/render passed; installed Ansible 2.18.18rc1 ran 10 local harmless fixture tests. That does not replace the earlier pinned Ansible 2.21.4 evidence and does not prove SSH deployment.

### 공통 benchmark v1 및 ES9 실기동: 2026-09-24

- `scripts/benchmarks.py`는 MariaDB/Kafka/Redis/Elasticsearch의 `schema_version: 1` live run 보고서를 입력받아 database/workload/runtime 일치를 확인하고 공통 numeric metric의 median, mean, sample stdev, CV를 계산한다. 중복 run ID/파일, 실패 run, 다른 설정을 거부한다. host fingerprint는 원문 이름을 저장하지 않고 hash로 기록하며 CPU/RAM과 Compose 컨테이너 limit을 읽는다.
- MariaDB/Kafka/Redis/ES 보고서 저장 흐름에 v1 메타데이터를 연결했다. Redis는 상세 report를 유지하면서 export에 `benchmark.json`을 추가한다. ES7/ES9는 공유 실행기를 쓰며 고정 seed/시간/속도의 bounded bulk benchmark를 전용 index에서 수행, refresh/count 검증 뒤 삭제한다. 충돌하는 임시 index가 이미 있으면 덮어쓰지 않고 중단한다.
- comparator는 configuration/resource 일치와 반복 산포를 표시하지만 `performance_comparison_ready: false`를 유지한다. 전체 구간 CPU contention, concurrent load, dataset condition은 자동 수집하지 않는다.
- ES9 9.5.3의 저장된 동일 설정 report 5개를 이번 공통 comparator로 확인했다(각 5초, 5 docs/s, 25 docs/run). 모두 bulk ack/count/cleanup PASS였고 p95 batch latency CV는 49.89%였다. 두 report에는 host/container metadata가 없어 `environment_confirmed=false`; `performance_comparison_ready=false`를 유지한다. 현재 읽기 전용 status는 green/285 shard copies STARTED였지만 host load average 9.92/12.94/23.13, node CPU 49%, RAM 92%, swap full을 보여 이번 turn에 새 load를 실행하지 않았다. 이전 실행 시점 상태와 현재 스냅샷은 구분한다. 상세 증거는 `docs/RUNTIME-ACCEPTANCE.md` 참조.
- ES7 live run, Kafka/MariaDB/Redis real benchmark, contention-isolated baseline 반복, Ansible opt-in host preparation/artifact deployment와 별도 원격 SSH target 검증이 남아 있다.

### Ansible loopback SSH transport: 2026-09-24

- `bash scripts/test-ansible-ssh.sh`가 임시 loopback `sshd`, 일회용 client/host key 및 정확한 `known_hosts`를 사용해 실제 SSH 인증, Ansible 모듈 전송, 원격 fixture의 native controller 호출을 통과했다.
- `scripts/quality.py offline`은 1,704개 호스트 테스트, `scripts/quality.py tools`는 실제 Ansible fixture 10건과 Helm rendering을 통과했다. 모두 `live_db_acceptance_executed=false`다.
- 이 검사는 별도 원격 Linux 호스트, OS/runtime 준비, DB 기동/복구를 증명하지 않는다.

### Ansible versioned artifact deployment: 2026-09-24

- `ansible/deploy.yml`은 controller의 Git worktree에서 비밀·런타임 파일을 뺀 결정적 archive를 만들고, SSH 대상 계정 홈 바로 아래의 새 SHA-256 release에 전달·검증·원자 게시한다. 기존 경로는 덮어쓰지 않으며 같은 release 재호출은 marker를 검증하고 재사용한다. `--check`는 archive나 target path를 만들지 않는다.
- `ansible/package_artifact.py`는 Git 추적 파일과 Git이 무시하지 않은 파일만 포함하고 `.env`, 인증서/키, 보고서, 상태/데이터/volume/backup 경로와 symlink를 제외/거부한다. 대상 `.env`, 런타임 설치, DB `up`, 자동 rollback은 처리하지 않는다.
- 실제 Ansible SSH fixture에서 `--check` 무변경, 첫 배포, 동일 release 재실행(`changed=0`), private `.env`/reports 제외, 배포된 `all.sh status` 호출까지 통과했다. 이는 현재 호스트의 loopback sshd 수용 시험이며 별도 원격 Linux/DB 인수는 아니다.
- 남은 Ansible 범위는 disposable 원격 Linux target에서 실제 controller/runtime 준비와 DB `init/doctor/up/verify/collect`, 실패 재개 경계를 확인하는 것이다.
- `ansible/bootstrap.yml`은 Ubuntu 24.04의 Docker/Podman, Debian 12의 Podman만 대상으로 OS 저장소 package 설치를 opt-in한다. 단일 호스트, `INSTALL:<engine>`, apt task만 become, check mode에서는 package task 미실행; 별도 저장소/그룹/firewall/패키지 제거/업그레이드는 하지 않는다.

### Ansible bootstrap package planning: 2026-09-24

- Bootstrap playbook의 실제 Ansible syntax/consent/check-mode/package-plan 12개 테스트와 artifact 패키징 2개 테스트를 통과했다. Root static quality도 통과했다. 모두 package 설치를 실행하지 않는 테스트다.
- `scripts/test-ansible-bootstrap-container.sh`가 별도 Podman VFS 저장소의 Debian 12 SSH 컨테이너에서 실제 apt bootstrap과 두 번째 실행(`changed=0`)을 통과했다. DB는 시작하지 않았다. 대상은 rootless Podman user namespace 내 root SSH였으므로 비루트 SSH 계정에서 become/sudo하는 경로와 별도 원격 서버는 아직 미검증이다.
- 초기 Docker 데몬 정체 및 공유 Podman 저장소 경합을 확인한 뒤 acceptance 러너를 개별 VFS 저장소·임시 authfile로 격리하고 모든 런타임 호출에 deadline을 두었다. 이전 시도의 정확한 임시 컨테이너와 새로 받은 이미지는 확인 후 삭제했다. 기존 서비스는 중지하거나 재시작하지 않았다.

### Non-root Ansible bootstrap acceptance path: 2026-09-24

- Expanded `scripts/test-ansible-bootstrap-container.sh` so the disposable Debian 12 target accepts SSH only as unprivileged `labops`; a container-local sudo rule enables the playbook's task-scoped apt become. SSH host-key checking remains enabled, and the test verifies sudo identity before applying the playbook twice.
- Added `ansible_become_method=sudo` to the real Ansible check-mode fixture while retaining the playbook guard against global `ansible_become`. The real Ansible playbook fixture suite passed after this change; shell syntax and diff checks passed.
- The isolated Debian 12 SSH acceptance passed with rootful Docker: SSH as non-root `labops`, task-scoped sudo become, real apt package installation, engine/tool verification, and a second run with `changed=0`. No DB service was started. The disposable container was removed; existing services were left running.
- A rootless Podman attempt was blocked because its container filesystem is mounted `nosuid`, so sudo cannot elevate there. The runner now supports `DB_LAB_BOOTSTRAP_TEST_RUNTIME=docker|podman|auto`. The first Docker attempt exposed Debian's locked-by-default test account and an `iptables` PATH lookup issue; the harness now initializes only its ephemeral account for key-only SSH, and `bootstrap.yml` verifies `/usr/sbin/iptables` directly.
- `ansible/tests/test_playbooks_live.py` passed all 12 cases after the absolute-path correction. This is disposable localhost Docker acceptance, not a separate remote Linux host or database deployment/recovery proof. Host swap remains full; no benchmark load or Kubernetes cluster was started.

### Helm MariaDB peer-gate acceptance attempt: 2026-09-24

- Added `scripts/test-helm-mariadb-runtime.sh`, which builds a unique 4-node kind cluster with private kubeconfig and namespace, installs the MariaDB-only profile, checks 3-node Galera state and replicated synthetic data, then recreates one Pod and verifies PVC identity/data. Failure collection is limited to the disposable namespace; cleanup targets only the generated kind cluster.
- Fresh runtime attempt reached node 0 `Primary/Synced` with `wsrep_cluster_size=1`. Joiners completed the new SQL peer gate but MariaDB subsequently logged `failed to reach primary view`; all-three readiness and the run did not pass. A same-release probe connected to seed TCP ports 3306/4567, so this run does not establish general network/firewall blockage. No synthetic writes or PVC rejoin ran.
- Interrupted the still-waiting acceptance after obtaining pod/event/log diagnostics and removed only `db-lab-maria-accept-1632257` and its temporary kubeconfig directory. `kind get clusters` is empty. Runtime root cause remains open; don't count the chart as MariaDB runtime-accepted.
- Improved the acceptance timeout override and failure diagnostics to include pod descriptions plus previous/current container logs. A second 5-minute isolated run confirmed distinct peer DNS addresses and successful TCP connections, then reproduced Galera timeout. Testing a single bootstrap FQDN as the join seed and deleting the policy in this disposable namespace did not restore the cluster; a socat probe separately confirmed UDP payload delivery between Pods. The seed-list change was reverted because it did not fix the observed failure. The generated cluster/kubeconfig were cleaned up by the runner; data replication and PVC rejoin remain untested.
- The same run reported NetworkPolicy `NotFound` because the policy had been deleted for the controlled diagnostic; all changes and data were confined to the disposable cluster. Acceptance remains failed, and the root cause is not yet identified.
- Latest isolated retry used a fresh kind v1.31.2 four-node cluster and private kubeconfig. Helm installed and all three 5Gi PVCs bound, but StatefulSet stayed `Ready: 0/3`: node 0 remained `Primary/Synced` at cluster_size 1 while joiners restarted after failing to reach a Primary view. Peer DNS resolved and the joiner init gate reached seed TCP 3306. kindnet does not enforce NetworkPolicy, so this attempt does not identify a firewall/policy cause. No replication writes or PVC rejoin were attempted. Runner cleanup removed the generated cluster and kubeconfig; `kind get clusters` was empty afterward.

### Benchmark image identity and Redis host metadata: 2026-09-24

- Extended the shared benchmark environment collector to record immutable running image IDs and available registry RepoDigests beside CPU/memory limits. Partial inspection yields explicit missing metadata; the comparator now requires engine/version, host identity/resources, limits and image IDs, and reports image or runtime differences across repeats.
- Redis host orchestration now passes the host-collected environment to its isolated workload runner over argv-based `podman exec --env`; the saved Redis `benchmark.json` carries the data-plane Redis nodes and workload containers without mounting the engine socket or exposing credentials.
- Focused tests passed: shared environment collector 3, comparator/runner 8, Redis operations 58. These are source/fixture checks; no fresh live DB reports were generated because host load is elevated and swap remains full. Existing reports lack the new image field and must be regenerated before they can confirm an environment.

### 설치·검증 게이트 재실행: 2026-09-24

- `bash scripts/test-offline.sh`가 전체 offline suite 868건을 통과했다. DB/Compose 테스트더블과 host-only 검사이며 실제 설치·DB 기동 증거로 계산하지 않는다. MariaDB HA lab은 별도 host-only 203건 PASS였고 disruptive run은 실행하지 않았다.
- `bash scripts/test-helm.sh`가 Helm 4.1.1에서 모든 shipped profile 및 beta release lint/render와 6개 unsafe value 거부를 통과했다. 범위는 manifest contract다.
- `python3 scripts/quality.py static`, `git diff --check`, `bash scripts/test-ansible-ssh.sh`가 통과했다. SSH 시험은 loopback disposable sshd에서 artifact 배포/재실행과 fixture 제어 호출까지 확인했으며 외부 Linux 대상의 DB lifecycle은 미검증이다.
- 실제 DB benchmark와 MariaDB Galera multi-node acceptance는 이번에 실행/통과하지 않았다. 기존 ES9/MVP 및 다른 프로젝트 컨테이너가 가동 중이고 swap이 가득 차 있어 경쟁 부하를 추가하지 않았다.

### Benchmark host pressure metadata: 2026-09-24

- 공통 환경 수집기가 host MemAvailable/SwapTotal/SwapFree를 기록하고 comparator가 반복 실행 간 해당 값을 필수 자원 메타데이터로 비교하도록 했다. swap이 0인 환경도 유효 값으로 인식한다.
- benchmark report가 저장되는 시점의 host 상태 스냅샷이다. 실행 전체 contention 추적이나 데이터셋 상태 검증은 아니며 기존 보고서는 새 필드가 없어 재생성 전까지 환경 비교 미확정으로 남는다.
- comparator에서는 load/available memory/swap-free를 고정 host 사양과 분리해 run별 압력 관측치로 나열한다. 관측치가 달라도 CPU/RAM 사양이 다르다고 오판하지 않는다.
- 반복 보고서 comparator는 PASS 외에 true인 boolean postcondition, 고정 환경, full-window runtime observation, 검증된 dataset state, 최소 3회 반복을 확인한다.
- `tests.test_benchmark_environment`와 `tests.test_benchmarks` 11개, `python3 scripts/quality.py static`, `git diff --check` 통과. 실제 DB 부하는 실행하지 않았다.
- MariaDB Helm acceptance 실패 진단에 allowlist 방식 wsrep 변수/상태 수집을 추가했다. Galera 변수 전체를 로그에 남기지 않아 SST 인증 정보를 피하면서 다음 격리 재현에서 peer 설정과 실제 cluster view를 함께 확인한다. `bash -n` 및 static quality 통과; 새 runtime acceptance는 실행하지 않았다.
- Benchmark host CPU utilization now sums only Linux `/proc/stat` fields through `steal`; `guest` and `guest_nice` are already included in `user`/`nice` and are excluded to prevent double counting. The focused environment suite passed all 6 tests. No live benchmark was started: host swap remains full and other active workloads are present.

### Ansible Debian target bootstrap re-acceptance: 2026-09-24

- 현재 코드로 `DB_LAB_BOOTSTRAP_TEST_RUNTIME=docker bash scripts/test-ansible-bootstrap-container.sh`를 재실행했다. 별도 Debian 12 컨테이너의 비루트 SSH 사용자, pinned host key, task-scoped sudo become, 실제 apt bootstrap 및 재실행 `changed=0`이 통과했다. disposable container는 종료 뒤 제거됐고 kind cluster는 없는 상태를 확인했다.
- 이는 다른 Linux 컨테이너에서 SSH/package preparation을 입증한다. container runtime의 Docker/Podman 준비와 DB `init/doctor/up/verify/collect`, 외부 원격 서버, 장애 재개 경계는 아직 검증하지 않았다.

### Elasticsearch 9 bounded live benchmark: 2026-09-24

- 기존 ES9 lab에 5초/5 docs/s/10-doc batch bounded run을 수행했다. run `7cdfa6481ddf446a`, ES 9.5.3, 25/25 bulk ack, refresh 후 count 일치, 임시 index 삭제가 모두 PASS다. report: `elasticsearch-9/reports/benchmarks/7cdfa6481ddf446a.json` (local ignored runtime evidence).
- 사후 `lab.sh status`에서 5 nodes/285 shard copies, zero unassigned, green을 확인했다. Run 중 host load 1m 약 9.1, swap free 약 20 MB여서 이 결과는 smoke/load-path evidence이며 성능 비교·capacity 추정에 사용할 수 없다. 반복 실측과 실행 전체 contention 표본화는 남아 있다.
- 새 run은 과거 5회와 batch size가 달라 comparator가 비교를 정확히 거부했다. 과거 동일 batch 5회만 재비교한 결과 p95 latency CV 49.89%; 누락/불일치 host/container metadata 및 full-run contention 때문에 `environment_confirmed=false`, `performance_comparison_ready=false` 그대로다.
- 이후 ES9에서 batch 5 동일 조건의 새 live run 3개(`e381081bd1024f4f`, `fa213d738bab420d`, `f4bc7bfc6eff40ab`)가 각각 25/25 ack·count 검증·임시 index cleanup PASS했다. 세 run 비교는 `configuration_comparable=true`, `environment_confirmed=true`; p95 batch latency 중앙값 6.08ms, CV 6.27%였다. run별 load 1m은 6.63→8.60, swap 여유는 약 20MiB여서 성능 승인/용량 추정으로 해석하지 않는다. 비교기는 full-run contention/dataset 관측 누락을 계속 blocker로 내고 `performance_comparison_ready=false`를 유지한다. 사후 cluster는 5 nodes/285 shard copies green, unassigned 0이었다.

### ES9 full-window host pressure sampling: 2026-09-24

- `lib/db_lab_benchmark.py`가 host `/proc/stat` cumulative CPU ticks를 약 1초 간격으로 workload loop 동안 표본화하고, load, MemAvailable, swap-free도 기록한다. ES9 report에는 평균/peak CPU busy, interval coverage, minimum available memory/swap, dataset state verification을 저장한다.
- 새 5초/5 docs/s/25-document runs `0057a35ebb4c4e23`, `757cfe1d66f7446a`, `f68748cec9864382`에서 각각 25/25 ack, count, cleanup, dataset postcondition이 PASS했다. 관측 구간은 5.04~5.05초였고 비교기에서 환경, pressure observation, dataset 증거 모두 확인됐다. `performance_comparison_ready=false`가 된 이유는 각 run이 smoke 최소 5초여서 30초 비교 기준을 못 채운 점이다. 이는 full-window observation 경로를 실제 ES에서 검증한 것이며 성능/capacity 승인 근거는 아니다. 사후 ES9는 5노드/285 shard copies green, 미할당 0.
- ES helper/comparator unit tests와 static quality 통과. 당시 MariaDB adapter는 원본 계정 데이터 변경 및 transfer history 누적으로 `dataset_state_verified=false`를 유지했다. 이후 아래 2026-09-24 변경은 해당 데이터 격리를 소스/호스트 테스트에서 확인했으며, 실제 MariaDB Galera 환경의 benchmark 실행과 report 비교는 아직 검증하지 않았다. Redis/Kafka adapter는 host sampler를 연결했다. Kafka는 아래 run-scoped topic 데이터 검증을 구현했다.

### MariaDB benchmark observation: 2026-09-24

- 공통 sampler가 blocking workload 실행 중 1초 간격으로 load/CPU/memory/swap을 관측할 수 있도록 start/stop을 추가했다. MariaDB load report는 해당 관측값을 기록한다.
- MariaDB report regression은 호스트 관측 필드와 명시적 dataset blocker, credential 비노출을 확인한다. `tests.test_benchmark_environment`, `tests.test_benchmarks`, `tests.test_es_benchmark` 16개 및 `mariadb-ha-lab/tests/validate.sh` 203개 host-only checks 통과. 실제 MariaDB benchmark는 실행하지 않았다.

### MariaDB benchmark dataset isolation: 2026-09-24

- 발견한 결함: 기존 `load`는 `lab_ops.account` 잔액을 직접 바꾸고 `transfer` 이력을 누적해 반복 run의 시작 데이터가 달라졌다.
- Controller가 원본 계정 row를 정렬해 SHA-256 fingerprint를 만들고 run별 이름으로 계정·송금 테이블을 복제한다. Workload SQL은 생성된 이름 패턴만 허용하고 해당 사본에서 실행한다. 종료 시 복사본 fingerprint와 임시 테이블 부재를 확인하며, fingerprint는 workload 비교 키에도 들어간다. cleanup/invariant 검증이 안 되면 dataset 증거를 false로 두고 report와 명령 모두 실패한다.
- `mariadb-ha-lab/tests/test_regressions.py` 44개와 `mariadb-ha-lab/tests/validate.sh` 통과. 실제 MariaDB 부하/DDL 및 Galera 복제는 현재 가동 환경의 자원 압박과 데이터 보호 때문에 이번 수정에서 실행하지 않았다. 런타임 확인 전까지 이 경로는 source/host-test 검증으로만 취급한다.

### Redis benchmark runtime evidence: 2026-09-24

- Redis host controller가 workload subprocess가 실행되는 동안 공통 host sampler를 구동하고, 완료된 run의 `report.json`/`benchmark.json`에 관측값을 반영한다. 보고서는 컨테이너 내부 측정에 의존하지 않는다.
- key prefix는 run별 고유값이고, 시드한 key 개수와 cleanup 개수가 일치할 때만 `dataset_state_verified=true`로 기록한다. cleanup 누락·부분 정리는 공통 comparator의 verification gate를 막는다.
- `bash redis-lab/tests/check.sh`의 218개 테스트와 benchmark/common 테스트 16개 통과. 현재 Redis lab이 실행 중이 아니므로 이번 변경에서 실 DB 부하는 실행하지 않았다.

### Kafka benchmark runtime evidence: 2026-09-24

- 기존 데이터 적재 명령 `./lab.sh seed`의 persistent topic 동작은 보존한다. 신규 `./lab.sh benchmark`와 공통 runner Kafka 경로가 workload subprocess 중 host pressure를 기록한다. 공통 벤치마크는 새 `lab.benchmark.<run-id>` topic을 만들고 workload 종류에 맞는 partition 수/복제 상태를 기다린 뒤 빈 offset을 확인한다.
- produce 완료 후 queued/delivered counters와 새 topic의 총 offset span이 일치하는지 검증하고 해당 topic을 삭제한다. 위 postcondition이 모두 참일 때만 report의 `dataset_state_verified=true`; 일반 persistent topic은 saver가 격리 완료를 인정하지 않는다. 임시 topic에는 장애 시를 위한 1시간 retention도 설정한다.
- Kafka 단위·shell fixture·report 테스트 통과. 현재 Kafka lab은 실행 중이 아니므로 실제 broker topic 생성/삭제와 run 반복 비교는 아직 검증하지 않았다.

### Ansible bounded benchmark control: 2026-09-24

- `control.yml`의 기존 단일-lab policy에 `benchmark` action을 연결했다. ES7/ES9, Kafka, MariaDB, Redis만 선택할 수 있고 `all`, MVP, Kubernetes는 거부한다. 각 DB별 허용 옵션과 공통 runner의 경계를 검사하며 arbitrary flags/명령 실행을 열지 않는다.
- 부하가 데이터를 쓰므로 `db_lab_allow_changes: true`가 필요하고 Ansible `--check`는 argv 계획만 만든다. 실제 실행은 root `all.sh benchmark` 경로를 써 기존 공통 report/비교 계약을 재사용한다.
- `tests.test_ansible_control` 92개와 실제 `ansible-playbook` fixture 13개 통과. disposable local fixture에서 동의 전 실행 거부, check 무호출, Kafka args forwarding을 확인했다. 별도 SSH/Linux target에서 실제 broker 부하는 수행하지 않았다.
- `./all.sh --dry-run benchmark kafka --seconds 30 --rate 1000 --payload 256`가 공통 `scripts/benchmarks.py run kafka` argv를 출력하고 DB를 시작하지 않음을 확인했다.

### Debian 12 Ansible install/runtime boundary: 2026-09-24

- Debian 12 bootstrap now has explicit consent for the official Bookworm Backports source and installs `podman-compose 1.0.6`; repeat bootstrap and source-only deploy/reuse remain idempotent. That version accepts the Redis static network map and both Redis lab images built.
- The live Debian 12 container acceptance now passes non-root SSH, pinned host key, task-scoped sudo, apt bootstrap/repeat (`changed=0`), source-only deploy/check/reuse, and Redis `init/doctor/up/health/status/down`. It also verified that `down` preserved the generated `.env` and named Redis data volume before the disposable target was removed.
- Two provider/runtime incompatibilities were corrected: health argv now works with Debian's Python argparse, and Redis sandbox/up/down no longer pass podman-compose 1.0.6's unsupported `--profile` option. Failed-action diagnostics fetch the matching action's private logs and redact passwords.
- To make nested Podman usable, the fixture disables child cgroups and inherits the disposable outer cgroup. The test proves lifecycle behavior but not resource-limit enforcement and is not acceptance on a Debian VM or external host. Run that separately before extending the support claim.
- Bootstrap fixture tests (14), Redis provider tests (2), two focused argv/provider regressions, shell syntax, and whitespace checks passed. Existing host services were left running.

## 다음 수행 항목

이 목록은 구현/fixture 검증과 실제 런타임 acceptance를 구분한다. 아래 runtime 항목이 증거를 남기기 전까지 이 로드맵을 완료로 표시하지 않는다.

1. **DB별 반복 벤치마크 실측:** 이미 구현된 `./all.sh benchmark <db> --seconds 30 --repeat 3`을 준비된 격리 lab에서 실행한다. ES7, Redis, MariaDB, Kafka와 ES9 각각 새 `live_database` report 세 개, 동일 workload/image/resource 조건, dataset postcondition 및 full-window host observation을 확인한다. 비교 JSON의 `performance_comparison_ready`와 blockers를 검토하고, host 부하가 비교를 무효화하면 결과를 성능 기준선으로 승격하지 않는다. 현재까지 반복 runner는 fixture/dry-run으로만 확인했다.
2. **별도 원격 Linux의 Ansible 인수:** disposable VM 또는 사용자가 소유한 격리 Linux host를 대상으로 실제 SSH Ansible을 실행한다. source artifact 배포, 승인된 prerequisite, `init → doctor → up → verify → collect`, 두 번째 멱등 실행, 실패 시 중단과 명시적 재개, report/secret 경계를 확인한다. Debian 12 컨테이너 acceptance와 loopback SSH fixture를 외부 host 증거로 대체하지 않는다.
3. **MariaDB Helm Galera acceptance:** joiner의 `failed to reach primary view` 원인을 추가 계측으로 특정하고, 새 disposable kind에서 3노드 `Primary/Synced`·cluster size 3, 합성 row 복제, Pod 재생성 후 동일 PVC/data 재합류를 모두 확인한다. 이후 계획된 full-cluster recovery/보존 절차도 격리 PVC에서 별도 확인한다. 기존 시도들은 모두 실패했으며 현재 원인은 미확인 상태다.
4. **최종 검토와 publication:** 위 runtime 결과를 `docs/RUNTIME-ACCEPTANCE.md`, 이 로드맵, 해당 lab/Ansible 사용 문서와 대조한다. 현재 dirty worktree의 기존 변경과 이번 로드맵 변경을 먼저 구분하고, 논리 단위별 검토·커밋·푸시를 수행한다. 현재 문서화 시점에는 commit/push하지 않았다.

다음 재개 시 먼저 host/context/resource 상태와 선택한 disposable target의 소유권을 확인한다. 실행 증거가 준비되지 않았으면 항목을 미완료로 남기고 기존 데이터를 대상으로 시험하지 않는다.
