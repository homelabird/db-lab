# Elasticsearch 9 랩 (es9) — 5노드 클러스터 + Kibana 9

Elasticsearch **9.5.x** 기반 5노드 클러스터와 Kibana 9를 올리는 실습 랩입니다.
legacy 7.x 랩(`elasticsearch/`)과 **같은 공용 코어**(`../lib/es-lab/`)를 공유하며,
코어 스택·snapshot·seed·query 외에 ES9 전용 노드 장애/마스터 정족수 실습을 제공합니다.
샤드 배치·allocation 정책 등 ES7 전용 토폴로지 드릴은 legacy 랩에 남습니다.
**Cerebro는 의도적으로 포함하지 않습니다**(legacy 7.x 보조 UI용) — 화면 검색과 관리는 Kibana를 사용합니다.

보안 인증이 필요한 read-only cluster/shard 조회는 [공통 시나리오 대응 가이드](../docs/SCENARIO-RESPONSE-QUERIES.md)를 참고하세요.

- Docker Compose로 검증했습니다. 2026-09-23에는 Podman 엔진의 `podman compose`
  경로로도 설치와 재실행을 확인했습니다. 이 호스트에는 별도 `podman-compose`
  실행 파일이 없어 해당 provider 자체의 동작은 검증하지 못했습니다.
- 기본 포트: Elasticsearch **9201**, Kibana **5602**. legacy 7.x 랩(9200/9000/5601)과
  충돌하지 않아 두 랩을 동시에 기동할 수 있습니다.
- 같은 ES9 랩의 별도 사본을 동시에 실행하려면 `.env`의 `COMPOSE_PROJECT_NAME`과
  `LAB_CONTAINER_PREFIX`를 모두 고유하게 지정하세요. `COMPOSE_PROJECT_NAME`을 바꾸면 기존 명명 볼륨은
  이전 프로젝트 이름으로 남으며 자동 이동/연결되지 않습니다. 이전 데이터를 쓰려면 볼륨을 확인하고 복원 절차를 따르세요.
- 기본 보안은 꺼져 있으며(무인증, loopback 바인딩), `XPACK_SECURITY_ENABLED=true`는
  옵트인입니다(아래 한계 참고).

## 빠른 시작

`doctor` 또는 `up`은 `.env`가 없으면 `.env.example`에서 권한 `600`으로 생성합니다.
기존 `.env`는 덮어쓰지 않습니다. Linux/WSL2, Bash, Python 3.9+, curl, Docker Compose
또는 Podman Compose(`podman compose` 포함)가 필요합니다.

```bash
./lab.sh doctor                 # 엔진·도구·메모리·네트워크 사전 점검
./lab.sh up                     # ES 5노드 green + snapshot + Kibana
./lab.sh seed --size-mb 5       # 빠른 확인용 데이터 적재
./lab.sh verify                 # 데이터·샤드·쿼리 검증
./lab.sh verify-install         # 설치 전체 확인
./lab.sh drills list             # ES9 장애 실습 목록
./lab.sh drills plan node-outage # 실제 변경 없이 계획 확인
./lab.sh ui                     # 접속 주소 출력
```

도구 설치가 필요하면 `./lab.sh doctor --install-missing`을 선택하세요. 감지한
패키지 관리자와 sudo로 누락된 `python3`, `curl`, `iptables`를 설치합니다.
`iptables`는 네트워크 장애 진단용 선택 도구입니다.

| 작업 | 명령 |
|---|---|
| 노드·샤드 상태 | `./lab.sh status` |
| 로그 보기 | `./lab.sh logs es02` |
| 쿼리 예제 | `./lab.sh query list` |
| ES9 기능 예제 | `./lab.sh features` |
| 장애 실습 목록/계획 | `./lab.sh drills list` / `./lab.sh drills plan node-outage` |
| 시드 인덱스만 삭제 | `./lab.sh purge --yes` |
| 중지하고 데이터 보존 | `./lab.sh down` |
| 컨테이너와 프로젝트 볼륨 삭제 | `./lab.sh down --purge --yes` |

`purge --yes`는 다섯 개의 시드 인덱스만 지웁니다. `down --purge --yes`는
Elasticsearch 데이터와 snapshot을 포함한 프로젝트 볼륨을 삭제합니다.

## 주요 설정

`.env`에서 설정합니다. Stack Monitoring은 보안 설정과 독립적으로 표시를 제어합니다.
`KIBANA_STACK_MANAGEMENT_ENABLED=false`는 `KIBANA_LOGIN_USERNAME` 계정에 제한된
권한을 부여합니다. 이 권한 제어에는 보안 모드와 로그인 계정이 필요합니다.

| 설정 | 기본값 | 설명 |
|---|---:|---|
| `COMPOSE_PROVIDER` | `auto` | `docker`, `podman`, `podman-compose` 선택 |
| `ES_PORT` / `KIBANA_PORT` | `9201` / `5602` | 호스트 포트 |
| `KIBANA_STACK_MONITORING_ENABLED` | `true` | Stack Monitoring 표시 |
| `KIBANA_STACK_MANAGEMENT_ENABLED` | `true` | 보안 모드의 관리 권한 허용 |
| `XPACK_SECURITY_ENABLED` | `false` | 인증 및 노드 간 TLS 활성화 |

루트 `./all.sh es9 <command>`로도 실행할 수 있습니다. 예: `./all.sh es9 up`.

### 반복 부하 측정

```bash
./lab.sh benchmark --seconds 30 --rate 100 --batch 100
python3 ../scripts/benchmarks.py reports/benchmarks/RUN-1.json reports/benchmarks/RUN-2.json
```

7.x와 같은 공유 실행기가 reserved `lab-benchmark-v1` 임시 인덱스에 합성 문서를 쓰고,
refresh/count 검증 후 인덱스를 삭제합니다. 보고서는 `reports/benchmarks/`에 남습니다.
측정값은 bulk write 응답 latency/throughput이며 검색, 내구성, host 동등성 또는 production
capacity를 증명하지 않습니다. 비정상 종료 후 임시 인덱스가 남으면 기존 데이터를 자동
삭제하지 않으므로 수동으로 검사한 뒤 정리해야 합니다.

## 구조

```
compose.yaml            ES 9.5.3 x5 + Kibana 9.5.3, YAML 앵커로 노드 중복 최소화
lab.sh                  subset 래퍼: doctor/up/down/status/ui/logs/compose/snapshot/
                        seed/benchmark/verify/size/purge/query/offline-tests
scripts/common.sh       공용 코어 로더 (LAB_ROOT/LAB_CONTAINER_PREFIX=es9-lab- 설정 후 ../lib/es-lab/common.sh source)
scripts/lablib.py       공용 lablib_core 임포트 + INDICES/LAYOUT + bulk/catalog 헬퍼
scripts/generate_and_load.py  시드 생성·적재 (../lib/es-lab/datagen/realistic 공용 생성기 위임)
scripts/verify_seed.py  시드·샤드·쿼리 예제 26종 라이브 검증
scripts/features.py     ES9 전용 기능: 데이터스트림+ILM, ES|QL, kNN, async search
scripts/drills.py       journal 기반 node-outage/quorum-loss 및 복구 검증
scripts/01-up.sh        [기동: 인증서/스냅샷 준비 → 5노드 green → Kibana → 설치 검증]
scripts/12-offline-tests.sh  셸 구문·호스트 회귀 테스트(실행 불필요)
mappings/*.json         5개 시드 인덱스 매핑 (legacy와 동일 스키마)
queries/*.json          쿼리 예제 26종 (legacy와 동일 카탈로그)
tests/test_es9_defaults.py   compose/.env/공용 코어 배선에 대한 정적 테스트
tests/test_es9_seed.py       생성기·매핑 타입·양 랩 동일성·카덴스 정적 테스트
tests/test_es9_features.py   features 스크립트·공용 카탈로그 비오염·kNN 픽스처 정적 테스트
tests/test_es9_drills.py     drill 계획·동의·보고서 권한·symlink 안전성 호스트 테스트
.env.example           포트/이름/보안 옵트인 기본값
```

접속/부트스트랩/스냅샷/포트/재시작 문제는 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)를 보세요.

## 기동 설계(ES 9 의미론)

- **Security**: ES 8/9의 자동 보안 구성은 단일 노드 전용이므로, 멀티노드 무인증 랩은
  `xpack.security.enabled=${XPACK_SECURITY_ENABLED:-false}`와
  `xpack.security.autoconfiguration.enabled=false`를 명시합니다.
- **Bootstrap**: `cluster.initial_master_nodes`(es01,02,03)는 **최초 부팅(빈 데이터
  디렉터리)에서만** entrypoint shim이 주입합니다. 이미 형성된 클러스터의 재시작에는
  설정하지 않으며 `discovery.seed_hosts`로 합류합니다(ES 공식 지침). 재시작은
  `./lab.sh down && ./lab.sh up`으로 검증했습니다.
- **스냅샷 볼륨 소유권**: 새 명명 볼륨은 root 소유로 생성되기 때문에, `snapshot-init`
  1회성 서비스가 ES 기동 전에 `chown -R 1000:0`을 수행합니다(rootless Docker 포함).
  모든 노드가 공유하는 `es-snapshots` 볼륨을 `path.repo`로 사용합니다.
- **호스트 포트**: legacy와 동일하게 es01(+Kibana)만 호스트 포트를 게시하고 나머지
  노드는 내부 브릿지에서 서비스합니다. 호스트에서의 접근은 es01(9201)을 통해
  클러스터 전체 API를 사용합니다.

## 시드 데이터

`./lab.sh seed`는 legacy 7.x 랩과 **같은 공용 생성기**(`../lib/es-lab/datagen/realistic.py`,
`seed-v4-real`)를 쓰므로 동일 설정·같은 seed면 문서가 완전히 동일합니다. 카드 발급사·
국내 가맹점·카테고리별 금액대, 요일·시간대 패턴, 사기 룰, 서비스별 엔드포인트·상태코드·
지연 분포, 상품 카탈로그 주문, 메트릭 트렌드·이상치를 현실적인 값으로 만듭니다(ES 9 엔진에서
라이브 검증).

```bash
./lab.sh seed --size-mb 5      # smoke용 소량
./lab.sh seed --size-mb 100    # 기본 100MiB(README 참고)
./lab.sh seed --recreate --yes # 기존 시드 인덱스 5개만 삭제 후 재생성
./lab.sh verify                # 문서 수·샤드·쿼리 예제 26종
./lab.sh size                  # Lucene 저장량
./lab.sh purge --yes           # 시드 인덱스 5개만 삭제
```

인덱스 5종(`lab-transactions-v1` 등)과 쿼리 예제 26종은 legacy와 동일합니다.

## 스냅샷 실습(요약)

```bash
BASE=http://127.0.0.1:9201
curl -XPUT "$BASE/es9-lab-test" -H 'Content-Type: application/json' \
  -d '{"settings":{"number_of_shards":1,"number_of_replicas":1}}'
curl -XPUT "$BASE/_snapshot/lab-snapshots/snap-1?wait_for_completion=true" \
  -d '{"indices":"es9-lab-test"}' -H 'Content-Type: application/json'
curl "$BASE/_snapshot/lab-snapshots/_all"
curl -XPOST "$BASE/_snapshot/lab-snapshots/snap-1/_restore" \
  -d '{"indices":"es9-lab-test","rename_pattern":"(.+)","rename_replacement":"$1-restored"}' \
  -H 'Content-Type: application/json'
curl -XDELETE "$BASE/_snapshot/lab-snapshots/snap-1"
```

## ES9 현대 기능 실습

`./lab.sh features`는 ES9 전용 추가 시나리오로, 공용 시드·legacy 카탈로그·7.x 드릴은
건드리지 않습니다. 결과는 `reports/features.json`에 기록됩니다.

| 기능 | 내용 |
|---|---|
| 데이터스트림 + ILM | `lab-api-logs` 데이터스트림(인덱스 템플릿 `lab-api-logs-template`, `data_stream` 활성) + `lab-ilm-30d` 수명주기(hot rollover → delete). 20건 API 로그 시드, 재실행 시 재사용 |
| ES|QL | `_query`로 시드 인덱스·데이터스트림 대상 집계 5종(고위험 거래 국가, 5xx 서비스, 데이터스트림 5xx, observability 평균, 감사 액션) |
| kNN 벡터 검색 | `lab-knowledge`(`dense_vector` dims=8)에 6개 문서, 검색 벡터로 가장 가까운 3건 반환 |
| async search | `_async_search` 제출(`keep_on_completion=true`) → 폴링 → 삭제. 일례로 국가별 잔액 집계를 비동기로 실행 |

```bash
./lab.sh features            # 실행(반복 가능, kNN 인덱스는 매번 재생성)
./lab.sh features --clean    # 데이터스트림/템플릿/ILM/인덱스 제거
./all.sh es9 features        # 루트 통로
```

## ES9 장애 시뮬레이션

두 실습은 green 상태의 정확한 ES9 5노드 클러스터에서만 시작합니다. 실행 전용 canary 인덱스에 합성 문서 하나를 쓰고,
장애 상태를 관찰한 뒤 원래 노드 수·cluster UUID·green 상태와 문서를 검증합니다. canary의 owner marker를 확인한 다음
해당 인덱스만 삭제하며 기존 시드/사용자 인덱스는 변경하지 않습니다. 보고서는 `reports/drills/`에 권한 600으로 기록됩니다.

```bash
./lab.sh drills list
./lab.sh drills plan node-outage --node auto
./lab.sh drills run node-outage --node auto --hold 5 --yes
./lab.sh drills plan quorum-loss
./lab.sh drills run quorum-loss --hold 5 --yes
./lab.sh drills status
./lab.sh drills recover --yes   # 중단 뒤 active journal이 남은 경우
```

- `node-outage`: canary shard copy가 실제 배치된 es02–es05 중 한 노드를 정지하고, 생존 copy의 strict GET/search와
  4노드 상태를 확인한 뒤 같은 컨테이너를 복구합니다. `auto`는 실행 직전 대상을 고릅니다.
- `quorum-loss`: master-eligible 노드 5개 중 es02/es03/es04를 정지해 과반수 상실을 관찰한 뒤 세 노드를 복구합니다.
  es01은 host API 접속용으로 유지합니다.
- 실제 실행과 수동 복구에는 `--yes`가 필요합니다. 정상 종료/예외에서는 자동 복구를 시도합니다. SIGKILL/호스트 종료 뒤에는
  journal이 남을 수 있으므로 `status`로 확인하고 `recover --yes`를 실행하세요. 컨테이너 ID나 project/cluster identity가
  바뀌면 자동 기동·삭제를 거부합니다.
- 동일 실습의 복제본에서는 `recover`에도 실행 당시와 같은 project/prefix 설정을 사용해야 합니다. 이 도구는 로컬 교육용이지
  운영 클러스터용 chaos 도구가 아닙니다.

## 오프라인 테스트

```bash
./lab.sh offline-tests   # (== bash scripts/12-offline-tests.sh)
```

`tests/test_es9_defaults.py::test_no_cerebro_and_no_docker_only_network_opts`가 compose에
Cerebro가 없음을 정적으로 강제합니다.

## 검증 상태

### 2026-09-25 — 현재 작업 환경의 실제 실행

- Podman 5.8.7 + `podman-compose` 1.6.0에서 **새 Compose project/prefix/포트와 새 볼륨**을 사용해 ES 9.5.3 5노드를 기동했습니다.
  cluster green, 5/5 snapshot verification, Kibana `available` 및 설치 검증이 통과했습니다.
- 5MiB seed 6,017건, ES9 쿼리 예제 26개, `features`, `node-outage`, `quorum-loss` 실행과 자동 복구가 통과했습니다.
- 기존 `elasticsearch-9` project 라벨의 볼륨은 재사용하거나 삭제하지 않았습니다. 현재 기본 project 설정과 이전 볼륨을 연결하는
  마이그레이션/복구 절차는 이 시험에 포함되지 않습니다.
- Docker CLI는 설치되어 있지만 이 세션에서 daemon socket 권한 거부를 받았고 `docker compose` 플러그인도 없었습니다.
  따라서 이 날짜의 실행 증거는 Docker provider 검증이 아닙니다.
- `podman-compose`에서 `compose ps <service>`가 오류가 되는 것을 재현해 설치 실패 진단을 provider 호환 방식으로 수정했습니다.
  현재 수정본의 오프라인 테스트 결과와 상세 실행 경계는 저장소 품질/런타임 보고서를 함께 참조하세요.

### 2026-09-23 — 이전 실행 기록

- Docker Compose: 기본 설치·재실행 및 보안 모드 설치를 확인했습니다.
- Podman: `podman compose`로 ES 9 설치·재실행, green 상태, snapshot과 Kibana를 확인했습니다.
- 당시 별도 `podman-compose` 실행 파일은 검증되지 않았습니다.
- `./lab.sh verify-install`은 현재 컨테이너·클러스터·snapshot·Kibana 상태를 확인하고
  `reports/install-verification.json`에 결과를 기록합니다.

각 결과는 기록된 로컬 provider/호스트에 한정됩니다. WSL의 Podman 네트워크 조합과 기존 볼륨은 해당 호스트에서
`doctor`, `verify-install`, 상태/로그 확인으로 별도 검증하세요.

## 한계

- `XPACK_SECURITY_ENABLED=true`는 기동 시 ES 9 transport TLS 인증서를 생성합니다.
  `ELASTIC_PASSWORD`, `KIBANA_PASSWORD`, `KIBANA_LOGIN_USERNAME`,
  `KIBANA_LOGIN_PASSWORD`를 `.env`에 지정해야 합니다.
- 단일 명명 볼륨은 로컬 디스크 전용입니다. 여러 호스트로 옮기면 스냅샷 공유 FS가
  깨지므로, 분산 실습은 별도 FS를 마운트하세요.
- 포트 충돌 시 `ES_PORT`/`KIBANA_PORT`/`ES_BIND_IP`를 `.env`에서 바꾼 뒤 `ES_URL`도
  함께 맞춰야 합니다.
