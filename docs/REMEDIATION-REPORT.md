# DB Lab 개선 작업 보고서

기준일: 2026-09-18. 입력: `db-lab-main-with-all-sh.zip` 및 앞선 F01~F16 진단 리포트. 수정 대상은 별도 작업 사본이며 사용자 호스트/클러스터/원본 ZIP을 직접 변경하지 않았습니다.

## 결과 요약

안전한 기본값, Kafka 실제 실행 산출물, MariaDB 부하 제어, 루트 운영 보호 장치, Helm 복제/저장/격리 템플릿을 수정했습니다. **코드 수정 및 오프라인 검증 완료와 실제 DB/HA 검증 완료는 다릅니다.** 특히 새 Helm 0.2.0은 실기동 검증 대기 상태이며 기존 0.1.x의 강제 in-place 교체를 지원하지 않습니다.

기존 `all.sh` 단독 배포 방식과 달리 이제 루트 `scripts/`가 필수입니다. 전체 수정 ZIP을 사용하고 기존 `.env`/볼륨은 별도 보존하세요. 이번 작업은 패키지 설치, 서비스 기동, 데이터 이동, 기존 암호 회전, 호스트 방화벽 변경을 수행하지 않았습니다.

## 실제 적용한 변경

### 안전 기본값과 Kafka

Elasticsearch 기본/Compose fallback 주소를 127.0.0.1로 바꾸고, 기존 `.env`에 원격 주소가 있어도 `ES_ALLOW_PUBLIC_BIND=yes`가 없으면 시작 전에 거부합니다. 원격 공개 시 주소/포트/인증 미사용 경고를 먼저 출력합니다. 일반 시작의 자동 sudo/iptables 변경을 제거했습니다. 커널 설정은 검사만 하고 수동 안내를 출력합니다. 실제 인터넷 노출 여부를 이번에 측정한 것은 아닙니다.

Kafka renderer에 포트/heap을 전달하고 tools build context를 실제 client 절대 경로로 생성합니다. 파일을 원자적으로 기록하고 포트 중복·범위, 노드 수, 모드 조합을 검증합니다. KRaft ID는 16바이트 URL-safe Base64 22자 표현으로 생성·검증하며, 기존 잘못된 ID, 공급한 값과 저장 값 불일치, 기존 볼륨만 있는데 ID가 없는 경우 임의 재생성하지 않습니다. `.env`는 실행 가능한 셸이 아니라 데이터로 읽습니다. 두 자리 이상 노드 관리와 KRaft/zk 상태 분기를 수정했습니다.

### 부하 생성기와 건강 상태

MariaDB simulator는 monotonic 기준의 건별 스케줄을 사용합니다. 목표 시각을 놓치면 빠진 슬롯을 건너뛰고 catch-up burst를 만들지 않습니다. 새 요청 시작은 기간 경계 안에서만 허용하지만 이미 진행 중인 DB 접속/SQL은 타임아웃까지 이어질 수 있으며 종료 초과 시간을 따로 기록합니다.

시도/성공/실패, 실제 경과/발행률, 성공 SQL/실패 시도/전체 시도/재접속 지연, 대기 시간을 분리했습니다. 저장한 API latency는 합성값임을 출력/데이터 metadata에 표시합니다. 주문 상태는 여전히 무작위 갱신으로 업무 정합성 모델이 아닙니다. 실행 ID와 이벤트 ID가 추가되었고 seed는 합성값 선택을 제어하지만 UUID·실제 서버 시각까지 완전히 재현하지는 않습니다.

루트 `health --json`은 단순 `status` 종료와 구분됩니다. ES는 예상 cluster 이름/green/노드 수, MariaDB는 Primary/Synced/멤버 수/UUID, Redis는 읽기 전용 topology, Kafka는 기존 native health로 판정합니다. 이는 쓰기 내구성/복구 검사도, 두 시점 사이의 무중단 보장도 아닙니다. DB 조회는 읽기 전용이지만 일부 하위 컨트롤러가 로컬 `.state`/생성 설정을 갱신할 수 있습니다.

### 루트 제어

공통 up/restart 전에 엔진/provider, 설정, 계획 포트 중복, 일부 커널/자원 상태를 점검합니다. 점유된 포트는 현재 실행 중인 정상 실습일 수 있어 경고만 하고 소유권을 단정하지 않습니다. 자원 수치는 관측 스냅샷이며 전체 실습 용량 충족을 인증하지 않습니다.

변경 작업에 `flock`을 적용하고 `reports/all-*.json`에 실행 ID, 명령 종료 코드, 단계별 결과, 시작/종료 시각, controller hash를 기록합니다. 환경/암호/SQL 원문은 저장하지 않습니다. 잠금은 같은 root 컨트롤러 사본에 한정되고 직접 호출한 하위 명령이나 다른 사본/호스트를 잠그지는 않습니다. 자동 DB 롤백·볼륨 삭제·중단 후 자동 재개는 추가하지 않았습니다.

### Helm 0.2.0

모든 Service/StatefulSet/Pod에 release 선택자를 맞추고 긴 이름의 단순 절단 충돌도 hash suffix로 줄였습니다. 고정 password 필드를 제거하고 Secret 참조를 사용합니다. `k8s init`은 기존 Secret을 보존하거나 표준 입력으로 새 자격 증명을 생성합니다. 명시한 context/allowlist, 전용 namespace, 기존 selector, bootstrap 대상 PVC, StorageClass를 검사합니다. context 이름 검사만으로 실제 운영 클러스터 여부를 증명할 수는 없습니다.

Redis는 Sentinel 다수 의견에 따라 primary/replica 역할을 설정하고 복제 인증을 적용합니다. Sentinel hostname 해석/announce와 설정 PVC를 추가해 재시작 때마다 redis-0으로 덮어쓰는 동작을 제거했습니다. MariaDB는 `/bitnami/mariadb`에 PVC를 연결하고 peer/client Service, 최초 bootstrap/수동 복구 경로를 분리했습니다. 가장 최신 Galera 복구 후보 자동 판정은 구현하지 않았습니다.

Kafka POD_NAME 선언 순서와 listener FQDN, ZooKeeper peer FQDN/1-based ID를 수정했습니다. Confluent 7.9.0 공식 소스의 실제 data/log 경로를 함께 보존하도록 PVC 마운트를 정리했습니다. 각 Pod에 startup/readiness, 자원 요청/제한, 비권한 실행 설정, 선택형 노드 분산과 PDB를 추가했습니다. NetworkPolicy는 CNI의 실제 시행 확인이 필요합니다.

기본 활성 구성은 Redis+Sentinel만으로 축소했고 full/kafka-only/mariadb-only/distributed를 분리했습니다. 지원하지 않는 replica 수/조합은 schema 또는 template에서 거부합니다. 최초 ES/MariaDB bootstrap 플래그는 성공적인 준비 대기 후 두 번째 upgrade로 끕니다. 기존 사용자 values를 보존하여 사전 계획과 실제 upgrade의 설정 결합 순서를 맞췄습니다. 실패 자원 자동 삭제나 운영 데이터 복구를 구현한 것은 아닙니다.

## F01~F16 처리 상태

| ID | 반영 상태 | 남은 완료 조건 |
|---|---|---|
| F01–F02 | loopback 기본값, 기존 공개 설정 차단, 자동 방화벽 변경 제거 | 실제 외부 접속 및 호스트 규칙 불변 시험 |
| F03–F06 | 생성 context/ID/포트/heap/노드 범위/모드 분기 수정 | 캐시 없는 이미지 빌드, zk/kraft 실제 생산·소비 |
| F07 | 건별 pacing, 종료 경계, 오류/재접속 지표 분리 | 실제 DB/호스트에서 목표 부하와 오차 측정 |
| F08 | Helm Redis 역할/복제 인증, Sentinel hostname·설정 보존 | 전환·복귀·Sentinel 전체 재시작/데이터 비교 |
| F09 | Helm Kafka/ZK 주소·ID·규모 검증 | 실제 env/DNS/쿼럼/I/O |
| F10 | MariaDB PVC 경로·peer/bootstrap/복구 보호 수정 | 이미지 pull/UID/datadir/SST, 전체 복구 후보 선정과 데이터 보존 |
| F11 | release 선택자 및 긴 이름 격리 | 실제 두 release EndpointSlice 격리, migration 검증 |
| F12 | Secret 참조, 대상 보호, 고정 암호 거부 | 현장 RBAC/접근 제어, 안전한 수동 암호 회전 |
| F13 | preflight, health JSON, root 잠금, 실행 기록 | 포트 소유권 종합 판정/용량 인증/자동 재개는 미구현 |
| F14 | probe/resources/placement/PDB/소형 기본 profile | 실제 Pending/자원 부족/쿼럼/worker 장애 시험 |
| F15 | 모의/실측 범위 문서화, 기존 개별 장애 도구 유지 | 공통 장애 harness·다중 호스트·업무 E2E 흐름은 미구현 |
| F16 | 실제 생성 파일 및 보호 장치 테스트, 별도 실제 Helm 검사 명령 | real engine/Helm/Kubernetes/image digest/현대화 검증 |

“반영”은 해당 코드 변경이 존재하고 아래 오프라인 검사 범위에서 확인했다는 뜻입니다. 런타임 잔여 조건까지 완료했다는 판정이 아닙니다.

## 검증과 증거

이번 수정본에서 **총 644개 테스트가 통과**했습니다.

| 대상 | 통과 수 |
|---|---:|
| root (51 routing + 24 guards + 16 chart subset) | 91 |
| elasticsearch | 77 |
| kafka | 94 |
| mariadb | 198 |
| redis | 184 |

Bash 구문 검사 53개, Python 구문 검사 60개가 통과했습니다. 실제 Helm은 미설치로 검사 명령이 127을 반환했고 대체 도구로 통과 처리하지 않았습니다.

정확한 최종 테스트 개수와 종료 코드는 `docs/remediation-evidence/summary.json`을 기준으로 합니다. root routing·guard·chart 계약 테스트와 네 하위 실습 테스트를 실행하고, Bash/Python 구문과 배포 ZIP 무결성도 검사했습니다. 기존의 공개 바인딩을 기대하던 ES 테스트 두 개는 새 안전 기본값 계약에 맞춰 바꿨습니다. ZIP 추출에서 사라진 실행 권한도 복원했습니다. 실패한 검사를 삭제해서 통과시키지 않았습니다.

MariaDB pacing의 1/10/100회 초당 제한, 대기, 실패/재접속, 기간 경계 시험은 가짜 시계/DB를 사용합니다. 실제 DB TPS 측정 결과가 아닙니다. chart 검사는 Go `text/template`와 작은 함수 집합으로 실제 템플릿을 실행한 후 YAML/schema 계약을 검사한 것으로 **Helm 실행 결과가 아닙니다.** 원본 renderer source를 `tests/tooling/`에 포함했습니다.

컨테이너 런타임, 실제 Helm/kubectl, ShellCheck가 없어 해당 단계는 미실행입니다. 이미지 pull 가능성/내부 바이너리, PVC 보존, failover, RTO/RPO, 취약점 scan 결과를 생성하지 않았습니다. 표준 테스트는 별도 호스트 Python 환경에서 수행했으며 작업 환경의 Python 시작 부가 처리를 피하기 위해 `-S`와 명시한 site-packages 경로를 사용했습니다. 테스트 대상 로직/의존성은 동일하지만 이 환경 설정도 증거에 기록합니다.

## 적용 순서

기존 `.env`/볼륨 및 유효한 KRaft ID를 먼저 백업하고 수정 ZIP을 별도 디렉터리에 해제합니다. 전체 소스가 필요하며 `all.sh`만 덮어쓰지 않습니다. `preflight`, `--dry-run`, 단일 프로젝트 실제 기동, `health` 순으로 확인합니다. 기존 공개 ES 설정은 loopback으로 전환하거나 격리된 네트워크에서 명시적으로 허용합니다. 잘못된 저장 ID를 삭제해 문제를 숨기지 않습니다.

Helm은 새 namespace/release로 시작하고 `helmchart/README.md`를 따릅니다. 기존 chart의 immutable selector/이름/PVC 차이가 있으므로 강제 교체하지 않습니다. 일반 down은 유지하고 `reset --yes` 또는 PVC 삭제는 시험 환경을 실제로 폐기할 때만 별도 판단합니다. 상세 인수 시험과 각 합격 조건은 `docs/RUNTIME-ACCEPTANCE.md`에 있습니다.

## 공식 계약 확인 출처

- Apache Kafka 3.9 `Uuid.java`: URL-safe Base64 16바이트 ID 형식. https://raw.githubusercontent.com/apache/kafka/3.9/clients/src/main/java/org/apache/kafka/common/Uuid.java
- Redis Sentinel 공식 문서: hostname 옵션, 쓰기 가능한 설정, quorum 및 복제 한계. https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/
- Bitnami MariaDB Galera 공식 README: 영속 경로·bootstrap 환경 변수. 문서가 고정 이미지의 pull/기동을 보장하는 것은 아님. https://raw.githubusercontent.com/bitnami/containers/main/bitnami/mariadb-galera/README.md
- Confluent 7.9.0 ZooKeeper template/configure: 고정 data/log 경로, server 번호, myid 경로. https://raw.githubusercontent.com/confluentinc/kafka-images/7.9.0/zookeeper/include/etc/confluent/docker/zookeeper.properties.template
- Helm 3.19 upgrade 구현: 기존 values 결합 계약 비교. https://raw.githubusercontent.com/helm/helm/v3.19.0/pkg/action/upgrade.go
