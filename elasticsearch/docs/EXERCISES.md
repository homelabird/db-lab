# 검색 연습문제 10개와 정답

기본 100MiB 시드 적재와 `lab.sh verify`를 마친 상태에서 시작합니다. JSON을 직접 작성한 다음 정답 예제와 비교하세요. 문제의 결과 건수는 데이터량에 따라 달라지므로 고정 숫자를 외우기보다 조건과 응답 구조를 확인합니다.

## 문제

| 번호 | 요구사항 | 확인할 개념 |
|---|---|---|
| 1 | 거래 인덱스 최근 10건을 보고 payload는 제외한다 | sort, size, source filtering |
| 2 | user-00042의 거래만 찾는다 | keyword + term |
| 3 | 웹 message에 payment와 timeout이 모두 있는 문서를 찾는다 | match + operator=and |
| 4 | APP, risk_score 800 이상, KR 제외 조건을 모두 만족하는 거래를 찾는다 | bool.filter, must_not, range |
| 5 | 감사로그에서 LOGIN 실패만 찾는다 | 여러 term 조건 |
| 6 | 원문을 받지 않고 고위험 거래 전체 건수를 확인한다 | _count |
| 7 | 기관별 KRW 거래 합계와 평균 위험도를 구한다 | query + terms aggregation + metrics |
| 8 | 서비스별 평균·P95·P99 응답시간을 구한다 | nested aggregations, percentile |
| 9 | 한국 날짜 기준 일별 웹 요청량을 구한다 | date_histogram, time_zone |
| 10 | PIT를 사용해 거래 10건씩 3페이지를 중복 없이 가져오고 PIT를 닫는다 | search_after, PIT 수명 |

## 정답 실행과 해설

### 1. 최근 10건

```bash
./lab.sh query 01-latest
cat queries/01-latest.json
```

`@timestamp desc`, 동률일 때 `document_id asc`를 사용합니다. `hits.total`은 전체 조건 일치 건수이고 `hits.hits`는 이번 페이지입니다. payload가 응답에서 없다고 저장되지 않은 것은 아닙니다.

### 2. 특정 사용자

```bash
./lab.sh query 02-user
```

`term`의 필드는 `user_id`입니다. `user_id.keyword`로 바꾸지 않습니다.

### 3. 문장 검색

```bash
./lab.sh query 03-message
./lab.sh query 17-analyze
```

두 단어가 바로 붙어 있을 필요는 없습니다. phrase 검색을 요구한다면 14-phrase와 비교하세요.

### 4. 복합 조건

```bash
./lab.sh query 04-high-risk
```

filter 안의 APP와 range 조건은 AND이고 must_not은 KR을 제외합니다. size=10이어도 `hits.total.value`로 전체 조건 일치 건수를 확인할 수 있습니다.

### 5. 로그인 실패

```bash
./lab.sh query 05-failed-login
```

action과 result는 대문자 keyword입니다. 소문자로 검색했을 때의 결과도 비교하세요.

### 6. 건수만 확인

```bash
./lab.sh query 09-count-risk
```

`/_count`의 count를 봅니다. 검색 결과 hits 목록을 가져와 클라이언트에서 세는 방식이 아닙니다.

### 7. 기관별 거래 집계

```bash
./lab.sh query 10-by-institution
```

서로 다른 통화를 합산하지 않도록 KRW filter가 있습니다. `aggregations.by_institution.buckets` 안의 key, doc_count, total_amount, avg_risk를 읽습니다.

### 8. 지연시간 집계

```bash
./lab.sh query 11-latency
```

평균은 전체 응답시간 분포를 한 수치로 압축합니다. 의도적으로 주입한 느린 payment 로그가 상위 percentile에 어떻게 반영되는지 봅니다. 정확한 수치는 실제 실행 결과를 사용하세요.

### 9. 일별 요청량

```bash
./lab.sh query 12-daily
```

기본 생성 기간이 UTC이고 bucket은 Asia/Seoul이므로 첫날·마지막 날 구간의 건수가 가운데 날짜와 다를 수 있습니다. 표본 날짜 범위도 직접 확인하세요.

### 10. PIT 페이지 조회

```bash
./lab.sh pit --pages 3 --page-size 10
```

각 응답 마지막 hit의 sort 배열 전체를 그대로 다음 요청으로 넘깁니다. 최신 pit_id를 이어 사용하고 종료 시 닫아야 합니다. 코드에는 페이지 간 중복 ID 검사도 있습니다.

## 추가 실험: 잘못 작성한 쿼리 찾아 고치기

아래 요청은 APP만 필수이고 should는 기본값 때문에 필수가 아닐 수 있습니다.

```json
{
  "query": {
    "bool": {
      "filter": [{"term":{"channel":"APP"}}],
      "should": [
        {"term":{"is_fraud":true}},
        {"range":{"amount":{"gte":1000000}}}
      ]
    }
  }
}
```

APP 중 "사기 모의 거래 또는 100만원 이상"을 원했다면 bool 안에 `minimum_should_match: 1`을 추가합니다.

```bash
./lab.sh query 20-should --show-only
```

공식 의미는 `QUERY-GUIDE.md`의 각 절에 연결된 Elasticsearch 7.17 문서로 확인할 수 있습니다.
