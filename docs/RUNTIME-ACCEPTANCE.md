# 실제 런타임 인수 시험 — 아직 수행하지 않은 항목

이 문서는 합격 결과가 아니라 실행 절차입니다. 이번 수정본에서 수행한 것은 호스트 로직/모의 테스트, 생성 설정, Go-template 부분 계약 검사입니다. 실제 Helm 검사는 별도 명령으로 실행하고, Kubernetes API 수용 여부와 DB 동작도 다시 검사해야 합니다.

## 시험 경계

폐기 가능한 전용 VM/클러스터를 사용합니다. 실제 업무 데이터·운영 Secret·운영 kubeconfig를 넣지 않습니다. 기존 데이터를 유지하는 복구 시험은 먼저 백업/복원 가능성을 확인합니다. `reset`, PVC 삭제, 강제 bootstrap, `prune`를 자동 사전 정리로 사용하지 않습니다. 장애 시험은 합의된 대상과 복구 방법을 확인하고 수행합니다.

각 실행에 코드 SHA, 실행 시각, OS/CPU 아키텍처, 엔진/Compose/Helm/Kubernetes 버전, 이미지 digest, 실습 seed, 토폴로지, 설정 hash, 장애 계획을 남깁니다. 환경 파일과 Secret 원문은 증거에 넣지 않습니다. `reports/all-*.json`은 명령 결과의 일부만 기록하므로 위 항목 전체를 자동 수집했다고 간주하면 안 됩니다.

## 1. 정적·생성 검증

```bash
# 전용 Python 환경에서 requirements 설치 후 실행
bash scripts/test-offline.sh
bash scripts/test-helm.sh
```

두 명령을 분리합니다. 첫 명령의 Go renderer는 차트에 필요한 일부 함수만 구현한 계약 검사이며 Helm 대체물이 아닙니다. 두 번째 명령은 실제 Helm이 없으면 127로 종료합니다. 실제 Helm 렌더링도 admission, 이미지 pull, 준비 상태, 데이터 보존의 증거는 아닙니다.

## 2. Compose 신규·기존 볼륨 시험

```bash
./all.sh preflight --json kafka mariadb
./all.sh --dry-run up kafka mariadb
./all.sh up kafka mariadb
./all.sh health --json kafka mariadb
./all.sh status kafka mariadb
```

처음부터 네 프로젝트를 동시에 띄우지 말고 한 프로젝트씩 검증한 뒤 누적 자원을 확인합니다. Kafka/Redis는 Podman 중심이며 이름만 바꾸어 Docker 지원을 주장하지 않습니다. Elasticsearch와 MariaDB는 각 프로젝트의 provider 선택에 맞추어 실제 지원 조합별로 검증합니다.

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

선택 이미지의 digest와 실제 UID, `SELECT @@datadir`, 컨테이너 mount/PVC를 기록합니다. 시험 행 삽입 후 Pod를 하나씩 재생성하여 데이터가 남고 모든 노드의 `wsrep_cluster_status=Primary`, `wsrep_local_state_comment=Synced`, `wsrep_cluster_size=3`, cluster UUID가 일치하는지 확인합니다.

전체 중단 시험은 모든 노드 상태를 모아 UUID/seqno/recover 결과와 `safe_to_bootstrap`를 검토합니다. 최신 상태를 확인하기 전에는 `recovery.confirmed`를 켜지 않습니다. 후보 선정/복구를 자동 완성한 기능은 없으며, 현장 검토 후 허용한 후보만 bootstrap하도록 보호했습니다. 전체 복구 후 승인된 쓰기 집합을 대조하고 bootstrap/recovery 플래그가 꺼졌는지 확인합니다.

## 6. 아직 구현하지 않은 확장

다중 호스트/가용영역 실험, 비대칭 단절·지연·손실·느린 디스크를 통합 제어하는 공통 harness, 복원 데이터 정합성 기반 RTO/RPO 자동 산출, MariaDB→Kafka→Elasticsearch/Redis end-to-end 업무 파이프라인은 이번 수정 범위에 들어 있지 않습니다. 기존 개별 장애 도구는 유지했습니다. 이 단계의 지표나 HA 보장을 이번 오프라인 테스트 수로 대신하지 않습니다.
