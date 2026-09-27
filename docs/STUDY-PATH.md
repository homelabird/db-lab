# DB Lab 학습 시작 경로

이 저장소는 Elasticsearch, Kafka, MariaDB, Redis를 직접 기동하고 데이터 흐름·성능·복구를 관찰하는 로컬 실습장입니다. 각 랩은 Linux/WSL2와 컨테이너 런타임을 요구하며, DB를 포함한 시험 자원은 실습 전용 환경에서만 사용하세요.

## 목표에 맞는 랩 선택

처음에는 한 랩만 선택합니다. `./all.sh up`은 네 HA 랩과 여섯 컨테이너 MVP를 모두 시작하므로, 노트북에서 첫 실습을 할 때는 합산 자원과 포트를 먼저 확인하세요.

| 배우려는 내용 | 시작 경로 | 첫 관찰 지점 |
|---|---|---|
| DB 네 개가 한 서비스 흐름에 미치는 영향 | [MVP 안내](../mvp-lab/README.md): `./all.sh mvp init` → `./all.sh mvp doctor` → `./all.sh mvp up` → `./all.sh mvp smoke` | 새 주문 ID를 기준으로 MariaDB, outbox, Kafka, Elasticsearch 검색, Redis cache hit를 대조합니다. 기본 여유 메모리 설계값은 6–8 GiB이며 실측 최소 사양은 아닙니다. |
| Kafka topic·consumer·복제·장애 복구 | [Kafka 기본](../kafka-lab/docs/01-kafka-basics.md) → [hands-on](../kafka-lab/docs/02-hands-on.md) → [장애 실습](../kafka-lab/docs/03-failure-scenarios.md) | `kafka-lab/`에서 `./lab.sh up → ./lab.sh smoke → ./lab.sh seed → ./lab.sh read → ./lab.sh consume → ./lab.sh demo lag → ./lab.sh demo broker-failover → ./lab.sh demo min-isr` 순서로 진행합니다. |
| SQL·InnoDB·Galera 복제와 lock | [MariaDB 워크북](../mariadb-ha-lab/docs/WORKBOOK.md) | `mariadb-ha-lab/`에서 `./lab.sh init` → `./lab.sh doctor` → `./lab.sh up` 뒤 워크북 01부터 진행합니다. 의도된 deadlock은 비정상 SQL 종료를 포함할 수 있습니다. |
| Redis 부하·메모리·복제·Sentinel | [Redis 단계별 시작](../redis-lab/docs/ops/00-start.md) → [기록용 worksheet](../redis-lab/docs/ops/worksheet.md) | `redis-lab/`에서 안내된 `./ops.sh` 기준선 → 원인별 실험 → 메모리 → HA 순서로 비교합니다. HA와 확장 실험은 4–6 GiB를 설계 예산으로 안내합니다. CPU 제한 실습은 cgroup v2 권한도 필요합니다. |
| 검색·샤드 배치·장애 | Elasticsearch 버전을 먼저 고릅니다: [ES7 검색·샤드·장애 교재](../elasticsearch/README.md) 또는 [ES9 기능·장애 안내](../elasticsearch-9/README.md) | ES7에는 기존 샤드 드릴이 있고, ES9에는 데이터스트림·ES|QL·kNN·async search와 별도 노드/quorum 드릴이 있습니다. 두 랩의 기능은 같지 않습니다. |
| Kubernetes 배포와 복구 경계 | [Helm 안내](../helmchart/README.md) | 별도 disposable context와 namespace를 설정한 뒤 프로젝트 루트에서 `./all.sh k8s init` → `./all.sh k8s preflight` → 선택 프로필 배포 순으로 접근합니다. 지원 범위와 MariaDB 복구 결과를 먼저 확인하세요. |

## 공통 실습 순서

1. 선택한 랩의 시작·안전 안내와 현재 런타임 검증을 읽습니다.
2. 해당 랩이 제공하는 `doctor`/`preflight`/`plan` 명령으로 실행 대상과 전제 조건을 확인합니다.
3. 기준 상태에서 health, 데이터 수, 지표를 기록합니다.
4. 한 번에 장애나 설정 변경 하나만 적용하고, 명령 결과뿐 아니라 로그·DB 상태·업무 데이터를 확인합니다.
5. 복구 명령 후 health와 원래 데이터 불변조건을 다시 확인합니다.
6. 아래 기록 항목에 예상·관찰·복구 결과와 해석을 적습니다.

질문은 매번 같습니다. **무엇이 실패했나? 어떤 계층의 지표가 이를 보여 주나? 데이터가 보존됐다는 근거는 무엇인가? 복구 뒤에도 같은 불변조건이 성립하나?**

```text
가설:
실행한 작업과 대상:
관찰한 상태·로그·데이터:
복구 후 확인한 불변조건:
해석과 남은 질문:
```

[MVP 기록 예시 양식](../mvp-lab/docs/DRILL-NOTES.md)도 사용할 수 있습니다.

## 검증 결과 읽기

각 결과의 적용 범위는 실행한 DB·버전·provider·시나리오로 제한됩니다.

| 증거 | 입증하는 범위 |
|---|---|
| static/offline/host test | 소스·설정·테스트 경로. DB 기동이나 복제는 입증하지 않습니다. |
| Helm lint/render 또는 Compose config | 설정 생성과 렌더링. 이미지 pull, readiness, 데이터 보존은 입증하지 않습니다. |
| container up/status | 컨테이너 상태. DB 쿼리 성공이나 업무 데이터 정합성과는 다릅니다. |
| health/smoke/verify | 해당 실행에서 확인한 서비스 연결·데이터 조건. provider와 버전 범위를 넘겨 일반화하지 않습니다. |
| 장애 주입 후 recovery와 데이터 대조 | 해당 장애·토폴로지의 복구 결과. 다른 장애나 production HA 보증은 아닙니다. |

아래는 저장소에 기록된 **가장 최근의 명시적 실기동 범위**입니다. 2026-09-27 rootless Podman kind acceptance에서 MariaDB fresh bootstrap·3노드 복제·동일 PVC Pod 재합류·순차 전체 정전 복구가 통과했습니다. Kubernetes node failure, 다른 provider/StorageClass, production 복구는 검증하지 않았습니다.

| 대상 | 저장소에 기록된 실기동 근거 | 실습에서의 의미 |
|---|---|---|
| MVP Compose | [MVP 안내](../mvp-lab/README.md)는 Docker Compose와 Podman provider 실기동을 확인하지 못했다고 기록합니다. | `doctor`/`smoke`를 실행해 본인 환경을 확인합니다. 오프라인 테스트는 DB 간 이벤트 전달 근거가 아닙니다. |
| Elasticsearch 7 Compose | Podman 5.8.7 / Elasticsearch 7.17.29 5노드 green에서 2026-09-27 bulk repeat 3회가 3,000건 색인·cleanup을 통과했습니다. p95 CV 80.11%, source revision/limits 누락으로 성능 기준선은 아닙니다. | Bulk write 경로만 확인했습니다. search latency·failover는 별도 검증이 필요하며 7.x는 legacy 교육 경로입니다. |
| Elasticsearch 9 Compose | [ES9 안내](../elasticsearch-9/README.md)에 Podman 기반 설치·green·기능·장애 복구와 2026-09-27 30초 반복 benchmark 3회가 기록돼 있습니다. 각 run 3,000건은 검증됐고 비교기 gates를 통과했지만 p95 산포(CV 50.27%)가 커 성능 기준선은 아닙니다. | 해당 provider/설정에서 bulk write와 cleanup을 관찰한 근거입니다. 검색 성능·capacity·SLO는 입증하지 않습니다. |
| Kafka Compose | [Kafka 안내](../kafka-lab/README.md)에 2026-09-27 isolated Podman 5.8.7 / CP 7.9.0 ZooKeeper mode 실기동과 30초 producer repeat 3회가 기록돼 있습니다. 90,000건 전달·offset 검증·temporary topic cleanup은 통과했고 p95 ack CV 15.25%, host CPU busy 52.7–57.5%였습니다. | 실제 producer/offset/cleanup 경로의 근거입니다. source revision과 broker limits가 없고 host contention이 있어 capacity/SLO 및 장애 전환은 입증하지 않습니다. |
| MariaDB / Redis 독립 랩 | MariaDB Galera 11.8.9는 2026-09-27 격리 Podman에서 3노드 benchmark 반복 3회와 dataset cleanup을 통과했지만 p95 CV 18.49%, source revision/limits 누락으로 기준선은 아닙니다. Redis는 [안내](../redis-lab/README.md)에 격리 Podman 실기동과 반복 workload가 기록돼 있으나 p99 CV 36.9%, dispatch skip이 있습니다. | 두 run set은 topology와 bounded workload/data invariant 범위만 입증합니다. Redis failover/PVC 복구, MariaDB failover/rebuild/PVC recovery는 별도 검증이 필요합니다. |
| Helm Redis / MariaDB | [Helm 런타임 기록](RUNTIME-ACCEPTANCE.md): Redis primary 전환은 2026-09-24 통과했습니다. MariaDB는 2026-09-27 rootless Podman kind에서 3노드 bootstrap/복제, 같은 PVC Pod 재합류, 전체 정전 복구와 flag 봉인이 통과했습니다. | MariaDB Kubernetes node failure·다른 provider/StorageClass·production 복구는 미검증입니다. |
| Ansible 원격 제어 | [운영 로드맵](LOCAL-OPS-AUTOMATION-ROADMAP.md)은 disposable Debian SSH 경로 통과와 외부 원격 호스트의 runtime·복구 미검증을 구분합니다. | SSH fixture 통과는 별도 서버의 DB 배포·복구 증거가 아닙니다. |

Elasticsearch 7/9, Redis, Kafka, MariaDB의 반복 부하는 관측자료 gates를 통과한 run set도 있었지만 산포·scheduler skip·source/container metadata 누락 때문에 안정된 성능 기준선으로 판정되지 않았습니다. 세부 환경, 제외 범위, acceptance 절차는 [런타임 인수 가이드](RUNTIME-ACCEPTANCE.md)와 각 랩 README를 따릅니다.

## 최신 제품과 교육용 버전 구분

- Elasticsearch 기본 랩과 MVP의 7.17 계열은 [지원 종료일](https://www.elastic.co/support/eol)이 2026-01-15인 legacy 교육 경로입니다. ES9는 별도 랩으로 선택하고 현대 기능 경로에 사용하세요.
- Kafka 기본 ZooKeeper 모드는 Kafka 3.9 계열에서의 역사적 실습입니다. [Kafka 4는 ZooKeeper를 제거하고 KRaft만 지원합니다](https://kafka.apache.org/40/getting-started/upgrade/). 이 저장소의 ZooKeeper와 KRaft 구성은 별도 상태·볼륨을 쓰며, 모드 전환은 마이그레이션이 아닙니다.
- Compose 실습은 한 호스트의 클러스터를 보여 줍니다. 다중 호스트·가용영역 내구성이나 실제 RTO/RPO를 증명하지 않습니다. Helm/Ansible도 대상·provider별 acceptance 범위를 읽고 사용하세요.

## 실제 운영 솔루션과 연결하기

실습에서 원리를 확인한 뒤 제품 운영 방식과 비교해 보세요.

- [Percona Operator for PostgreSQL](https://docs.percona.com/percona-operator-for-postgresql/3.0.0/features.html)은 자동 failover, 백업, PITR을 제공하는 Kubernetes 운영 솔루션입니다. 수동 복구 실습에서 사람이 실행한 단계 중 무엇을 Operator가 조정하는지 비교할 수 있습니다.
- [PostgresAI DBLab Engine](https://v2.postgres.ai/docs/database-lab)은 PostgreSQL 데이터 clone/branch로 쿼리와 migration을 안전하게 시험하는 오픈소스 제품입니다. 본 저장소의 다중 DB 장애 실습과 목표가 다른 도구이므로, 전체 생산 데이터 복제와 임시 실험 DB 제공이라는 별도 운영 문제를 살펴보는 비교 자료로 사용하세요.
- SQL 기초는 [PostgreSQL Tutorial](https://www.postgresql.org/docs/current/tutorial.html), 문제 풀이 연습은 [PGExercises](https://www.pgexercises.com/)를 참고할 수 있습니다. 이 자료는 SQL 학습에, 본 저장소는 HA·장애 훈련에 초점을 둡니다.
