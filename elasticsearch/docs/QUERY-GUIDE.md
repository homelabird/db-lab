# Elasticsearch 검색 실습 — 처음부터 따라 하기

이 문서는 이 프로젝트의 실제 시드 필드와 `queries/*.json`을 사용합니다. 서버와 데이터를 먼저 준비한 뒤 **프로젝트 루트에서** 명령을 실행하세요.

```bash
./lab.sh up
./lab.sh seed
./lab.sh verify
```

기본 시드는 **2026년 8월 UTC 데이터**입니다. `now-15m`처럼 현재 시간 기준 조건을 처음부터 넣으면 결과가 0건일 수 있습니다. 아래 예제는 이 문제를 피하도록 전체 기간 또는 고정 날짜를 사용합니다.

## 1. 제일 먼저 실행해 볼 명령

```bash
./lab.sh query 01-latest
```

거래 인덱스에서 시간순으로 최근 10건을 조회합니다. 입력 요청만 보고 싶을 때:

```bash
./lab.sh query 01-latest --show-only
```

이 명령은 네트워크 요청 없이 HTTP method, path, JSON body를 출력합니다. 다른 예제도 `01`처럼 번호만 주거나 전체 이름을 줄 수 있습니다.

```bash
./lab.sh query list
./lab.sh query 03
./lab.sh query 10-by-institution
```

## 2. Elasticsearch는 어떻게 요청하는가

기본 형태는 **HTTP method + 인덱스/API 경로 + JSON 본문**입니다. 아래 예는 검색 요청이므로 POST를 사용하지만 데이터를 추가하지 않습니다. 마지막 `_search`가 검색 API라는 뜻입니다.

```text
POST /lab-transactions-v1/_search
{
  "size": 10,
  "query": { "match_all": {} }
}
```

Bash에서 직접 실행하려면 아래 명령을 사용합니다. 예제의 `127.0.0.1`은 **Elasticsearch가 실행 중인 서버에서 명령을 실행하는 경우**입니다. 다른 PC에서는 `export ES_URL=http://<서버IP>:9200`으로 바꿉니다. 기본 포트 바인딩은 `0.0.0.0`이지만 클라이언트의 접속 URL에 이 값을 넣지는 않습니다.

```bash
# .env에서 포트를 변경했다면 여기에도 해당 주소를 넣습니다.
export ES_URL=http://127.0.0.1:9200

curl --fail-with-body -sS -X POST "$ES_URL/lab-transactions-v1/_search?pretty" \
  -H 'Content-Type: application/json' \
  --data-binary '{
    "size": 10,
    "track_total_hits": true,
    "_source": {"excludes": ["payload"]},
    "query": {"match_all": {}},
    "sort": [{"@timestamp": "desc"}, {"document_id": "asc"}]
  }'
```

`?pretty`는 응답 JSON을 사람이 읽기 쉽게 꾸미는 옵션입니다. 검색 의미를 바꾸지 않습니다. `_source.excludes`는 응답에서 큰 payload를 제외하는 것이지 원본 필드를 삭제하는 동작이 아닙니다.

파일로 저장된 쿼리를 보내면 쉘 따옴표 실수를 줄일 수 있습니다.

```bash
curl --fail-with-body -sS -X POST "$ES_URL/lab-transactions-v1/_search?pretty" \
  -H 'Content-Type: application/json' \
  --data-binary @queries/01-latest.json
```

일반 터미널에 `GET /인덱스/_search`만 입력하면 실행되지 않습니다. 이는 HTTP 요청 표기이지 Bash 명령이 아닙니다. 터미널에서는 curl이나 제공 스크립트를 사용합니다.

### Cerebro REST 화면에서 실행

브라우저로 Cerebro에 들어가 등록된 클러스터를 선택한 다음 REST 화면을 엽니다. 다음 출력에서 method, path, body를 각각 옮겨 입력하세요.

```bash
./lab.sh query 04-high-risk --show-only
```

Method는 `POST`, path는 `/lab-transactions-v1/_search`, body는 JSON 객체입니다. JSON 본문 안에 `POST /...`를 같이 넣지 않습니다. JSON에는 `//` 주석이나 마지막 항목 뒤의 불필요한 쉼표를 넣지 마세요.

Cerebro는 API 요청을 보내고 결과를 보여주는 도구입니다. JSON Query DSL을 처리하고 실제 데이터를 검색하는 주체는 Elasticsearch입니다. Kibana는 이 랩에 설치되어 있지 않습니다.

## 3. 응답에서 어디를 봐야 하는가

| 응답 필드 | 의미 |
|---|---|
| `took` | Elasticsearch가 처리한 시간(ms). 클라이언트의 전체 네트워크 왕복시간과 같지 않음 |
| `timed_out` | 검색 시간 제한에 걸렸는지 여부 |
| `_shards.total/successful/failed` | 검색 대상 샤드와 처리 상태. 노드 수가 아님 |
| `hits.total.value` | 조건에 맞는 전체 문서 수 또는 그 하한 |
| `hits.total.relation` | `eq`: 정확한 값, `gte`: 표시값 이상 |
| `hits.hits` | 이번 응답에서 실제 반환한 문서 목록 |
| `_index`, `_id` | 문서가 들어 있는 인덱스와 고유 ID |
| `_score` | 관련도 점수. 점수를 계산하지 않는 조건이나 정렬 방식에서는 활용되지 않을 수 있음 |
| `_source` | 저장된 원문 문서. 요청의 source 필터가 적용될 수 있음 |
| `sort` | 정렬값. `search_after`의 다음 페이지 커서로 사용 |

`size: 10`은 조건에 맞는 문서 중 이번에 최대 10건을 돌려달라는 뜻입니다. 전체 데이터가 10건이라는 뜻이 아닙니다. 이 랩의 문서 조회 예제는 `track_total_hits: true`로 정확한 전체 건수를 요청합니다. 큰 데이터에서 정확한 total 계산에는 추가 비용이 있으므로 항상 켜야 하는 운영 기본값은 아닙니다.

HTTP 200이어도 `timed_out` 또는 `_shards.failed`가 있으면 완전한 검색 결과인지 확인해야 합니다. 제공 예제 실행기는 이 경우 실패로 처리합니다.

## 4. `keyword`에는 `term`: 정확 일치

특정 사용자 거래를 찾습니다.

```bash
./lab.sh query 02-user
```

본문의 핵심은 다음과 같습니다.

```json
{"query":{"term":{"user_id":"user-00042"}}}
```

`user_id`는 keyword 타입이므로 전체 값이 일치하는 문서를 찾습니다. 이 매핑에는 소문자 변환 normalizer가 없으므로 `USER-00042`와는 다릅니다. `channel: "APP"`, `action: "LOGIN"`도 시드의 실제 대소문자를 사용하세요.

필드 타입부터 확인하는 습관이 중요합니다.

```bash
./lab.sh query 21-mapping
```

**여기서는 `user_id.keyword`, `service.keyword`가 아닙니다.** 두 필드는 원래 keyword입니다. `.keyword`를 무조건 붙이지 마세요.

공식 근거: [Term query](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/query-dsl-term-query.html).

## 5. `text`에는 `match`: 전문 검색

```bash
./lab.sh query 03-message
```

```json
{
  "query": {
    "match": {
      "message": {"query":"payment timeout","operator":"and"}
    }
  }
}
```

`message`는 text 필드입니다. standard 분석기로 분리한 검색 토큰이 대상 문서에 있는지 검사합니다. 이 예제의 `operator: "and"`는 payment와 timeout이 모두 있어야 한다는 의미이며 두 단어가 바로 붙어 있어야 한다는 뜻은 아닙니다.

분석 결과도 직접 볼 수 있습니다.

```bash
./lab.sh query 17-analyze
```

`Payment Gateway Timeout`을 분석한 토큰을 확인하고, 입력의 대문자·공백이 어떻게 처리되는지 보세요. 시드 message는 추가 형태소 분석기 설치 없이 연습할 수 있도록 영어 로그 문장으로 작성했습니다. 한국어 검색 품질을 이 데이터셋으로 평가하지 않습니다.

문구와 순서를 고려하고 싶다면:

```bash
./lab.sh query 14-phrase
```

```json
{"query":{"match_phrase":{"message":"payment gateway timeout"}}}
```

`term`은 검색어를 분석하지 않습니다. 따라서 text 필드에 문장 전체를 term으로 넣으면 기대한 검색이 되지 않을 수 있습니다. 문장 전체의 정확 일치가 필요하면 이 랩의 짧은 message에서는 `message.keyword`를 검토할 수 있습니다.

공식 근거: [Match query](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/query-dsl-match-query.html).

## 6. `bool`: 여러 조건 결합

"APP 거래이면서 risk_score가 800 이상이고, 국가가 KR은 아닌 거래"를 찾습니다.

```bash
./lab.sh query 04-high-risk
```

```json
{
  "query": {
    "bool": {
      "filter": [
        {"term":{"channel":"APP"}},
        {"range":{"risk_score":{"gte":800}}}
      ],
      "must_not": [
        {"term":{"country":"KR"}}
      ]
    }
  }
}
```

| 조건 | 판단 방식 |
|---|---|
| `must` | 모두 만족해야 함. 관련도 점수에 기여할 수 있음 |
| `filter` | 모두 만족해야 함. 점수 계산 없이 조건 판단 |
| `must_not` | 해당 조건을 만족하는 문서 제외 |
| `should` | 선택 조건 또는 OR 조건. 필수 만족 수를 확인해야 함 |

수치·상태·기간처럼 점수보다 조건 만족 여부가 중요하면 filter가 편리합니다. filter를 쓴다고 모든 요청이 항상 캐시에 저장된다고 단정하면 안 됩니다.

### `should`를 OR로 썼는데 결과가 너무 많을 때

```bash
./lab.sh query 20-should
```

이 예제는 APP 거래 중 `is_fraud=true` 또는 `amount>=1000000`을 요구합니다. 핵심은 `minimum_should_match: 1`입니다.

bool에 must/filter가 함께 있으면 should의 기본 필수 개수는 0이 될 수 있습니다. 의도한 OR 조건을 필수로 만들려면 1을 명시하세요. should만 있는 bool의 기본과 혼동하지 마세요.

공식 근거: [Boolean query](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/query-dsl-bool-query.html).

## 7. 숫자와 시간 범위

```bash
./lab.sh query 08-http-5xx
./lab.sh query 06-time-range
```

```json
{"range":{"status":{"gte":500,"lt":600}}}
```

`gt`는 초과, `gte`는 이상, `lt`는 미만, `lte`는 이하입니다. 시간 범위는 다음처럼 시작 포함·끝 제외로 정의하면 일/월 경계에서 중복을 줄일 수 있습니다.

```json
{
  "query": {
    "range": {
      "@timestamp": {
        "gte":"2026-08-01T00:00:00Z",
        "lt":"2026-09-01T00:00:00Z"
      }
    }
  }
}
```

`Z`는 UTC입니다. `2026-08-01T00:00:00+09:00`은 한국 시간 자정이므로 UTC 자정과 다른 순간입니다. 이 랩의 기본 생성 구간은 UTC 기준입니다. 한국 시간의 월 전체가 필요하면 쿼리 경계도 한국 시간으로 정하세요.

시드를 `--start-date`로 바꿨다면 `queries/06-time-range.json`의 날짜도 수정합니다. 검색 결과 0건은 문법 오류와 다릅니다.

## 8. count, exists, 여러 값 중 하나

문서 본문은 필요 없고 고위험 건수만 필요하면:

```bash
./lab.sh query 09-count-risk
```

대상 경로가 `/_search`가 아니라 `/_count`이며 `count` 응답을 확인합니다.

필드에 검색 가능한 값이 존재하는 문서를 찾으면:

```bash
./lab.sh query 15-error-exists
```

이 예제는 `_source`에 키가 적혀 있는지만 확인하는 일반 JSON 검사와 다릅니다. exists는 필드가 색인된 값으로 존재하는지 판단합니다. 값이 null이거나 매핑에서 색인되지 않는 등 상황에 따라 원문에 키가 있어도 결과가 다를 수 있습니다.

여러 행위 중 하나를 찾는 `terms` 예:

```bash
./lab.sh query 22-privileged
```

`term`은 단일 값, `terms`는 배열로 전달한 값 중 하나가 일치하는 조건으로 이해하고 시작하면 됩니다.

## 9. 그룹 집계와 평균·합계

기관별 KRW 거래 건수, 거래 합계, 평균 위험도를 조회합니다.

```bash
./lab.sh query 10-by-institution
```

```json
{
  "size":0,
  "query":{"term":{"currency":"KRW"}},
  "aggs":{
    "by_institution":{
      "terms":{"field":"institution","size":10},
      "aggs":{
        "total_amount":{"sum":{"field":"amount"}},
        "avg_risk":{"avg":{"field":"risk_score"}}
      }
    }
  }
}
```

`size: 0`은 집계만 필요하므로 개별 hits를 반환하지 않도록 합니다. 집계 대상 데이터가 0건이라는 의미가 아닙니다. 결과의 `aggregations.by_institution.buckets`를 봅니다. 각 bucket의 `key`는 기관명, `doc_count`는 문서 수, `total_amount.value`는 합계입니다.

**통화가 다른 금액을 그냥 더하지 않도록 KRW로 제한했습니다.** 실제 환산율 처리나 회계 정밀도를 구현한 데이터셋은 아닙니다.

여기서 terms aggregation은 값을 묶는 집계입니다. 8절의 terms query는 검색 조건으로 용도가 다릅니다. 집계는 keyword 필드인 institution에 수행합니다. message 같은 text 필드를 그대로 그룹 집계하다가 fielddata 오류가 났다고 무조건 fielddata를 켜지 마세요.

terms aggregation은 기본적으로 상위 bucket을 반환합니다. 고유값이 많은 필드에서는 `size`, `sum_other_doc_count`, 오차 관련 필드도 확인해야 하며 "모든 그룹을 완전히 반환"한다고 가정하면 안 됩니다. 이 랩의 기관은 4종이고 size=10이라 학습이 단순합니다.

공식 근거: [Terms aggregation](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/search-aggregations-bucket-terms-aggregation.html).

## 10. 응답시간 P95/P99, 날짜별 집계

```bash
./lab.sh query 11-latency
./lab.sh query 12-daily
./lab.sh query 13-audit-actions
```

11번은 서비스별 평균과 50/95/99 percentile을 반환합니다. payment 서비스에 느린 오류 로그를 일부러 넣었으므로 평균만 보는 것과 percentile을 함께 보는 차이를 관찰할 수 있습니다. percentile 집계는 근사 알고리즘이므로 정확한 정렬 순위값과 항상 동일하다고 가정하지 마세요.

12번은 `date_histogram`의 `calendar_interval: "day"`와 `time_zone: "Asia/Seoul"`로 일 단위 bucket을 만듭니다. `min_doc_count: 0`은 결과 범위 사이의 빈 bucket도 표시하게 하지만, 데이터 밖의 임의 기간 전체를 반드시 생성해주는 설정은 아닙니다. 그 경우 extended_bounds 등을 별도로 지정해야 합니다.

13번은 action으로 묶고 그 안에서 result로 다시 묶는 하위 집계입니다. action별 성공·실패 건수를 확인합니다.

공식 근거: [Date histogram](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/search-aggregations-bucket-datehistogram-aggregation.html), [Percentiles aggregation](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/search-aggregations-metrics-percentile-aggregation.html).

## 11. 여러 인덱스에 걸친 사용자 검색

```bash
./lab.sh query 19-user-timeline
```

경로에 인덱스를 쉼표로 연결하여 한 번에 검색합니다.

```text
POST /lab-transactions-v1,lab-web-logs-v1,lab-audit-v1/_search
```

공통 `user_id`로 user-00042를 찾고 시간순으로 봅니다. `_index`를 보면 각 문서가 거래·웹·감사 중 어디에서 왔는지 구분할 수 있습니다. 이는 여러 문서를 함께 조회한 것이며 관계형 JOIN을 수행한 것은 아닙니다.

여러 인덱스를 검색할 때 같은 이름의 필드가 서로 다른 타입이면 검색·정렬·집계가 실패할 수 있습니다. 이 랩은 공통 필드의 타입을 맞추었습니다.

## 12. 검색은 노드와 샤드를 어떻게 사용하는가

```bash
./lab.sh query 16-search-shards
./lab.sh scenario 12
```

거래 인덱스에는 12개 primary shard 그룹이 있습니다. 특별한 routing이나 가지치기가 없는 일반 검색은 이 샤드 그룹들을 대상으로 하고 각 그룹의 primary 또는 replica copy 중 선택된 copy가 요청을 처리할 수 있습니다. 응답의 `_shards` 수를 "노드 5개" 또는 "primary와 replica를 모두 더한 copy 수"로 읽지 마세요.

Replica가 있다고 같은 논리 문서가 검색 결과에 복제 수만큼 중복되어 나타나는 것은 아닙니다. `_search_shards`는 검색 대상 그룹과 후보 배치를 보는 API이며 모든 표시된 copy가 그 검색을 동시에 처리했다는 증거가 아닙니다. 샤드 이동·고장 상황에서는 일부 그룹 처리 실패 여부도 확인해야 합니다.

공식 근거: [Search shard routing](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/search-shard-routing.html), [Search shards API](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/search-shards.html).

## 13. 페이지 조회: from/size와 PIT + search_after

작은 결과에서 두 번째 10건을 조회하려면:

```bash
curl --fail-with-body -sS -X POST "$ES_URL/lab-transactions-v1/_search?pretty" \
  -H 'Content-Type: application/json' --data-binary '{
    "from":10,"size":10,
    "_source":{"excludes":["payload"]},
    "sort":[{"@timestamp":"desc"},{"document_id":"asc"}],
    "query":{"match_all":{}}
  }'
```

from은 0부터 시작합니다. 깊은 페이지로 갈수록 비용이 커지고 기본 설정에서 `from + size`는 10,000 제한을 받습니다. 이 제한을 무턱대고 늘리는 대신 안정적인 대량 페이지 조회에는 PIT + search_after를 사용해봅니다.

```bash
./lab.sh pit --pages 3 --page-size 10
```

스크립트의 흐름은 다음과 같습니다.

```text
1. POST /lab-transactions-v1/_pit?keep_alive=1m  → PIT id
2. POST /_search → pit, sort, size 지정
3. 응답 마지막 hit의 sort 배열을 다음 요청의 search_after로 그대로 전달
4. 응답에 새 pit_id가 있으면 다음 요청에 최신 ID 사용
5. 완료/오류 시 DELETE /_pit → PIT 닫기
```

PIT 검색의 경로는 `/_search`이며 인덱스 경로를 다시 붙이지 않습니다. 스크립트는 `@timestamp`, `_shard_doc`로 정렬합니다. 같은 PIT에서 `_shard_doc`은 동률을 구분할 수 있는 정렬값을 제공하며, 응답의 sort 배열 전체를 유지합니다. PIT를 열어 둔 채 잊으면 자원을 계속 붙잡을 수 있어 finally에서 닫도록 작성했습니다.

기본값은 전체 14만 건 내보내기가 아니라 **10건씩 3페이지 확인**입니다.

공식 근거: [Paginate search results](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/paginate-search-results.html).

## 14. 쿼리 검증과 성능 관찰

```bash
./lab.sh query 18-validate
```

`_validate/query?explain=true`는 Query DSL 조건의 유효성을 확인하는 도구입니다. 전체 검색 요청의 aggregation·sort까지 모든 부분을 대신 실행해 검증하는 것은 아닙니다. valid=true라도 실제 데이터가 조건에 맞아야 결과가 나옵니다.

실제 예제 전체 실행:

```bash
./lab.sh query all --save-dir reports/query-results
```

첫 실행과 반복 실행의 took은 캐시, JIT, 부하, 디스크 상태 등에 따라 달라질 수 있습니다. `took` 한 번만으로 성능을 결론내리지 마세요. `_search`에 `profile: true`를 넣어 내부 실행을 관찰하는 방법도 있지만 측정 부하가 있어 이 기본 예제에는 항상 켜두지 않았습니다.

## 15. 선택 실습: 문서 생성·수정·삭제

시드 검증 건수를 깨뜨리지 않도록 **별도 `lab-query-scratch` 인덱스**를 사용합니다. 아래는 새 scratch 인덱스에서 한 번씩 실행하는 연습입니다. 이미 같은 인덱스가 있으면 생성 요청이 실패할 수 있으니 자동으로 지우지 말고 기존 내용을 먼저 확인하세요.

```bash
curl --fail-with-body -sS -X PUT "$ES_URL/lab-query-scratch" \
  -H 'Content-Type: application/json' --data-binary '{
    "settings":{"number_of_shards":1,"number_of_replicas":1},
    "mappings":{"properties":{
      "message":{"type":"text"},"status":{"type":"keyword"}
    }}
  }'

# ID=demo-1인 문서 생성. 같은 ID로 PUT을 다시 보내면 전체 원문을 대체.
curl --fail-with-body -sS -X PUT "$ES_URL/lab-query-scratch/_doc/demo-1?refresh=wait_for" \
  -H 'Content-Type: application/json' \
  --data-binary '{"message":"payment gateway timeout","status":"OPEN"}'

# ID로 직접 읽기: 전체 검색과 별도 API
curl --fail-with-body -sS "$ES_URL/lab-query-scratch/_doc/demo-1?pretty"

# 일부 필드 업데이트: _update의 doc 아래에 변경할 필드를 지정
curl --fail-with-body -sS -X POST "$ES_URL/lab-query-scratch/_update/demo-1?refresh=wait_for" \
  -H 'Content-Type: application/json' --data-binary '{"doc":{"status":"CLOSED"}}'

# 문서 한 건 삭제
curl --fail-with-body -sS -X DELETE "$ES_URL/lab-query-scratch/_doc/demo-1?refresh=wait_for"

# 실습용 scratch 인덱스 자체 삭제: 이 안의 데이터도 전부 삭제
curl --fail-with-body -sS -X DELETE "$ES_URL/lab-query-scratch"
```

`refresh=wait_for`는 변경이 검색에 보이는 시점까지 기다리도록 합니다. 대량 적재의 매 문서마다 refresh를 강제하는 방식과 구분하세요. refresh는 검색 가시성을 위한 개념이며 디스크 영속성과 완전히 같은 말이 아닙니다.

scratch가 존재하는 동안 클러스터 전체 샤드 수는 시드의 84개보다 많습니다. `lab.sh verify`는 시드 인덱스 3개만 대상으로 84개를 검사합니다.

공식 근거: [Index API](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/docs-index_.html), [Update API](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/docs-update.html), [refresh parameter](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/docs-refresh.html).

## 16. 자주 하는 실수 정리

| 증상 | 먼저 확인할 것 |
|---|---|
| `index_not_found_exception` | 시드 적재 여부, 인덱스 이름, ES_URL |
| 검색 0건 | 고정 데이터 날짜와 쿼리 날짜, 대소문자, `.keyword` 존재 여부 |
| 본문에 10건만 보임 | size와 hits.total은 다름 |
| message term 검색이 안 됨 | text 분석과 term/match 구분 |
| should 조건을 안 만족해도 검색됨 | minimum_should_match 확인 |
| 집계에 fielddata 오류 | text가 아닌 keyword 대상인지 확인 |
| JSON parse 오류 | Content-Type, 따옴표, 마지막 쉼표, 본문에 HTTP 요청행을 같이 넣었는지 확인 |
| 깊은 페이지에서 오류 | from+size 제한과 PIT/search_after 사용 검토 |
| 쿼리 실행은 되는데 일부 결과만 옴 | timed_out, _shards.failed, 클러스터 장애 상태 확인 |
| 적재 직후 검색이 안 보임 | refresh 가시성과 시드 완료 여부 확인 |

전체 장애 진단은 [TROUBLESHOOTING.md](TROUBLESHOOTING.md), 실습 문제는 [EXERCISES.md](EXERCISES.md)를 참고하세요.
