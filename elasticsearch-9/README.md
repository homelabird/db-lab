# Elasticsearch 9 랩 (es9) — 5노드 클러스터 + Kibana 9

Elasticsearch **9.5.x** 기반 5노드 클러스터와 Kibana 9를 올리는 실습 랩입니다.
legacy 7.x 랩(`elasticsearch/`)과 **같은 공용 코어**(`../lib/es-lab/`)를 공유하며,
범위는 **코어 스택 + 스냅샷 저장소 + 시드(현실적) 데이터 + 쿼리 검증**입니다.
장애 시나리오/샤드/시드 토폴로지 드릴은 legacy 랩 전용입니다. **Cerebro는 의도적으로
포함하지 않습니다**(legacy 7.x 보조 UI용) — 이 랩은 Kibana로 화면 검색/포인 매니지 합니다.

- Docker(+Compose v2)로 검증했습니다. podman-compose는 compose 파일을 평범한 YAML
  앵커/브릿지로 유지해 해석 가능하지만 로컬 검증은 하지 못했습니다.
- 기본 포트: Elasticsearch **9201**, Kibana **5602**. legacy 7.x 랩(9200/9000/5601)과
  충돌하지 않아 두 랩을 동시에 기동할 수 있습니다.
- 기본 보안은 꺼져 있으며(무인증, loopback 바인딩), `XPACK_SECURITY_ENABLED=true`는
  옵트인입니다(아래 한계 참고).

## 바로 실행

```bash
# 이 디렉터리에서
cp .env.example .env
chmod 600 .env
chmod +x lab.sh scripts/*.sh

./lab.sh doctor     # 엔진/도구/메모리 사전 진단
./lab.sh up         # 콜드 부트: 5노드 green + 스냅샷 저장소 등록·검증 + Kibana
./lab.sh seed --size-mb 5   # 실제 분포의 시드 데이터 적재(빠른 smoke)
./lab.sh verify     # 문서 수·샤드 레이아웃·쿼리 예제 26종 검증
./lab.sh features   # ES9 현대 기능: 데이터스트림+ILM, ES|QL, kNN, async search
./lab.sh query list # 쿼리 예제 목록
./lab.sh status     # 노드/샤드 상태
./lab.sh ui         # 접속 URL 출력
./lab.sh snapshot   # 스냅샷 저장소 재검증
./lab.sh down
```

루트에서도 쓸 수 있습니다(공용 배치에는 미포함, opt-in).

```bash
cd ..
./all.sh es9 doctor
./all.sh es9 up
./all.sh es9 seed --size-mb 5
./all.sh es9 verify
./all.sh es9 status
./all.sh es9 health
./all.sh es9 snapshot
./all.sh reset es9 --yes   # 볼륨까지 삭제
```

## 구조

```
compose.yaml            ES 9.5.3 x5 + Kibana 9.5.3, YAML 앵커로 노드 중복 최소화
lab.sh                  subset 래퍼: doctor/up/down/status/ui/logs/compose/snapshot/
                        seed/verify/size/purge/query/offline-tests
scripts/common.sh       공용 코어 로더 (LAB_ROOT/LAB_CONTAINER_PREFIX=es9-lab- 설정 후 ../lib/es-lab/common.sh source)
scripts/lablib.py       공용 lablib_core 임포트 + INDICES/LAYOUT + bulk/catalog 헬퍼
scripts/generate_and_load.py  시드 생성·적재 (../lib/es-lab/datagen/realistic 공용 생성기 위임)
scripts/verify_seed.py  시드·샤드·쿼리 예제 26종 라이브 검증
scripts/features.py     ES9 전용 현대 기능: 데이터스트림+ILM, ES|QL, kNN, async search
scripts/01-up.sh        ［기동: 5컨테이너 → yellow 5노드 → 스냅샷 저장소 → Kibana］
scripts/12-offline-tests.sh  정적 회귀 테스트(실행 불필요)
mappings/*.json         5개 시드 인덱스 매핑 (legacy와 동일 스키마)
queries/*.json          쿼리 예제 26종 (legacy와 동일 카탈로그)
tests/test_es9_defaults.py   compose/.env/공용 코어 배선에 대한 정적 테스트
tests/test_es9_seed.py       생성기·매핑 타입·양 랩 동일성·카덴스 정적 테스트
tests/test_es9_features.py   features 스크립트·공용 카탈로그 비오염·kNN 픽스처 정적 테스트
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

## 오프라인 테스트

```bash
./lab.sh offline-tests   # (== bash scripts/12-offline-tests.sh)
```

`tests/test_es9_defaults.py::test_no_cerebro_and_no_docker_only_network_opts`가 compose에
Cerebro가 없음을 정적으로 강제합니다.

## 검증 기록(최신 버전 정상 구동)

`reports/live-verification.json`은 실제 기동 후 기록한 결과입니다. 최근 확인(2026-09-22):

- `./lab.sh doctor` — 엔진/자원/포트 진단 OK
- `./lab.sh up` — 콜드 부트: 5노드 전원 합류(`es9-lab`, Elasticsearch 9.5.x) + 스냅샷 저장소 검증 + Kibana 9
- `./lab.sh status` — 78 primary + 102 replica 전원 STARTED(5노드 샤드 분산)
- `./lab.sh snapshot` — `lab-snapshots` 저장소 5노드 verify
- `./lab.sh seed --size-mb 5` + `./lab.sh verify` — 시드 적재·26개 쿼리 예제 전부 PASS
- `./lab.sh features` — 데이터스트림+ILM, ES|QL 5종, kNN top-1 일치, async search 제출·폴링·삭제 전부 PASS(`reports/features.json`), `--clean`→재생성 재현 확인
- `./lab.sh down` — 정상 종료(볼륨 보존)

`docker.elastic.co/elasticsearch/elasticsearch:9.5.3` 이미지 기준이며, 재시작은
`discovery.seed_hosts`로 재합류합니다. 랩 완성은 7.x legacy로 검증한 기능
(롤링 업그레이드·장애 드릴 등)만 제외됩니다.

## 한계

- **보안 옵트인은 TLS 미포함**: `XPACK_SECURITY_ENABLED=true`로 올리면 HTTP는 비활성화
  되지만 ES 8/9 멀티노드는 transport TLS도 요구합니다. 이 랩은 TLS 구성을 제공하지
  않으므로 보안 켜기는 실습 범위 밖이며, 필요하면 별도 구성 가이드를 따라야 합니다.
- 단일 명명 볼륨은 로컬 디스크 전용입니다. 여러 호스트로 옮기면 스냅샷 공유 FS가
  깨지므로, 분산 실습은 별도 FS를 마운트하세요.
- 포트 충돌 시 `ES_PORT`/`KIBANA_PORT`/`ES_BIND_IP`를 `.env`에서 바꾼 뒤 `ES_URL`도
  함께 맞춰야 합니다.