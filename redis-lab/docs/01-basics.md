# 01 · Redis 기본 명령

모든 셸 명령은 프로젝트 루트에서 실행합니다. Redis CLI 내부의 `127.0.0.1:6379>` 표시는
프롬프트이며 복사할 명령의 일부가 아닙니다.

## 문자열, 카운터, 만료

```bash
./lab.sh cli master SET lab:practice:greeting hello
./lab.sh cli master GET lab:practice:greeting
./lab.sh cli master TYPE lab:practice:greeting
./lab.sh cli master EXISTS lab:practice:greeting
./lab.sh cli master INCR lab:practice:login-count
./lab.sh cli master SET lab:practice:session token123 EX 60
./lab.sh cli master TTL lab:practice:session
```

예상: SET은 OK, GET은 hello, TYPE은 string입니다. TTL은 남은 초입니다.
TTL `-1`은 키는 있지만 만료가 없다는 뜻이고, `-2`는 키가 없다는 뜻입니다.
세션 생성 후 60초가 지나면 GET이 비어 보이고 TTL은 -2가 됩니다.
`--raw` CLI는 nil을 빈 줄로 표시할 수 있습니다. 빈 문자열과 키 없음은 EXISTS/TYPE으로 구분하세요.

실습: 같은 키에 다시 `SET`하면 기존 만료가 유지되는지 관찰하세요.
유지하려면 `SET ... KEEPTTL`을 비교하세요. 저장 전에 설정했던 EXPIRE가 항상 따라간다고 가정하지 마세요.

## Hash, Set, Sorted Set, Stream

```bash
./lab.sh cli master HSET lab:practice:user name tester tier silver risk 30
./lab.sh cli master HGETALL lab:practice:user
./lab.sh cli master HINCRBY lab:practice:user risk 10
./lab.sh cli master SADD lab:practice:seen u1 u2 u1
./lab.sh cli master SCARD lab:practice:seen
./lab.sh cli master ZADD lab:practice:ranking 70 u1 90 u2 80 u3
./lab.sh cli master ZREVRANGE lab:practice:ranking 0 2 WITHSCORES
./lab.sh cli master XADD lab:practice:events '*' action login uid u1
./lab.sh cli master XRANGE lab:practice:events - + COUNT 5
```

Set의 중복 원소는 늘어나지 않습니다. Sorted Set의 member는 고유하며 score로 정렬합니다.
셸에서 `*`를 쓸 때는 작은따옴표로 감싸세요. 셸이 파일명으로 확장하면 Redis에 다른 인자가 전달됩니다.

## 리스트와 blocking 요청

```bash
./lab.sh cli master LPUSH lab:practice:queue task1 task2
./lab.sh cli master RPOP lab:practice:queue
./lab.sh cli master BLPOP lab:practice:empty-queue 10
```

다른 터미널에서 `INFO clients`를 보며 blocked_clients를 관찰합니다.
이것은 Redis 전체 프로세스가 멈췄다는 의미가 아니라, 특정 클라이언트가 blocking 명령을 기다리는 상태입니다.

## 탐색과 메모리

```bash
./lab.sh cli master SCAN 0 MATCH 'lab:practice:*' COUNT 100
./lab.sh cli master MEMORY USAGE lab:practice:user
./lab.sh cli master INFO memory
./lab.sh cli master DBSIZE
```

SCAN이 반환한 다음 cursor를 다음 호출에 넣고 **cursor=0으로 돌아올 때까지** 반복해야 합니다.
COUNT는 엄격한 결과 개수 제한이 아니라 힌트이며, 순회 중 변경되는 데이터는 중복/누락 가능성에 유의하세요.
일반적인 대량 키 탐색에서 전체를 한 번에 처리하는 KEYS보다 SCAN 기반 순회를 연습합니다.

자동 생성 데이터:

```bash
./lab.sh seed --users 2000 --payload-bytes 256
./lab.sh cli master HGETALL lab:user:100
./lab.sh cli master TTL lab:session:100
./lab.sh cli master ZREVRANGE lab:ranking:risk 0 9 WITHSCORES
./lab.sh cli master XLEN lab:events:transactions
```

사용자 한 명당 Hash/세션을 만들고, 공용 순위·활성 사용자 집합·최대 약 10,000건 Stream에 데이터를 추가합니다.
seed 재실행은 동일 사용자 키를 덮어쓰고 세션 TTL을 갱신하며 Stream에는 새 이벤트를 추가합니다.
삭제/초기화 없이 실행할 때 이 차이를 확인하세요.

참고: 공식 명령과 자료구조 문서는 [REFERENCES](../REFERENCES.md)의 R3/R4/R5를 보세요.
