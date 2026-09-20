# DB 시뮬레이션 v5 — 재고·결제 원장·환불의 트랜잭션 정합성

작성일: 2026-09-19. **구현 및 호스트 테스트와 실제 MariaDB 검증을 구분합니다.** 제작 환경에는 Docker가 없어 실제 InnoDB 실행은 하지 않았습니다. 시험용 SQLite 어댑터는 MariaDB의 행 잠금·격리·내구성을 대신 증명하지 않습니다.

## 1. 공부할 대상

DB가 연결되고 HTTP가 성공해도 재고, 주문, 결제 기록은 서로 틀릴 수 있습니다. 이번 실습은 같은 입력으로 **일부러 잘못 만든 비교군(unsafe)**과 **보호된 구현(protected)**을 실행하고, 오류 증상과 최종 행을 나란히 검사합니다.

새 상시 서비스를 추가하거나 기본 주문 API에 결제 기능을 붙인 것이 아닙니다. 준비된 MVP의 MariaDB와 같은 실제 이미지 ID로 **별도 임시 MariaDB와 client**를 만들고, 빈 `txlab` schema의 합성 데이터만 사용합니다. 기본 구성은 여전히 MariaDB·Kafka·Elasticsearch·Redis·API·worker, 총 6개입니다. 새 실습의 잘못된 주문은 기본 Kafka·검색·캐시에 전달하지 않습니다.

구매자·판매자 잔액은 같은 MariaDB 안의 **가짜 지갑**입니다. 카드사/PG 호출, 외부 환불, 금전 이동은 없습니다. 단일 SQL 트랜잭션의 원자성·동시성 학습이지 분산 결제·exactly-once·Saga 구현이 아닙니다.

## 2. 설치와 실행

전체 소스를 적용하며 기존 `.env`, `.state`, 데이터 볼륨을 보존합니다. `all.sh`만 교체하지 않습니다. 기존 v4 `messages --keep` 자원이 있으면 생성 당시 API가 살아 있을 때 해당 run을 inspect/cleanup하고 나서 API를 다시 빌드하세요. API ID가 바뀐 자원의 자동 승계는 지원하지 않습니다.

프로젝트 루트에서:

```bash
bash ./all.sh mvp init
bash ./all.sh mvp doctor
bash ./all.sh mvp up
bash ./all.sh mvp smoke

# Docker/DB를 변경하지 않고 목록과 계획 확인
bash ./all.sh mvp drills list
bash ./all.sh mvp drills plan stock-race --clients 4 --seed 42

# 같은 실험 안에서 unsafe/protected를 모두 비교
bash ./all.sh mvp drills run stock-race --clients 4 --seed 42 --yes
```

`list/plan`은 DB를 연결하지 않습니다. 루트 래퍼가 로컬 실행 영수증을 남길 수는 있습니다. `run`은 이미 준비된 **로컬 Linux Docker + Compose v2**만 대상으로 하며 원격 Docker/Podman을 자동 조작하지 않습니다. 기존 engine pin·프로젝트·API 설정·정상 스택을 확인합니다. 준비가 안 됐다고 시뮬레이터가 원본을 자동 초기화하지 않습니다.

`drills`는 기존 5개에서 **11개**가 됐습니다. `simulate` 13개, `messages` 6개는 별도로 유지됩니다. 신규 여섯 실습에는 `drills prepare`의 netem helper가 필요하지 않습니다.

## 3. 여섯 비교 실습

| 이름 | 의도적으로 잘못된 구현 | 보호된 구현 | 통과에 필요한 관측 |
|---|---|---|---|
| `stock-race` | 같은 재고를 읽은 여러 연결이 과거 값으로 덮어씀 | 재고 조건을 포함한 원자적 차감 | 재고 1개에 unsafe 초과 주문, protected 1건 성공·나머지 품절 |
| `duplicate-checkout` | 같은 키의 요청을 매번 새 구매로 처리 | UNIQUE 요청 기록과 구매를 같은 트랜잭션에 저장 | unsafe 중복 주문, protected 동일 주문 1건·잔액 차감 1회 |
| `checkout-rollback` | 단계마다 commit 후 다음 단계 수행 | 여섯 단계와 요청 기록 전체를 한 트랜잭션으로 처리 | 각 중간 실패에서 unsafe 잔여 변경, protected 원상태와 동일·같은 키 재시도 성공 |
| `commit-ambiguity` | commit 후 응답을 받지 못했다고 새 구매를 실행 | 같은 키 읽기 확인과 멱등 재요청 | unsafe 이중 차감, protected 원래 주문 재사용·단회 차감 |
| `idempotency-conflict` | 키만 같으면 다른 수량에도 이전 응답 반환 | 키와 요청 내용의 fingerprint를 함께 검사 | unsafe 요청/응답 의미 불일치, protected 충돌 거절·추가 변경 없음 |
| `refund-race` | 여러 연결이 과거 `paid` 상태만 보고 각각 환불 | 주문 행 잠금 후 상태를 확인하고 단회 전이 | unsafe 중복 환불·재고 초과, protected 환불 1회·반복은 재응답 |

표는 구현한 목표 관측과 기준입니다. 실제 MariaDB에서 이미 관찰한 결과표가 아닙니다.

### A. 마지막 재고를 여러 요청이 동시에 구매

```bash
bash ./all.sh mvp drills run stock-race --clients 4 --seed 42 --yes
```

초기 재고는 1개입니다. 잘못된 비교군에서는 읽기 이후 barrier로 연결들을 맞춰, 모두 같은 과거 재고를 보고 나서 UPDATE하도록 합니다. 최종 재고가 0이라도 주문이 4개면 정합성이 깨집니다. 보호된 구현은 `available >= quantity` 조건을 포함해 차감하고, 영향받은 행이 없으면 품절로 처리합니다.

이는 위험한 순서를 **의도적으로 만드는 비교 실험**입니다. 실제 서비스에서 해당 경합이 일어날 확률이나 처리량을 측정한 결과가 아닙니다. 보호된 구현은 잠금을 잡은 상태에서 이 읽기 barrier를 기다리지 않습니다.

### B. 결제 재시도와 같은 키의 다른 요청

```bash
bash ./all.sh mvp drills run duplicate-checkout --clients 4 --yes
bash ./all.sh mvp drills run idempotency-conflict --yes
```

동일 키·동일 수량 요청은 같은 주문을 반환해야 합니다. 동일 키로 수량을 1에서 2로 바꾸면 새로운 주문이나 예전 성공 응답이 아니라 `idempotency_conflict`가 되어야 합니다. 재고·잔액만 일치해도 잘못된 수량의 성공 응답은 별도 계약 위반으로 판정합니다.

`requests`의 복합 PRIMARY KEY가 동일 키의 실행을 중복 생성하지 못하게 하고, 해당 기록·주문·원장·이벤트를 함께 commit합니다. 일반 SQL에서 발생한 아무 `1062` 오류나 정상 재시도로 간주하지 않고, 요청 기록 INSERT의 중복 오류만 해석합니다.

### C. 어느 지점에서 실패해도 rollback되는가

```bash
bash ./all.sh mvp drills run checkout-rollback --yes
```

한 실험에서 다음 여섯 경계 각각에 명시적 Python 예외를 넣습니다.

```text
재고 차감 → 구매자 차감 → 판매자 증가 → 주문 INSERT → 원장 INSERT → 이벤트 INSERT → COMMIT
```

각 경계마다 잘못된 비교군과 보호된 구현을 새 case로 실행하므로 총 12개 비교 결과가 생깁니다. 보호된 구현은 실패 직후 **여섯 테이블의 모든 행이 실패 전 상태와 같아야** 하고, 같은 키로 다시 실행하면 한 주문만 저장되어야 합니다. DDL/schema 생성은 구매 트랜잭션 밖에서 수행합니다. MariaDB DDL은 implicit commit을 일으킬 수 있습니다.[S3]

마지막 outbox 경계의 잘못된 비교군은 데이터 자체는 모두 저장돼 정합성 검사를 통과할 수도 있습니다. 그래도 호출자는 실패를 받았는데 부분 commit 방식으로 이미 변경이 남았다는 사실을 따로 기록합니다. 모든 단계별 잔여 변경을 같은 종류의 데이터 손상이라고 부르지 않습니다.

### D. 타임아웃이 곧 rollback은 아님

```bash
bash ./all.sh mvp drills run commit-ambiguity --yes
```

이번 주입은 **DB commit 성공을 확인한 직후 앱에서 응답 소실을 나타내는 예외를 발생**시키는 방식입니다. 실제 TCP 연결 단절, API HTTP 타임아웃, 브로커 장애는 발생시키지 않습니다. 보고서에도 `simulated=true`를 기록합니다.

잘못된 비교군은 다시 구매하여 같은 요청으로 두 주문을 만듭니다. 보호된 구현은 읽기 전용 키 조회로 저장된 주문을 확인한 다음 같은 키로 재요청하여 기존 주문을 돌려줍니다. 조회 자체는 주문·캐시를 만들거나 수정하지 않습니다.

실제 드라이버의 commit에서 예외가 발생하면 결과를 `commit_unknown`으로 남깁니다. rollback됐다고 단정하거나 새 키로 자동 재결제하지 않습니다. 이 드라이버 경계는 호스트 테스트에서 commit 적용/미적용 두 모의 경우를 검사했으며, 실제 네트워크 장애 시험은 아닙니다.

### E. 동시에 들어오는 환불

```bash
bash ./all.sh mvp drills run refund-race --clients 4 --yes
```

먼저 단일 구매를 만든 뒤 같은 주문에 여러 환불을 요청합니다. 잘못된 비교군은 모두 `paid`를 읽은 상태에서 진행하여 같은 금액과 재고를 반복 반환할 수 있습니다. 보호된 구현은 **트랜잭션 안에서 `SELECT … FOR UPDATE`**로 주문을 잠그고 상태를 재확인합니다. 최초 요청은 환불하고, 뒤의 요청은 이미 환불된 주문으로 응답합니다.[S1, S2]

실제 외부 결제사의 환불 API는 이 SQL 잠금에 포함되지 않습니다. 이 구현을 그대로 PG 멱등성이나 이종 시스템 정합성 해결책으로 일반화하지 마세요.

## 4. 검사하는 데이터와 판정

임시 `txlab`에는 `inventory`, `wallets`, `requests`, `orders`, `ledger`, `events` 여섯 테이블이 있습니다. 마지막 `events`는 구매·환불과 함께 저장하는 **로컬 outbox 역할**이며 Kafka로 보내지 않습니다.

감사기는 성공 응답 수만 세지 않고 모든 행을 읽습니다. 핵심 기준은 다음과 같습니다.

```text
초기 재고 = 남은 재고 + 환불되지 않은 주문의 수량 합
구매자 + 판매자 잔액 합 = 초기 잔액 합
각 지갑 잔액 = 그 지갑의 초기 잔액 + 원장 증감 합
각 주문 = 정확히 대응하는 차감/증가 원장 + 상태별 이벤트
같은 요청 키 = 한 주문 + 동일한 요청 내용
```

음수 잔액·재고 범위 초과·고아 원장·고아 이벤트·누락 이벤트·중복 환불·잘못된 이벤트 내용까지 검사합니다. 합계가 우연히 맞아도 개별 주문의 원장과 내용이 틀리면 실패합니다. 모든 연결이 끝난 뒤 한 repeatable-read 스냅샷으로 비교하며, 끝나지 않은 연결이 있으면 일관된 최종 관측으로 처리하지 않습니다.

호스트 컨트롤러가 저장된 행을 다시 검사하여 helper의 감사 결과와 비교합니다. 이는 결과 누락이나 서로 모순되는 보고서를 잡는 방어선이며, 악의적인 helper 인증이나 두 번째 실제 DB 조회는 아닙니다.

| 판정/종료 코드 | 의미 |
|---|---|
| `passed` / 0 | 잘못된 비교군의 목표 증상을 확인하고 보호된 구현의 행·동작 계약을 모두 확인 |
| `failed` / 1 | 보호된 구현 불일치, 예상 밖 오류, 잘못된 증거, 관측·정리 실패 |
| `inconclusive` / 2 | 보호된 구현은 맞지만 잘못된 비교군의 목표 경합·증상을 충분히 관측하지 못함 |
| `aborted` / 130 | 사용자 중단; 가능한 범위에서 자신이 만든 임시 자원 정리 |

**실습의 passed는 unsafe 구현이 안전하다는 뜻이 아닙니다. 실패 사례를 실제로 드러내고 보호된 동작과 구분했다는 뜻입니다.** 원본의 정상 데이터까지 고쳐 줬다는 의미도 아닙니다.

## 5. 옵션과 자원 범위

`--clients`는 2~8, 기본 4입니다. 경합 시나리오의 동시 SQL 클라이언트 수이며, rollback/commit/내용 충돌 실습은 순차 실행합니다. `--seed`는 0~2147483647이고 fixture 단가를 `100 + seed % 101`로 정합니다. UUID·스레드 순서·실제 지연·DB 스케줄링을 고정하는 옵션은 아닙니다. 두 옵션은 이번 여섯 `drills`에만 적용됩니다.

금액은 소수 없는 합성 정수 단위입니다. 통화·세금·수수료·환율·외부 잔액을 모델링하지 않습니다. 같은 입력의 여러 실험을 성능 순위로 비교하지 마세요.

helper 실행 한도 75초, SQL 연결/읽기/쓰기 timeout 3초, lock-wait 2초, 한 case의 테이블당 최대 1,000행, trace 최대 4,096건, helper 출력 최대 8MiB입니다. 75초에는 컨테이너 생성·이미지 준비·DB 기동·정리가 포함되지 않습니다. 잠금 timeout/데드락의 명시적 오류만 rollback과 close 후 제한적으로 재시도하고, commit 불명확 오류는 자동 재시도하지 않습니다.

임시 MariaDB는 메모리 한도 768MiB, datadir 512MiB tmpfs, client는 메모리 256MiB를 사용합니다. 호스트의 여유 메모리를 인증하는 수치가 아니며 tmpfs도 메모리를 소비합니다. 기존 안전 검사로 로컬 Linux Docker의 메모리·swap 지원과 총 메모리 6GiB 이상을 확인하지만, 여유가 없는 호스트에서 여러 실습을 동시에 실행하지 마세요.

## 6. 결과와 정리

각 실행의 출력에 `report_directory`가 나오며 기본 위치는 아래와 같습니다.

```text
mvp-lab/reports/drills/drill-<실제실행ID>/
    summary.json          전체 상태, 관측, 실패 단계, 정리 결과
    transaction.json      입력 계획, 전체 행, 비교 판정, 재시도·조회 결과
    report.md             unsafe/protected 주문·재고·잔액·위반 비교표
    trace.jsonl           요청, 단계별 SQL 경계, commit/rollback/replay 순서
    source-containers.json
    source-manifest.json
```

helper가 구조화된 결과를 만들기 전에 실패하면 transaction/report가 없고 summary의 부분 관측만 남을 수 있습니다. `.env`나 engine pin이 없으면 실행에 진입하기 전 CLI 오류만 있을 수도 있습니다. `checkout-rollback` 표는 **실패 직후** 상태이며, 재시도 후 결과는 `recovered_snapshot`/`recovered_audit`로 확인합니다. trace 시간은 클라이언트 관측값이지 DB 내부 실행시간이나 성능 측정값이 아닙니다.

클라이언트는 새 MariaDB의 loopback network namespace만 공유하고, MariaDB 자체는 `network none`으로 생성됩니다. 원본 네트워크·named volume·호스트 디렉터리를 연결하지 않습니다. 원본 DB 데이터는 복사하지 않으며 기본 웹 화면에 이 실험의 잘못된 결제/재고가 표시되지 않는 것이 정상입니다. 관측은 파일 보고서로 합니다.

정상 종료 시 소유권을 확인한 임시 DB/client를 지우고 tmpfs만 사라집니다. 보고서는 보존됩니다. 실패나 강제 종료 뒤 marker가 남으면:

```bash
bash ./all.sh mvp drills recover --yes
```

engine pin, 프로젝트/run, container/image ID, 네트워크·마운트·자원 한도를 확인합니다. 다른 컨테이너, named volume/bind mount 연결 등 계획과 다른 자원은 자동 삭제하지 않습니다. `rm -v`, volume prune, 원본 초기화는 사용하지 않습니다. 원본 전용 데이터 복구 명령이 아니라 **실습 임시 자원 정리**입니다. `--keep`으로 이 임시 DB를 보존하는 기능은 제공하지 않습니다.

## 7. 검증 명령과 미검증 범위

```bash
# 실제 DB 없는 전체 호스트 회귀
bash ./scripts/test-offline.sh

# 준비된 로컬 Docker MVP에서 신규 여섯 실습을 순차 실행
# 임시 DB/client를 생성·정리하며 실패 또는 inconclusive에서 멈춥니다.
bash ./scripts/test-runtime-transactions.sh --yes
```

실제 경로는 항상 PyMySQL과 MariaDB를 사용하며 런타임 부재 시 SQLite로 대체하지 않습니다. 테스트 전용 `tests/transaction_sqlite.py`는 SQL placeholder, DDL, session 명령과 `FOR UPDATE`를 변환합니다. 일부 시험은 읽기 barrier를 만들기 위해 첫 DML 전 읽기를 SQLite 트랜잭션 밖에서 수행하고, 쓰기는 SQLite의 DB 단위 잠금을 사용합니다. 따라서 테스트 통과는 MariaDB READ COMMITTED/MVCC, 유니크 키 대기, 행 잠금, 데드락, 실제 연결 끊김, fsync·재시작 내구성의 증거가 아닙니다.

실제 Docker/고정 이미지/PyMySQL/InnoDB 인수 시험은 남아 있습니다. 외부 결제·Sagas, 다중 DB 원자성, 분산 worker, HTTP 결제 UI, 원본 API에 재고 연결, 영구 원장 감사, 물리 백업/PITR, HA·성능·RTO/RPO는 이번 작업 범위가 아닙니다. 기존 v2/v3/v4 기록의 테스트 수는 해당 버전의 과거 결과입니다.

## 공식 동작 계약 참고

2026-09-19 확인. 문서 조회는 고정 이미지 실행 검증이 아닙니다.

[S1] MariaDB FOR UPDATE: 트랜잭션 및 autocommit 조건.
https://mariadb.com/docs/server/reference/sql-statements/data-manipulation/selecting-data/for-update

[S2] MariaDB START TRANSACTION: commit/rollback, READ ONLY, consistent snapshot.
https://mariadb.com/docs/server/reference/sql-statements/transactions/start-transaction

[S3] MariaDB implicit commit 문장: DDL과 구매 트랜잭션의 분리 근거.
https://mariadb.com/docs/server/reference/sql-statements/transactions/sql-statements-that-cause-an-implicit-commit

[S4] MariaDB INSERT ON DUPLICATE KEY UPDATE: PRIMARY/UNIQUE 키의 중복 식별 의미 참고. 이번 보호된 구현은 무조건 upsert하지 않고 해당 claim INSERT의 1062만 별도 처리합니다.
https://mariadb.com/docs/server/reference/sql-statements/data-manipulation/inserting-loading-data/insert-on-duplicate-key-update
