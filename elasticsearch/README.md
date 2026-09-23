# Elasticsearch 5노드 + Cerebro — 시드·검색·샤드 실습 랩

**실제 데이터를 ZIP에 포함하지 않습니다.** `./lab.sh seed`를 실행하면 로컬에서 모의 데이터를 만들고 Elasticsearch에 스트리밍 Bulk 적재합니다. 외부 데이터셋 다운로드, 개인정보, 실제 금융 거래는 사용하지 않습니다.

운영 조회/대응 학습은 [시나리오별 dev/staging/prod 대응 쿼리](../docs/SCENARIO-RESPONSE-QUERIES.md)의 Elasticsearch 절을 참고하세요.

## 시작하기

Linux/WSL2, Bash, Python 3.9+, curl, Docker Compose 또는 Podman Compose가 필요합니다.
추가 Python 패키지는 필요하지 않습니다. `doctor`와 `up`은 `.env`가 없으면
`.env.example`에서 권한 `600`으로 생성하며, 기존 `.env`는 덮어쓰지 않습니다.

```bash
./lab.sh doctor                 # 사전 점검 및 누락 도구 안내
./lab.sh up                     # ES 7.17 5노드 + Cerebro + Kibana 시작
./lab.sh seed --size-mb 5       # 빠른 확인용 작은 데이터 적재
./lab.sh verify                 # 문서·샤드·검색 예제 검증
./lab.sh ui                     # 접속 주소 출력
```

`./lab.sh doctor --install-missing`은 선택형 설치 명령입니다. 감지한 패키지
관리자와 sudo를 사용해 없는 `python3`, `curl`, `iptables`를 설치합니다.
`iptables`는 네트워크 장애 진단용 선택 도구입니다. `vm.max_map_count`는
호스트 설정이므로 자동으로 바꾸지 않습니다. 필요하면 doctor가 안내하는 명령을 실행하세요.

## 자주 쓰는 명령

| 작업 | 명령 |
|---|---|
| 설치 전체 확인 | `./lab.sh verify-install` |
| 상태·로그 | `./lab.sh status`, `./lab.sh logs es01 --tail 100` |
| 샘플 실행·검증 | `./lab.sh demo` |
| 시드 인덱스만 삭제 | `./lab.sh purge --yes` |
| 중지하고 데이터 보존 | `./lab.sh down` |
| 컨테이너와 프로젝트 볼륨 삭제 | `./lab.sh down --purge --yes` |
| 장애 실습 목록 | `./lab.sh scenario list`, `./lab.sh fault list` |

`purge --yes`는 다섯 시드 인덱스만 삭제합니다. `down --purge --yes`는
Elasticsearch 데이터와 snapshot을 포함한 프로젝트 볼륨을 삭제합니다.

## 주요 설정

| `.env` 설정 | 기본값 | 설명 |
|---|---:|---|
| `COMPOSE_PROVIDER` | `auto` | `docker`, `podman`, `podman-compose` 선택 |
| `ES_PORT` / `CEREBRO_PORT` / `KIBANA_PORT` | `9200` / `9000` / `5601` | 호스트 포트 |
| `KIBANA_STACK_MONITORING_ENABLED` | `true` | Stack Monitoring 표시 |
| `KIBANA_STACK_MANAGEMENT_ENABLED` | `true` | 보안 모드의 관리 권한 허용 |
| `XPACK_SECURITY_ENABLED` | `false` | 인증 및 노드 간 TLS 활성화 |

`KIBANA_STACK_MANAGEMENT_ENABLED=false`는 `KIBANA_LOGIN_USERNAME` 계정에 제한된
권한을 부여합니다. 이 권한 제어에는 보안 모드와 로그인 계정이 필요합니다.
일반 `up`은 호스트 방화벽을 변경하지 않습니다.

기본 호스트 바인딩은 `127.0.0.1`(loopback)입니다. Elasticsearch 9200, Cerebro 9000,
Kibana 5601은 기본적으로 같은 호스트에서 접속하며, 두 UI는 같은 Elasticsearch를 사용합니다.

원격 접속은 SSH 터널을 우선 사용하세요. 격리된 네트워크에서 공개하려면 `.env`에
`ES_BIND_IP=0.0.0.0`과 `ES_ALLOW_PUBLIC_BIND=yes`를 명시해야 합니다. 다른 PC에서는
서버 IP와 각 포트로 접속합니다. `0.0.0.0`은 listen 주소이며 브라우저 주소가 아닙니다.

기존 `.env`는 새 기본값보다 우선합니다. 포트나 바인딩을 바꾸면 해당 설정을 `.env`에
맞춰 적고 `./lab.sh up`을 실행하세요. 적용에는 컨테이너 재생성이 필요하지만 named
volume은 보존됩니다. 호스트 Elasticsearch 포트를 바꾸면 `ES_URL`도 함께 맞추세요.

`./lab.sh up`은 시드를 자동 적재하지 않습니다. `./lab.sh verify`는 실행 중인
Elasticsearch에서 문서 수, 노드, 샤드 배치, 검색 예제를 검사합니다. 장애 실습 후
정상 상태를 확인할 때 사용하세요.

## 런타임 자동 감지와 네트워크 진단

`lab.sh`는 실행 환경을 자동 감지합니다. `.env`의 `COMPOSE_PROVIDER`를
`auto|docker|podman|podman-compose`로 지정할 수 있고, `auto`는
podman-compose → `podman compose` → `docker compose` 순으로 선택합니다.
컨테이너 런타임(`RUNTIME`)과 Compose 네트워크(`es-lab`)의 실제 서브넷을
`./lab.sh doctor`에서 확인할 수 있습니다.

```
Container runtime: docker
Compose provider: docker
Compose network: cerebro-seed-lab_es-lab (172.26.0.0/16)
```

`compose.yaml`의 네트워크는 Docker 전용 옵션 없이 일반 bridge를 사용합니다.

`./lab.sh up`은 인증서와 snapshot 볼륨을 준비한 뒤 다음 상태를 순서대로 확인합니다.

```
1  컨테이너 5개가 실행 중이고 Elasticsearch 클러스터가 green인지
2  공유 Snapshot Repository가 5개 노드에서 검증되는지
3  Kibana API status와 Cerebro HTTP가 응답하는지 (보안 모드에서는 Cerebro 생략)
4  설치 검증 결과를 reports/install-verification.json에 기록
```

클러스터가 준비되지 않으면 스크립트가 ①요청 서비스 자체가 죽었는지(로그 확인)
②컨테이너 내부 localhost:9200은 응답하는데 컨테이너 간 통신만 실패하는지(호스트
방화벽)를 구분해 안내합니다. 둘째 경우 실제 서브넷이 포함된 실행 가능한 규칙을
출력합니다(`LAB_SUBNET` 같은 placeholder를 사용하지 않습니다).

```
sudo iptables-legacy -I FORWARD 1 -s 172.26.0.0/16 -d 172.26.0.0/16 -j ACCEPT
```

일반 `up`은 호스트 방화벽을 변경하지 않습니다. FORWARD 전체 허용은 관리자가 수동 적용합니다.

## Snapshot Repository

기본적으로 5개 노드가 공유하는 `es-snapshots` 볼륨을
`/usr/share/elasticsearch/snapshots`에 마운트하고, `./lab.sh up`이 기동 중에
`lab-snapshots`(fs) 저장소를 등록하고 `_verify`까지 수행합니다. 저장소 이름은
`.env`의 `SNAPSHOT_REPO_NAME`으로 바꿀 수 있습니다. `up` 직후부터 다음 실습이
바로 가능합니다.

```bash
./lab.sh snapshot          # (재)등록 + verify + 확인 노드 출력
# 저장소 지정 스냅샷 생성/조회/복원/삭제 (Kibana Console 또는 curl)
curl -X PUT  http://127.0.0.1:9200/_snapshot/lab-snapshots/snap1?wait_for_completion=true \
     -H 'Content-Type: application/json' -d '{"indices":"lab-transactions-v1"}'
curl        http://127.0.0.1:9200/_snapshot/lab-snapshots/_all
curl -X POST http://127.0.0.1:9200/_snapshot/lab-snapshots/snap1/_restore?wait_for_completion=true
curl -X DELETE http://127.0.0.1:9200/_snapshot/lab-snapshots/snap1
curl -X POST http://127.0.0.1:9200/_snapshot/lab-snapshots/_verify
```

## X-Pack Security (옵트인)

기본은 기존처럼 인증 없는 legacy 랩입니다(`xpack.security.enabled=false`).
`.env`에서 명시적으로 켤 수 있습니다.

```bash
XPACK_SECURITY_ENABLED=true
ELASTIC_USERNAME=elastic
ELASTIC_PASSWORD=<강한 비밀번호>
KIBANA_PASSWORD=<kibana_system 비밀번호>
KIBANA_LOGIN_USERNAME=lab-admin
KIBANA_LOGIN_PASSWORD=<강한 로그인 비밀번호>
```

보안 모드에서는 `ELASTIC_PASSWORD`로 `elastic` 계정을 인증합니다. `lab.sh`의
curl 요청과 `lablib.py`(Python 스크립트)는
`ELASTIC_USERNAME`/`ELASTIC_PASSWORD`로 Basic 인증을 자동 첨부합니다.
Kibana 서버는 `KIBANA_USERNAME`/`KIBANA_PASSWORD`로 인증하며, 기본 사용자는
`kibana_system`입니다. 이 계정의 비밀번호는 `elastic` 비밀번호와 별도로
Elasticsearch에 설정해야 합니다.

ES 7.17용 노드 간 TLS 인증서는 보안 모드 기동 시 자동 생성됩니다. `KIBANA_PASSWORD`,
`KIBANA_LOGIN_USERNAME`, `KIBANA_LOGIN_PASSWORD`도 `.env`에 설정해야 합니다.
보안 모드에서는 현재 인증 구성이 없는 Cerebro를 시작하지 않습니다.

## Kubernetes 배포 및 버전 업그레이드

Compose 외에 `./lab.sh k8s apply`로 Elasticsearch 3노드 StatefulSet, Kibana,
Cerebro를 Kubernetes에 배포할 수 있습니다. `./lab.sh k8s verify`는 실제
클러스터 health와 3노드 구성을 검사하고, `./lab.sh k8s upgrade
docker.elastic.co/elasticsearch/elasticsearch:7.17.29`는 7.17.x 범위의
rolling update를 수행합니다. 상세 조건과 삭제 주의사항은
[Kubernetes 가이드](docs/KUBERNETES.md)를 참고하세요.

재사용 가능한 Helm Chart도 제공하며 다음처럼 설치합니다.

```bash
./lab.sh helm lint helm/elasticsearch-lab
./lab.sh helm upgrade --install es-lab ./helm/elasticsearch-lab \
  -n elasticsearch-lab --create-namespace
```

이전 랩이 실행 중이면 포트 9200/9000/5601이 겹칠 수 있습니다. 이전 랩을 종료하거나 `.env`의 `ES_PORT`, `CEREBRO_PORT`, `KIBANA_PORT`, `ES_URL`을 함께 변경하세요. 별도 사본을 동시에 실행하려면 `COMPOSE_PROJECT_NAME`과 `LAB_CONTAINER_PREFIX`도 각각 고유하게 설정하세요. 기존 볼륨을 자동 이관하거나 삭제하지 않습니다.

## 장애 대응 검증 (추가)

실제 노드 정지·SIGKILL·master 교체·미할당·쓰기 차단을 주입하고, 관측과 복구를 검사합니다. **이 제작 환경에서 완료한 것은 오프라인/모의 검사이며 실제 ES 5노드 장애 시험은 미실행입니다.** [장애 실습 가이드](docs/FAULT-DRILLS.md)와 [검증 결과/한계](docs/FAULT-VALIDATION.md)를 먼저 읽으세요.

```bash
# 기존 writer와 다른 장애 실험을 먼저 중지
./lab.sh fault list
./lab.sh fault run node-stop --node es03 --yes

# 8개 기본 장애를 각각 주입·확인·원복
./lab.sh fault run all --yes

# 직접 관찰 후 원복
./lab.sh fault apply write-block --yes
./lab.sh fault diagnose
./lab.sh fault recover
```

복구 시 원래 노드 목록과 green뿐 아니라 시드 문서 수·index UUID·문서 표본 해시와 canary 읽기/쓰기/검색을 검사합니다. 설정을 임의의 기본값으로 초기화하지 않고 변경 전 값을 복원합니다. 기본 보고서는 `reports/faults/`에 저장됩니다. `active.json`이 남아 있으면 원복을 완료하기 전 삭제하지 마세요.

기존 번호별 `02/03/05/06/07/08/10/11`은 동일한 검증기를 사용합니다. 예: `./lab.sh scenario 06 --test --yes`. 장애 주입에는 명시적인 `--yes`가 필요합니다.

## 구성과 기본 데이터량

| 항목 | 기본값 |
|---|---|
| Elasticsearch | 7.17.29, `es01`~`es05` 5개, 모두 master/data/ingest 역할 |
| Cerebro | 0.9.4 |
| JVM heap | 노드당 640MiB, 5개 합계 3.125GiB |
| 원문 데이터 크기 | UTF-8 `_source` JSON + 줄바꿈 합계 약 100MiB |
| 날짜 | 2026-08-01 00:00:00 UTC 이상, 2026-09-01 00:00:00 UTC 미만 |
| 난수 seed | 42 |
| 적재 방식 | 기본 500건씩, 요청 크기 최대 4MiB, 고정 문서 ID |
| 정적 데이터 파일 | ZIP에 없음. 기본 적재 실행 시에도 원문 파일을 저장하지 않음 |

### 9.x 랩과 공용 코어

이 7.x 랩과 신규 **`../elasticsearch-9/`(es9)** 랩은 `../lib/es-lab/`의 공용 코어
(`common.sh` + `lablib_core.py`)를 공유합니다. `scripts/common.sh`는 코어 로더이고,
노드·네트워크·스냅샷·진단 공통 로직은 코어에 있습니다. 이 랩의
`scripts/lablib.py`는 7.x 전용(인덱스 레이아웃/시드 적재/쿼리 예제)만 유지합니다.
`es9`은 opt-in 프로젝트이며 루트 `./all.sh es9 ...`로 실행합니다.

| 인덱스 | 기본 생성 확인 문서 수* | 원문 크기 | Primary | Replica 설정 | 전체 copy |
|---|---:|---:|---:|---:|---:|
| `lab-transactions-v1` | 47,157 | 약 40MiB | 12 | 1 | 24 |
| `lab-web-logs-v1` | 28,021 | 약 25MiB | 18 | 1 | 36 |
| `lab-audit-v1` | 25,411 | 약 18MiB | 8 | 2 | 24 |
| `lab-commerce-v1` | 10,623 | 약 12MiB | 16 | 2 | 48 |
| `lab-observability-v1` | 8,929 | 약 8MiB | 24 | 1 | 48 |
| **합계** | **약 120,000** | **약 103MiB** | **78** | — | **180** |

\* 제공 환경에서 기본 설정으로 생성한 결과입니다. 정확한 실행 결과는 `reports/seed-manifest.json`으로 확인하세요. 용량·기간·payload·seed 변경 시 문서 수는 달라집니다.

**100MiB는 `pri.store.size` 목표가 아닙니다.** 원문 JSON, Bulk 전송량, Primary Lucene 저장량, Replica 포함 저장량, translog까지 포함한 파일시스템 사용량은 서로 다릅니다. Primary/Replica 저장량은 `./lab.sh size`로 측정합니다. 많은 작은 샤드는 배치와 이동을 관찰하기 위한 의도적인 교육용 설정이며 운영 권장 샤드 크기를 뜻하지 않습니다.

**시드 데이터는 현실적인 분포로 생성됩니다.** 생성기(shared `lib/es-lab/datagen/realistic.py`, `seed-v4-real`)는 카드 발급사/브랜드, 국내외 실제풍 가맹점명·도시, 카테고리별 금액대, 요일·시간대 패턴, 기기·해외·암호화폐 결제의 사기 룰, 웹 요청의 서비스별 엔드포인트·상태코드·지연 분포, 상품 카탈로그 기반 주문/배송/결제, 메트릭 계열의 트렌드·이상치 등 실제 근접한 필드와 값으로 문서를 만듭니다. 결정성(seed 42), ID 규칙, 고정 사고 카덴스는 유지되며, ES 9 랩(`../elasticsearch-9/`)과 동일 생성기·동일 스키마를 공유합니다.

## 문서 읽는 순서

| 문서 | 내용 |
|---|---|
| [시작·환경 설정](docs/QUICKSTART.md) | Podman, 포트 변경, 원격 접속, 중지·재시작 |
| [시드 데이터 가이드](docs/SEED-GUIDE.md) | 스키마·생성 조건·100MiB 기준·재적재·오류 처리 |
| [검색 실습 가이드](docs/QUERY-GUIDE.md) | curl/Cerebro REST, match/term/bool/range, 집계, 페이지 조회, CRUD |
| [연습문제와 정답](docs/EXERCISES.md) | 시드 데이터를 사용하는 10개 문제와 실행 명령 |
| [샤드 실습](docs/SHARD-LABS.md) | 이동, drain, replica, 장애, allocation explain |
| [문제 해결](docs/TROUBLESHOOTING.md) | 검색 0건, 기동 실패, yellow, 매핑 오류, 포트 충돌 |
| [장애 대응 실습](docs/FAULT-DRILLS.md) | 자동/수동 주입, 증상별 진단, 복구, Cerebro 관찰 |
| [장애 스크립트 검증](docs/FAULT-VALIDATION.md) | 수정 내용, 오프라인/모의 검사, 실제 검증 미실행 항목 |
| [검증 범위와 결과](docs/VALIDATION.md) | 실제 수행한 검사와 미실행한 항목 구분 |

## 고급 시드 적재 옵션

### 반복 부하 측정

```bash
./lab.sh benchmark --seconds 30 --rate 100 --batch 100
python3 ../scripts/benchmarks.py reports/benchmarks/RUN-1.json reports/benchmarks/RUN-2.json
```

7.x와 `es9`는 공유 실행기를 사용합니다. 측정 전 reserved `lab-benchmark-v1` 인덱스가
없어야 합니다. 실행기가 빈 임시 인덱스를 만들고 합성 bulk 문서를 적재·refresh·count 검증한 뒤
인덱스를 삭제하며, 결과는 `reports/benchmarks/`에 남깁니다. 비정상 종료로 인덱스가 남으면
먼저 이름과 내용을 확인하세요. 기존 시드 인덱스는 변경하지 않습니다. 측정치는 bulk batch
응답 latency와 indexing throughput이며 검색 latency, 데이터 내구성, host 동등성 또는 운영
capacity를 나타내지 않습니다. host/container 자원 정보가 빠지면 공통 비교기에서
`performance_comparison_ready: false`로 표시됩니다.

### 고급 시드 적재 옵션

```bash
# 기본: 약 100MiB 생성 + 적재
./lab.sh seed

# 데이터셋이 없는 새 클러스터에서 소량 시험
./lab.sh seed --size-mb 5

# 이미 다른 설정으로 시드가 있으면, 명시적으로 5개 인덱스를 삭제·재생성
# 해당 인덱스의 실습 수정·추가 데이터도 삭제됩니다.
./lab.sh seed --size-mb 100 --recreate --yes

# 완전히 같은 설정으로 재실행: 고정 ID에 덮어쓰기, 문서 수 중복 증가 없음
./lab.sh seed

# ES 연결 없이 파일 생성만. 이때만 datasets/generated/에 원문 파일이 생깁니다.
./lab.sh seed --generate-only --size-mb 100

# 검색 요청 자체를 출력: Cerebro REST에 넣을 Method / Path / Body 확인
./lab.sh query 04-high-risk --show-only

# 모든 읽기 전용 예제 실행
./lab.sh query all

# PIT + search_after 3페이지
./lab.sh pit --pages 3 --page-size 10

# 데이터 삭제 없이 실습용 allocation / replica / refresh 설정 복구
./scripts/05-reset-cluster-settings.sh

# 컨테이너 중지, 볼륨 보존
./scripts/02-down.sh
```

## 자원과 안전

5개 노드의 heap 이외에도 JVM native memory, Lucene page cache, Cerebro에 메모리가 필요합니다. 실습 가이드 기준으로 VM/호스트 메모리 8GiB 이상, 가용 메모리 약 6GiB 이상, 이미지와 데이터를 위한 가용 디스크 10GiB 이상을 준비하세요. 이는 랩용 여유치이며 성능 보증이 아닙니다.

인증/TLS가 비활성화되어 있으며, 기본 포트는 **loopback(`127.0.0.1`)**에 바인딩됩니다. 원격 바인딩은 명시적 동의가 필요합니다. **접근 가능한 사용자는 데이터 조회·변경·삭제를 할 수 있으므로 인터넷에 공개하지 마세요.** 호스트 방화벽과 클라우드 보안그룹에서 실습 PC의 IP만 허용하세요. 로컬 접속만 필요하면 `.env`에 `ES_BIND_IP=127.0.0.1`을 지정하고 컨테이너를 재생성합니다. ES 7.17은 기존 랩 호환을 위한 고정 버전이며 신규 운영 배포 권장이 아닙니다. 데이터 변경 스크립트는 기본적으로 `cluster.name=cerebro-shard-lab`을 확인하지만, 이름 검사는 인증이나 보안 경계가 아닙니다.

공식 7.17 문서에 API 의미와 이 버전의 문서 유지보수 상태가 명시되어 있습니다: [Bulk API](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/docs-bulk.html), [클러스터 초기 설정](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/important-settings.html).
