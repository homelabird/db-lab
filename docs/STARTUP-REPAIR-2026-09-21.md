# DB Lab v11 — 실행 오류 분석·수정 보고서

작성일: 2026-09-21 · 대상: 사용자가 이번 대화에 첨부한 `db-lab-main(1).zip`

원본 ZIP SHA-256: `180bf1284e00c0f29a7868295df8a92d5e911d334b22a29a55ff9064f624da6e`

## 1. 결론과 적용 범위

기존 호스트 테스트가 **1,566개 모두 통과해도 실제 실행 경로가 깨질 수 있는 상태**였습니다. 특히 Redis Cluster의 생성 Compose 빌드 경로, 클러스터 볼륨 검사, redis-py 클라이언트 생성, 초기화 및 준비 확인에 실제 결함이 있었습니다. Kafka는 노드 수를 바꿔 실행해도 준비 확인에서 3개를 요구하고, 선택한 구성이 다음 명령에 유지되지 않았습니다.

이번 수정은 해당 소스 결함을 재현하는 테스트를 먼저 추가하고 실행 코드와 함께 수정했습니다. 코드의 오류를 확인한 것이며, 사용자 호스트의 실제 로그가 제공되지 않은 상태에서 모든 접속 실패의 원인을 확정한 것은 아닙니다. 새 기능을 크게 늘리기보다 기존 실행 경로와 데이터 보존을 우선했습니다.

**실제 Docker/Podman 이미지 빌드, DB 컨테이너 기동, 복제, 장애 전환 및 원격 Ansible 실행은 이 작업 환경에서 검증하지 못했습니다.** 이 환경에는 해당 엔진과 Docker 소켓이 없고, Helm/Ansible CLI도 없습니다. 따라서 이 문서의 호스트 테스트 통과를 운영·실기동 보증으로 해석하면 안 됩니다.

## 2. 먼저 구분해야 하는 실행 대상

| 진입점 | 실제 대상 | 이번 점검에서의 구분 |
|---|---|---|
| `bash all.sh mvp up` | MariaDB·Kafka·Elasticsearch·Redis·API·worker를 묶은 독립적인 6개 컨테이너 주문 실습 | 아래의 4개 HA 실습에 자동 연결되는 시스템이 아님 |
| `bash all.sh up` | Elasticsearch, Kafka, MariaDB, Redis의 별도 HA 실습을 순차 실행 | 전체 preflight가 실패하면 어느 대상도 시작하지 않음 |
| `bash all.sh up redis` 등 | 지정한 HA 실습만 실행 | 먼저 개별 대상을 진단할 때 사용 |
| `redis-lab/ops.sh` | Redis의 별도 운영·성능 확장 도구 | 기존 Sentinel 전제를 유지하며 Cluster 지원으로 확장하지 않음 |
| `helmchart/`, `ansible/` | 별도의 Kubernetes 배포 / 제어 경로 | Compose 기동과 다른 경로이며 이번에 실렌더링·실배포하지 않음 |

Kafka 및 Redis의 기존 HA 랩은 **Podman + podman-compose 전제**입니다. Docker만 설치된 호스트에서 공통 `all.sh up`을 실행해도 네 랩 모두가 자동으로 Docker로 전환되지는 않습니다. 이 제한을 README 상단에 명시했습니다. 이번 수정이 모든 HA 랩에 Docker 지원을 추가한 것은 아닙니다.

## 3. 확인한 결함과 실제 수정

### A. Redis Cluster: 빌드·볼륨·초기화

| 원래 문제 | 수정 내용 | 주요 파일 |
|---|---|---|
| `.lab/`에 생성한 Compose가 `build.context: .`를 사용하여 실제 Containerfile이 없는 디렉터리를 빌드 컨텍스트로 지정 | 실제 Redis 프로젝트 루트의 절대 경로를 JSON 인용하여 생성. 공백이 포함된 경로도 검사 | `redis-lab/scripts/manage.py` |
| 클러스터 노드 볼륨을 Sentinel용 볼륨 허용 목록으로 검사해 정상 볼륨도 거부 | 현재 모드·노드 수에 맞는 활성 볼륨만 허용. 타 랩 볼륨의 소유권 검사는 유지 | 동일 |
| 초기화 주소를 이전 `CLUSTER_1_IP` 형태에서 읽어 현재 설정과 불일치, KeyError 가능 | 현재 노드 수와 기본 IP로 계산한 `cluster_endpoints()` 사용 | 동일 |
| 초기화 명령을 다시 실행할 때 이미 구성된 클러스터도 무조건 생성 대상으로 취급 | 모든 노드를 인증하여 확인. 완전히 비어 있는 노드만 생성, 정상 기존 클러스터는 재생성 없이 검증 | 동일 |
| 불완전한 토폴로지·연결 실패·데이터가 있는 노드와 빈 노드 구분이 불충분 | 상태를 읽지 못하면 빈 상태로 간주하지 않고 실패. 부분 구성·비정상·비어 있지 않은 미구성 노드는 자동 초기화/초기화 해제하지 않음 | 동일 |
| 노드 수/복제 수의 조합이 실제 3개 이상의 primary를 만들 수 있는지 미검사 | primary 최소 3개 조건 추가. 예: 4개 노드/복제 1개와 6개 노드/복제 2개 거부 | 동일 |
| 이미 지문이 기록된 환경에서 `--nodes`를 바꾸면 실패 전 `.env`가 바뀔 수 있음 | 토폴로지 변경을 `.env` 기록 전에 거부. 설정 지문 검사 선행 | 동일 |

정상 여부는 모든 노드의 `cluster_state`, 알려진 노드 수, 할당/정상 슬롯 수 16384, 실패 슬롯 0 등을 확인합니다. 단순 PING 성공이나 컨테이너 존재만으로 준비 완료라고 표시하지 않습니다. 새 클러스터 생성 뒤에도 클라이언트의 준비 확인을 거칩니다.

### B. Redis 클라이언트: 연결 생성·상태 해석·WAIT

| 원래 문제 | 수정 내용 | 주요 파일 |
|---|---|---|
| `RedisCluster` 생성 시 명시적 `password`와 `kwargs()` 내부 `password`가 중복 | 중복 인자 제거. 원본에 대해 실제 `TypeError`를 발생시키는 회귀 테스트 추가 | `redis-lab/client/client.py` |
| `execute_command('CLUSTER', 'INFO')` 호출로 redis-py의 `CLUSTER INFO` 응답 변환 경로를 놓침 | 전체 명령 이름 `CLUSTER INFO`로 호출하여 매핑 응답 사용 | 동일 |
| Cluster의 쓰기·WAIT·읽기에 일반 Redis와 동일한 `.client()` 사용 전제 | 키 담당 노드를 찾고 해당 노드의 Redis 연결에서 전용 연결 확보. 쓰기와 WAIT를 같은 연결에서 수행 | 동일 |
| 준비 확인의 복제 수가 실제 `CLUSTER_REPLICAS`와 불일치 | 생성 Compose에 복제 수 전달, wait/marker/workload에서 설정값 사용 | 클라이언트 및 관리 스크립트 |
| CLUSTER INFO가 누락되거나 노드 수가 맞지 않아도 정상 판정 가능 | 응답 완전성, 슬롯 상태, 알려진 노드 수와 설정 노드 수 일치를 필수로 검사 | 클라이언트 |

`WAIT`는 그 연결에서 앞서 수행한 쓰기의 복제 확인에 관한 명령입니다. 임의의 다른 노드나 연결에서 호출해도 같은 쓰기를 확인했다고 볼 수 없습니다. 또한 WAIT를 사용한다고 강한 일관성이나 모든 장애에서의 무손실이 보장되는 것은 아닙니다. 이 수정은 호출 경로를 바로잡은 것이지 Redis의 보장 수준을 바꾼 것이 아닙니다.

### C. Redis Cluster bus 포트

커스텀 bus 포트는 광고 설정만 바뀌고 실제 리스너의 `cluster-port`가 빠져 있었습니다. 템플릿에 리스너 설정을 추가하고 데이터 포트 6379와의 충돌을 거부했습니다.

이미 생성된 설정 파일에는 기존 환경 지문이 일치하는 경우에만 누락된 `cluster-port` 한 줄을 보완합니다. 기존 `nodes.conf`, 역할/epoch 정보와 데이터 파일은 수정하지 않습니다. 임시 파일시스템을 이용한 실제 entrypoint 실행 테스트로 보완의 반복 실행과 기존 내용 보존을 확인했습니다. 이것은 실제 Redis 서버 기동 테스트는 아닙니다.

### D. Kafka: 가변 노드 기동과 다음 명령의 설정 유지

원래 초기 준비 확인이 `--brokers 3`으로 고정되어, 1개/5개 등 다른 노드 수로 생성한 토폴로지와 맞지 않았습니다. 이제 실제 `NODES`로 검사합니다.

새 `kafka-lab/scripts/topology-state.py`는 첫 기동 시 유효한 `LAB_NAME`, `KAFKA_MODE`, `NODES`를 `.env`에 저장하고 `.state/active-topology.json`에 선택한 토폴로지를 기록합니다. 다음 health/status/down도 같은 구성을 사용합니다. 다른 비밀값과 KRaft ID는 변경하지 않습니다.

토폴로지 잠금은 DB 컨테이너를 만들기 전에 기록하므로 기동 도중 실패해도 다음 명령이 엉뚱한 구성을 사용하지 않습니다. 이미 기록한 노드 수·모드·이름을 바꾸는 작업은 Compose 재생성 전에 거부합니다. `down`은 볼륨과 토폴로지 기록을 보존하며, 기존의 명시적 데이터 삭제 reset이 성공한 경우에만 해당 기록을 제거합니다.

이 기능은 자동 확장/축소나 ZooKeeper↔KRaft 마이그레이션이 아닙니다. v11 이전의 기록 없는 실행 환경을 자동으로 역추적하지도 않습니다. 기존 환경에 적용할 때는 원래 사용한 토폴로지 값을 유지해야 합니다. 파일 잠금, 원자적 교체, 깨진 JSON/중복 설정/심볼릭 링크 거부를 회귀 테스트에 포함했습니다.

### E. 품질 검사와 MariaDB 진단 안내

`quality.py`가 Redis가 정상 실행 중 생성하는 `.lab/` 상태 및 `redis-lab/output/` 결과 파일까지 배포 소스처럼 취급했습니다. 이 때문에 정상 사용 이후 release 검사가 실패하거나 생성 Compose를 소스 검사에 섞을 수 있었습니다. 해당 런타임 경로를 제외하고 추적 대상 `.gitkeep`은 유지했습니다. 임의의 다른 `output/` 디렉터리에 추가한 실제 소스를 숨기지 않는 양성 대조 테스트도 추가했습니다.

MariaDB의 Galera 네트워크 진단 실패에 DB 데이터 삭제 reset을 제안하던 메시지를 제거했습니다. 이제 status/logs/네트워크 확인을 안내하며, 연결 문제 해결 목적으로 볼륨을 지우지 말라고 명시합니다. MariaDB의 복제 구현을 새로 작성하거나 데이터 복구를 실행하지는 않았습니다.

## 4. 검증 결과

<!-- FINAL_VALIDATION_START -->
최종 배포 소스의 호스트 검사: **1,608개 통과, 실패 0, 건너뜀 0**. 원본 대비 42개 회귀 테스트를 추가했습니다.

| 검사 | 원본 | 수정본 | 결과 |
|---|---:|---:|---|
| 루트 제어기·품질 검사 | 206 | 209 | 통과 |
| MVP | 807 | 807 | 통과 |
| Elasticsearch | 77 | 77 | 통과 |
| Kafka | 94 | 108 | 통과 |
| MariaDB | 198 | 198 | 통과 |
| Redis | 184 | 209 | 통과 |
| 합계 | **1,566** | **1,608** | **통과** |

정적 검사도 통과했습니다: Python 141개, Bash 59개, 일반 YAML 36개, Markdown 82개. Helm 템플릿 11개는 일반 YAML 파싱에서 제외했으며, 이를 Helm 렌더링 통과로 계산하지 않았습니다.

별도 임시 사본에서는 실제 help / dry-run / init / MVP init 진입점을 실행했습니다. dry-run은 환경·상태 파일을 생성하지 않았고, init을 반복해도 5개 `.env` 파일 내용이 그대로 유지되었습니다. 엔진 없는 호스트의 preflight는 예상한 실패 코드 1로 종료했습니다.

실제 도구 검사(`quality.py tools`)는 Ansible과 Helm 모두 미설치로 **BLOCKED, 종료 코드 127**입니다. 건너뛴 검사를 통과로 바꾸지 않았습니다.

배포 패치의 적용 가능성·설정 파일 보존, 소스 해시/권한 일치, 최종 ZIP 재압축 무결성 검사는 별도 배포 검증 로그에 기록합니다. 테스트의 기능 실행 코드는 전체 검사 시작 전에 고정했고, 이후에는 이 결과 문서와 release manifest만 갱신했습니다.

증빙: 별도 제공 파일 `db-lab-validation-20260921.zip`의 `baseline/`, `before/`, `final/`, `tools/`, `smoke/`, `release/`, `static-final/`, `packaging.json`.
<!-- FINAL_VALIDATION_END -->

### 기존 테스트 통과를 그대로 신뢰하지 않은 이유

원본에서 기존 테스트 1,566개가 모두 통과했습니다. 그 다음 새로 작성한 Redis Cluster, Kafka 토폴로지, 품질 검사 회귀 테스트를 **원본 실행 코드에 적용하여 실패를 재현**했습니다. 동일한 기대 조건을 유지한 채 실행 코드를 수정했습니다. 단순히 테스트 수를 늘리거나 실패 조건을 제거해 통과시킨 것이 아닙니다.

추가 테스트의 주요 범위는 키워드 인자 중복, 실제 Containerfile 위치, 클러스터 소유 볼륨, 변경된 노드 수/IP, 설정 파일 보존, 전체 명령 이름에 따른 응답 파서 계약, 키 소유 노드의 전용 연결, 누락/불완전 상태, 초기화 반복 실행, bus 리스너 설정, Kafka 선택 구성 유지 및 토폴로지 변경 차단입니다.

CLI 호출은 명령을 기록하는 테스트 더블로, Redis 드라이버는 모듈 더블로 검증한 부분이 있습니다. 로컬 파일, 쉘 진입점, HTTP 테스트와 표준 라이브러리 동작은 실제 실행했습니다. **가짜 엔진의 응답을 실제 클러스터 증거라고 보고하지 않습니다.**

### 실행하지 못한 검증

Docker/Podman 및 Compose 실구동, 이미지 pull/build, redis-py 실제 패키지와 실제 Redis 서버의 통합 실행, Kafka 리더 선출, Redis 슬롯 구성/복제/장애 전환, MariaDB Galera 복구, Elasticsearch shard 복구, MVP의 실제 DB 간 이벤트 전파, Helm 실제 렌더링, Ansible 실제 제어 명령 실행은 미검증입니다. 외부 패키지 다운로드도 이 컨테이너의 네트워크 제약으로 완료하지 못했습니다.

## 5. 기존 데이터가 있는 프로젝트에 적용

수정 ZIP은 소스 배포물입니다. 사용자 `.env`, `.state`, `.lab`, 데이터 볼륨 및 이번 검사 중 생성한 임시 비밀값을 포함하지 않습니다. 이전 소스에 원래 들어 있던 보고서는 과거 기록으로 보존했습니다.

기존 DB를 재사용한다면 새 디렉터리로 이동한 뒤 무조건 `init/up`을 실행하기보다 **기존 프로젝트에서 패치를 먼저 검토**하세요. 원래 실행하던 사용자 계정, 경로, 런타임, 프로젝트 이름과 설정을 유지해야 다른 볼륨을 새로 만들거나 다른 엔진의 상태를 보는 혼선을 피할 수 있습니다.

```bash
# 기존 프로젝트 루트에서 실행. patch 경로는 실제 보관 위치로 지정합니다.
git apply --check /path/to/db-lab-fix-20260921.patch
git apply /path/to/db-lab-fix-20260921.patch

# 사용한 대상부터 확인합니다. 이 명령은 전체 기동 지시가 아닙니다.
bash all.sh status
bash all.sh preflight
```

패치가 충돌하면 강제로 덮어쓰거나 원래 `.env`를 삭제하지 말고 로컬 변경과 비교해야 합니다. `.env`/상태 파일의 비공개 백업과 DB 자체 백업은 별도로 관리하세요. 상태 파일을 지워 토폴로지 보호 장치를 우회하는 것은 권장하지 않습니다.

이번 패치는 기존 reset 명령의 명시적 사용자 확인을 없애지 않습니다. `reset`, `down -v`, `volume rm`, `.env` 재생성을 연결 실패의 기본 해결책으로 사용하지 마세요.

## 6. 실행 순서

### 작은 통합 시스템을 확인하려는 경우: MVP

기존 HA 랩 네 개를 전부 띄우는 것이 아니라 다음 경로입니다. init은 초기 환경 준비용이며 기존 설정을 임의로 교체하기 위한 명령이 아닙니다.

```bash
bash all.sh mvp init
bash all.sh mvp doctor
bash all.sh mvp up
bash all.sh mvp smoke
bash all.sh mvp diagnose
```

기본 접속 주소는 `http://127.0.0.1:18090`입니다. 원격 서버에서 실행한다면 자신의 PC의 localhost와 혼동하지 마세요. 상세 런타임 요구 조건은 [MVP 가이드](../mvp-lab/README.md)를 따릅니다. doctor 실패를 무시하고 다음 단계로 계속 진행하지 마세요.

### 기존 HA 랩: 필요한 대상부터 확인

```bash
bash all.sh init
bash all.sh preflight redis
bash all.sh doctor redis
bash all.sh up redis
bash all.sh health --json redis
```

위 Redis 기본 구성은 Sentinel입니다. Redis Cluster를 새로 만드는 경우에는 **최초 기동 전** `redis-lab/.env`의 모드, 노드 수, 복제 수를 선택해야 합니다. 예를 들어 `DEPLOYMENT_MODE=cluster`, `CLUSTER_NODE_COUNT=6`, `CLUSTER_REPLICAS=1`로 설정한 새 랩에서:

```bash
cd redis-lab
bash lab.sh up
bash lab.sh cluster-init
bash lab.sh health --json
```

이미 Sentinel로 사용한 데이터/설정 위에 모드만 바꾸는 절차가 아닙니다. 기존 healthy Cluster에서 `cluster-init`을 반복하면 재생성하지 않고 검증합니다. 부분 구성/비정상 상태가 확인되면 안전하게 중단하며 이를 자동 복구했다고 표시하지 않습니다.

Kafka 기존 환경은 기존 `.env`를 유지하고 `bash all.sh up kafka`, `bash all.sh health --json kafka`로 확인합니다. 새 랩의 `--nodes` 선택은 최초 기동 전에 하며, 이미 실행한 환경에서 노드 수나 모드를 바꾸는 용도로 사용하지 마세요. 다른 토폴로지 실습은 별도 디렉터리와 고유한 LAB_NAME으로 분리합니다.

## 7. 남은 위험과 판단

컨테이너 엔진을 자동 통일하지 않은 점, 4개 HA 랩과 별도 MVP의 복수 제어 경로, 큰 관리 함수들이 유지보수 위험으로 남습니다. 포트 충돌, 호스트 메모리/디스크, Elasticsearch 호스트 커널 설정, Podman rootless 네트워크, 레지스트리 접근 실패와 오래된 데이터의 실제 버전 호환성은 사용자 호스트에서 별도로 확인해야 합니다.

이번 작업에서 구체적인 실행 결함은 고쳤지만, 모든 기능과 모든 환경을 완전 검증했다거나 사용자 환경의 문제가 전부 해결됐다고 결론내리지 않습니다. 제공한 로그는 검증한 범위를 재현하기 위한 자료이며, 남은 실기동 인수 절차는 [런타임 검증 절차](RUNTIME-ACCEPTANCE.md)를 참고하세요.

## 8. 수정 판단에 사용한 공식 자료

프로젝트 결함의 직접 근거는 첨부 소스와 회귀 테스트입니다. 외부 API 의미는 다음 1차 자료와 대조했습니다.

- Docker Compose build context 경로 해석: `https://docs.docker.com/reference/compose-file/build/`
- redis-py 6.4.0 Cluster API: `https://raw.githubusercontent.com/redis/redis-py/v6.4.0/redis/cluster.py`
- redis-py 명령 이름 및 응답 변환: `https://raw.githubusercontent.com/redis/redis-py/v6.4.0/redis/commands/core.py`, `https://raw.githubusercontent.com/redis/redis-py/v6.4.0/redis/_parsers/helpers.py`
- Redis WAIT의 연결 단위 의미와 한계: `https://redis.io/docs/latest/commands/wait/`
- Redis 7.4.11 설정의 cluster-port: `https://raw.githubusercontent.com/redis/redis/7.4.11/redis.conf`

외부 문서와의 대조는 이 환경에서 해당 서버/드라이버를 설치해 통합 테스트했다는 의미가 아닙니다.
