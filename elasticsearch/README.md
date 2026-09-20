# Elasticsearch 5노드 + Cerebro — 시드·검색·샤드 실습 랩

**실제 데이터를 ZIP에 포함하지 않습니다.** `./lab.sh seed`를 실행하면 로컬에서 모의 데이터를 만들고 Elasticsearch에 스트리밍 Bulk 적재합니다. 외부 데이터셋 다운로드, 개인정보, 실제 금융 거래는 사용하지 않습니다.

## 바로 실행

Linux / Bash / Python 3.9+ / curl 7.76+ / Podman Compose 기준입니다. Python 패키지를 따로 설치할 필요는 없습니다.

```bash
# 이 README가 있는 프로젝트 디렉터리에서 실행
cp .env.example .env
chmod +x lab.sh scripts/*.sh scripts/*.py scenarios/*.sh

sudo sysctl -w vm.max_map_count=262144
./lab.sh doctor
./lab.sh up
./lab.sh seed
./lab.sh verify

./lab.sh query list
./lab.sh query 01-latest
./lab.sh query 04-high-risk
```

모든 관리 작업은 `lab.sh` 하나로 실행할 수 있습니다. 기존 번호별
`scripts/*.sh`와 `scenarios/*.sh`는 호환성을 위해 그대로 남아 있으며,
`lab.sh`가 해당 스크립트로 전달합니다.

일반 `up`은 호스트 방화벽을 수정하지 않습니다. 개발 컨테이너/VM의 네트워크 정책 때문에
노드 통신이 실패하면 관리자와 해당 네트워크·방화벽 경로를 진단하세요. 전체 FORWARD 허용 규칙을 자동 삽입하지 않습니다.

```bash
./lab.sh status
./lab.sh ui
./lab.sh demo                  # 5MiB smoke test
./lab.sh logs es01 --tail 100
./lab.sh scenario list
./lab.sh scenario 01
./lab.sh scenario manual-shard-move
./lab.sh scenario 06-node-failure-and-recovery --test --yes
./lab.sh fault list
./lab.sh down
```

`./lab.sh demo`는 클러스터를 시작하고 5MiB 데이터를 적재한 뒤 전체 검증까지
수행하는 가장 쉬운 정상 동작 확인 방법입니다. 기본 100MiB 데이터는
`./lab.sh seed`를 별도로 실행합니다. `./lab.sh ui`는 Elasticsearch, Cerebro,
Kibana와 Kibana Console 주소를 출력합니다.

기본 호스트 바인딩은 **`127.0.0.1`(loopback)** 입니다. Elasticsearch 9200, Cerebro 9000, Kibana 5601은 기본적으로 같은 호스트에서 접속합니다. Cerebro와 Kibana는 같은 Elasticsearch 클러스터를 동시에 사용합니다.

원격 접속은 SSH 터널을 우선 사용하세요. 격리된 네트워크에서 공개하려면 `.env`에 `ES_BIND_IP=0.0.0.0` 및 `ES_ALLOW_PUBLIC_BIND=yes`를 명시해야 합니다. 기존 공개 `.env`도 동의값이 없으면 기동을 거부합니다. 공개를 선택한 경우 다른 PC에서는 Cerebro `http://<서버IP>:9000`, Kibana `http://<서버IP>:5601`, Elasticsearch `http://<서버IP>:9200`에 접속하세요. 서버 자체에서는 `http://127.0.0.1:9000`, `http://127.0.0.1:5601`, `http://127.0.0.1:9200`을 사용할 수 있습니다. **`0.0.0.0/0`은 CIDR 대역 표기이며 바인딩 값이나 브라우저 접속 주소가 아닙니다.**

기존 프로젝트의 `.env`가 있다면 새 기본값보다 우선합니다. `KIBANA_PORT=5601`을 추가하고 `./lab.sh compose up -d kibana`를 실행하세요. 이미 존재하는 `.env`의 바인딩 설정까지 바꾸는 경우에는 `./lab.sh compose up -d --force-recreate es01 cerebro kibana`를 사용합니다. 포트 설정 적용에는 컨테이너 재생성이 필요하며 named volume은 보존됩니다.

`./lab.sh up`은 클러스터만 기동하고 시드는 자동 실행하지 않습니다. `./lab.sh verify`는 사용자의 **실제 Elasticsearch**에서 문서 수, 5개 이상의 데이터 노드, 샤드 배치, 검색 예제 22개를 검사합니다. 모든 샤드가 안정적인 green 상태여야 하므로 장애 시나리오를 진행하기 전에 실행하세요.

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

이전 랩이 실행 중이면 포트 9200/9000/5601이 겹칠 수 있습니다. 이전 랩을 종료하거나 `.env`의 `ES_PORT`, `CEREBRO_PORT`, `KIBANA_PORT`, `ES_URL`을 함께 변경하세요. 새 프로젝트명은 `cerebro-seed-lab`, 컨테이너 이름 접두사는 `cerebro-seed-`이며, 기존 볼륨을 자동 이관하거나 삭제하지 않습니다.

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

| 인덱스 | 기본 생성 확인 문서 수* | 원문 크기 | Primary | Replica 설정 | 전체 copy |
|---|---:|---:|---:|---:|---:|
| `lab-transactions-v1` | 66,796 | 약 50MiB | 12 | 1 | 24 |
| `lab-web-logs-v1` | 47,956 | 약 32MiB | 18 | 1 | 36 |
| `lab-audit-v1` | 27,888 | 약 18MiB | 8 | 2 | 24 |
| `lab-commerce-v1` | — | 약 12MiB | 16 | 2 | 48 |
| `lab-observability-v1` | — | 약 8MiB | 24 | 1 | 48 |
| **합계** | **약 142,000** | **약 100MiB** | **78** | — | **180** |

\* 제공 환경에서 기본 설정으로 생성한 결과입니다. 정확한 실행 결과는 `reports/seed-manifest.json`으로 확인하세요. 용량·기간·payload·seed 변경 시 문서 수는 달라집니다.

**100MiB는 `pri.store.size` 목표가 아닙니다.** 원문 JSON, Bulk 전송량, Primary Lucene 저장량, Replica 포함 저장량, translog까지 포함한 파일시스템 사용량은 서로 다릅니다. Primary/Replica 저장량은 `./lab.sh size`로 측정합니다. 많은 작은 샤드는 배치와 이동을 관찰하기 위한 의도적인 교육용 설정이며 운영 권장 샤드 크기를 뜻하지 않습니다.

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

## 자주 쓰는 명령

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
