# 시드 데이터 가이드

## 1. 무엇이 만들어지는가

실제 고객·거래 데이터가 아닌 **실습용 합성 데이터**입니다. 금융 거래, 웹/API 요청, 감사 이벤트, nested 주문, observability metric을 각각 별도 인덱스에 넣습니다. 정상 데이터만 무작위로 넣지 않고 알려진 이상 사례를 주기적으로 주입하므로 기본 검색 예제에 결과가 생깁니다. 비율과 위험도는 교육용 조건이지 금융 모델이나 탐지 품질의 검증 기준이 아닙니다.

| 인덱스 | 주요 필드 | 알려진 사례 |
|---|---|---|
| `lab-transactions-v1` | `transaction_id`, `user_id`, `institution`, `channel`, `amount`, `currency`, `country`, `risk_score`, `is_fraud`, `decision` | 약 5%: 해외 APP 거래, risk 850~1000, BLOCK, is_fraud=true |
| `lab-web-logs-v1` | `request_id`, `method`, `path`, `status`, `latency_ms`, `service`, `message`, `error_code` | 약 5%: payment 서비스 `/api/pay`, 502, upstream timeout, 지연 1500~7999ms |
| `lab-audit-v1` | `event_id`, `actor`, `user_id`, `action`, `result`, `target`, `privileged`, `message` | 약 5% LOGIN/FAIL, 다음 약 5% ROLE_CHANGE |
| `lab-commerce-v1` | `order_id`, `shipping.geo`, `items[]`, `payment`, `promotion` | nested 상품, geo_point 배송지, 객체 내부 검색 |
| `lab-observability-v1` | `metric_name`, `metric_value`, `host`, `service`, `labels`, `trace`, `histogram`, `alert` | metric histogram, trace sampled, anomaly alert |

공통 필드는 `@timestamp`, `document_id`, `event_seq`, `user_id`, `trace_id`, `src_ip`, `tags`, `scenario`, `message`, `payload`입니다. `src_ip`는 사설 IP 형태입니다. `payload`는 용량 실습용 해시 문자열이며 실제 토큰·암호·개인정보가 아닙니다. `_source`에는 저장하지만 검색과 doc values는 비활성화했습니다.

`user-00042`는 각 인덱스의 주입 사례에서 반복 등장합니다. `trace_id`도 인덱스 간 같은 순번을 비교하기 위한 합성 식별자입니다. 독립적으로 생성한 이벤트 시간을 실제 서비스의 인과관계나 정확한 분산 추적 시간으로 해석하지 마세요.

## 2. 매핑이 왜 필요한가

| 타입 | 이 랩의 예 | 주된 사용 |
|---|---|---|
| `keyword` | `user_id`, `service`, `action`, `institution` | 정확 일치, 그룹 집계, 정렬 |
| `text` | `message` | standard 분석기로 토큰화한 전문 검색 |
| `integer` / `long` | `risk_score`, `status`, `latency_ms`, `bytes` | 수치 범위와 집계 |
| `double` | `amount` | 수치 범위·합계. 실제 금액 설계에서의 정밀도 선택과는 별도 실습 |
| `boolean` | `is_fraud`, `privileged` | true/false 조건 |
| `date` | `@timestamp` | 시간 필터·정렬·기간 집계 |
| `ip` | `src_ip` | IP 형식과 IP 범위 검색 |

`mappings/*.json`에 명시적으로 정의되어 있으며 `dynamic: strict`입니다. nested, geo_point, object, multi-field, disabled doc_values를 함께 사용합니다. 오타 필드를 실수로 새 필드처럼 만들어버리는 것을 막습니다. 필드를 생략하는 것은 허용되지만 정의되지 않은 필드를 적재하면 오류가 납니다.

`service`는 이미 keyword이므로 `service.keyword`로 검색하지 않습니다. `message`에만 `message.keyword`라는 보조 필드가 있으며 `ignore_above=256`입니다. 긴 message 전체를 무조건 keyword로 검색할 수 있다는 뜻은 아닙니다.

## 3. 100MiB의 정확한 의미

기본 `--size-mb 100`은 이름과 달리 이진 단위 MiB를 사용합니다.

```text
목표 = 100 × 1024 × 1024 = 104,857,600 bytes
기준 = UTF-8 compact JSON _source 본문 + 각 본문의 LF 줄바꿈
분배 = 거래 40% / 웹 로그 25% / 감사로그 15% / 주문 12% / observability 8%
```

각 인덱스는 문서를 중간에 자를 수 없으므로 목표 크기를 마지막 문서 한 건 이내에서 넘깁니다. 제공 환경에서는 합계 104,858,904 bytes, 142,640건을 생성했습니다. 이 수치는 Elasticsearch에 저장한 후의 측정값이 아니라 **실제 생성한 원문 파일을 전수 검사한 결과**입니다.

Bulk action 메타데이터와 재시도 전송량은 위 기준에 포함되지 않습니다. Lucene은 저장·색인·압축 구조가 다르므로 `pri.store.size`가 원문과 같지 않습니다. Replica를 포함하는 `store.size`는 더 다른 값이며, translog 등을 포함한 전체 디스크 사용량과도 같지 않습니다.

```bash
./lab.sh size
```

`reports/seed-manifest.json`에는 생성 설정, 인덱스별 문서 수, 원문 바이트 수, Bulk 바이트 수, 완료 상태, 적재한 클러스터 UUID 등이 기록됩니다. 적재 완료 직후의 store 값은 이후 세그먼트 병합·복제 상태에 따라 달라질 수 있으므로 현재 값은 위 명령으로 확인합니다.

## 4. 기본 실행: 생성과 적재를 한 번에

```bash
./lab.sh seed
```

처음 실행하기 전에 다음 순서를 지키세요.

```bash
./lab.sh doctor
./lab.sh up
./lab.sh status
./lab.sh seed
./lab.sh verify
./lab.sh size
```

`seed`는 최소 5개 노드와 cluster UUID를 확인하고, 다섯 인덱스의 signature를 검사한 뒤
없는 인덱스를 생성합니다. 적재 중에는 refresh를 잠시 끄고 최대 500건/4MiB 단위로
Bulk 요청을 보냅니다. 각 Bulk item을 검사하고 429/502/503/504만 재시도한 다음,
refresh 복원·문서 수 확인·manifest 기록까지 수행합니다.

다음 출력은 정상적인 100MiB 기본 적재 결과입니다.

```text
[connect] http://127.0.0.1:9200; wait for >= 5 nodes
[lab-transactions-v1] complete: 66,796 docs, source=50.001 MiB
[lab-web-logs-v1] complete: 47,956 docs, source=32.001 MiB
[lab-audit-v1] complete: 27,888 docs, source=18.001 MiB
[DONE] 142,640 docs / 100.001 MiB source
[report] .../reports/seed-manifest.json
```

`generated=...`는 진행률, `complete`는 인덱스 단위 완료, `[DONE]`는 다섯 인덱스
적재 완료입니다. `[DONE]` 이후에도 `./lab.sh verify`를 실행해야 매핑·샤드·검색
예제까지 검증됩니다.

기본 실행은 100MiB짜리 로컬 파일을 만들지 않습니다. 요청 배치만 메모리에 유지합니다. 기본 배치는 최대 500건, 최대 4MiB이며 먼저 도달하는 기준으로 분할합니다.

전체 옵션은 다음 명령으로 확인합니다.

```bash
./lab.sh seed --help
```

주요 옵션: `--size-mb`는 다섯 인덱스 합계 `_source` 목표 MiB, `--seed`는 재현 가능한
난수 seed, `--start-date`/`--days`는 시간 범위, `--payload-bytes`는 비색인 payload,
`--batch-size`/`--max-batch-mb`는 Bulk 크기, `--generate-only`는 ES 없이 파일만
생성, `--recreate --yes`는 세 seed 인덱스를 삭제 후 재생성합니다.

## 5. 크기·날짜·부하 조절

새 데이터셋을 생성할 때:

```bash
./lab.sh seed --size-mb 5
./lab.sh seed --size-mb 100
./lab.sh seed --size-mb 300 --batch-size 300 --max-batch-mb 2
```

다른 설정의 시드가 이미 있다면 아래처럼 **삭제를 명시해야** 합니다.

```bash
# 거래·웹·감사 인덱스 3개 안의 모든 데이터가 삭제됩니다.
./lab.sh seed --size-mb 300 --recreate --yes

# 날짜를 변경한 새 시드. 기존 검색 예제의 고정 날짜 범위도 직접 조정해야 합니다.
./lab.sh seed --start-date 2026-09-01T00:00:00Z --days 7 --recreate --yes
```

`--seed`는 재현 가능한 난수 데이터 설정입니다. `--payload-bytes 0`은 추가 payload를 없애며, 같은 원문 크기를 맞추기 위해 문서 수가 늘어날 수 있습니다. 시간대 없는 날짜는 받지 않습니다. `Z` 또는 `+09:00` 같은 오프셋을 포함하세요.

과거 랩의 `DOCS_TRANSACTIONS`, `DOCS_WEB`, `DOCS_AUDIT` 기준은 이 버전에서 사용하지 않습니다. 데이터량은 `--size-mb` / `SEED_SIZE_MB`로 통일했습니다. 구버전 인덱스는 새 `_meta.seed_lab.signature`가 없으므로 기본 실행 시 안전하게 거부합니다.

## 6. 반복 실행과 부분 실패

같은 설정으로 반복하면 고정 `_id`에 `index` 동작으로 덮어씁니다. 중간 실패 후에도 같은 명령으로 재시도할 수 있으며 정상 처리된 문서가 새 ID로 중복 생성되지 않습니다. 단, 모든 배치를 다시 보내므로 재실행 시간과 쓰기 작업은 발생합니다.

HTTP 200만으로 Bulk 성공을 판정하지 않습니다. 응답의 각 item을 검사하고 429/502/503/504만 재시도합니다. 부분 실패이면 실패한 항목만 재시도합니다. 매핑 오류 같은 400은 이유를 출력하고 중지합니다. 이 동작은 Bulk API가 각 문서의 성공·실패를 별도로 반환한다는 점을 반영한 것입니다.

실패 시 성공한 일부 데이터는 남습니다. 트랜잭션처럼 전체 롤백하지 않습니다. manifest는 실패 상태로 기록하며 refresh 설정 복원을 시도합니다. 복원 자체가 실패하면 수동 복구 메시지를 출력합니다.

시드 외 문서를 같은 인덱스에 추가했거나 live load가 실행 중이면 정확한 문서 수 검증이 실패할 수 있습니다. 자동으로 추가 문서를 삭제하지 않습니다. live writer를 중지하고 실습 데이터를 정리하거나 `--recreate --yes`로 의도적으로 초기화하세요.

## 7. 파일만 생성해서 내용을 직접 보기

```bash
# ES가 꺼져 있어도 가능. 이 실행 모드에서만 로컬 원문 파일을 저장합니다.
./lab.sh seed --generate-only --size-mb 100

ls -lh datasets/generated/
head -n 2 datasets/generated/lab-transactions-v1.bulk.ndjson
```

파일은 action 한 줄, source 한 줄을 반복하는 **Bulk NDJSON**입니다. 전체 JSON 배열이 아닙니다. 각 줄과 마지막 줄에 LF가 있으며 원문 100MiB 외에 action 줄도 들어가므로 파일 합계는 100MiB보다 큽니다. 파일 생성만 한 상태는 적재 완료가 아닙니다. ES에 넣으려면 일반 시드 명령을 실행하세요. 생성 파일을 읽어 올리는 별도 importer가 아니라 동일 설정으로 다시 생성·스트리밍하는 구조입니다.

```bash
./lab.sh seed --size-mb 100
```

대용량 Bulk 파일 전체를 하나의 HTTP 요청으로 보내지 마세요. 이 랩의 배치 분할 경로를 사용합니다.

## 8. 실제 환경 검증

```bash
./lab.sh verify
```

manifest의 클러스터 UUID·시드 signature, 실제 문서 수, 5개 이상 data node, 인덱스별 shard/replica 설정, 78 primary + 102 replica가 서로 다른 노드에 STARTED인지 확인합니다. 26개 검색 예제도 실제 요청합니다. 정상일 때 `reports/live-verification.json`에 PASS를 기록합니다. 이 결과를 생성기 오프라인 테스트 결과와 혼동하지 마세요.

공식 근거: [Bulk API](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/docs-bulk.html), [Term query](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/query-dsl-term-query.html), [Match query](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/query-dsl-match-query.html).
