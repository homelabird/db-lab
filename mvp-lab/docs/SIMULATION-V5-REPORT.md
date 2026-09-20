# DB 시뮬레이션 v5 구현·검증 보고서

**작성일: 2026-09-19 · 입력: `db-lab-simulation-v4.zip` · 출력: `db-lab-simulation-v5.zip`**

입력 SHA-256: `0a83227bfa7f4cad20f23aec1c18d5d2ecc59d565ef78548a608ce9f74e0b972`

## 1. 결과 요약

기존 `drills`에 재고 경합·중복 결제·여섯 경계 rollback·commit 결과 불명확·동일 키/다른 내용·동시 환불 비교 실습 6개를 추가했습니다. 잘못된 구현의 증상을 실제로 관측하고 보호된 구현의 주문·재고·지갑·원장·이벤트를 대조해야 성공입니다. 기존 13개 `simulate`, 6개 `messages`, 5개 `drills`는 유지하여 명령상 총 30개 시나리오입니다. 30개 모두를 실제 DB에서 실행했다는 뜻이 아닙니다.

**전체 호스트 회귀 1,133개와 별도 독립 재검사 19개가 통과했습니다. 실제 Docker/MariaDB 기동·행 잠금·트랜잭션·정리는 미검증입니다.** 새 여섯 실습의 예시는 실제 runner/repository에 명시적인 SQLite SQL·스케줄 어댑터를 연결한 시험입니다. MariaDB 결과로 표시하지 않습니다.

사용자 호스트·실행 중인 DB에 적용하지 않았으며 원본 ZIP은 보존했습니다. 결과물은 첨부 프로젝트의 수정 패키지입니다. 새 실습을 사용할 때에도 원본 MVP 데이터가 아니라 별도 임시 DB에 합성 데이터를 씁니다.

## 2. 구현 내용

| 기능 | 잘못된 비교군 | 보호된 구현/관측 |
|---|---|---|
| stock-race | 재고 읽기 이후 barrier로 과거 값을 공유, stale absolute UPDATE | 조건부 원자적 차감, 재고 1개에 주문 1개·나머지 품절 |
| duplicate-checkout | 같은 키를 매번 구매로 처리 | 요청 PRIMARY KEY와 주문을 같은 txn에 기록, 동일 주문·단회 차감 |
| checkout-rollback | 재고/차감/증가/주문/원장/이벤트 경계에서 각각 부분 commit | 6개 failpoint 각각 원상태 동일·동일 키 정상 재시도 |
| commit-ambiguity | commit 후 명시적 응답 소실 예외에 무조건 새 구매 | 읽기 전용 키 조회·같은 키 재요청으로 이미 저장한 주문 재사용 |
| idempotency-conflict | 같은 키로 수량을 바꿔도 기존 성공 응답 반환 | 요청 fingerprint 비교, 추가 데이터 변경 없이 명시적 충돌 |
| refund-race | 동시에 읽은 paid 상태로 각각 환불 | 트랜잭션 안의 FOR UPDATE·단회 상태 전이·재시도 응답 |

단일 DB에 합성 구매자·판매자 지갑을 둡니다. 외부 결제사/PG, 분산 트랜잭션, Saga, 실제 통화·세금 모델은 아닙니다. 일반 주문 API·worker·Compose·이미지 기본값·기존 HA 실습·Helm은 변경하지 않았습니다. `events`는 임시 DB 내부 outbox이며 원본 Kafka와 연결하지 않습니다.

### 판정 강화

감사기는 여섯 테이블의 행을 읽어 재고·금액 보존, 지갑/원장 일치, 요청키/주문 단일성, 정확한 차감·환불 분개, 이벤트 수와 내용, 고아 기록을 검사합니다. 단순 합계가 맞아도 잘못된 개별 원장을 허용하지 않습니다. 호스트에서 helper가 반환한 행을 다시 계산하여 저장된 감사와 일치하는지도 확인합니다. helper 신원 인증이나 두 번째 DB 재조회 기능은 아닙니다.

`idempotency-conflict`는 회계 수치가 일치해도 요청 의미가 다르면 별도 계약 위반을 관측합니다. 마지막 outbox 경계의 부분 commit은 내부 수치가 일치해도 호출 실패 뒤 변경이 남는 것으로 구분합니다. 모든 실패를 데이터 유실/손상으로 일반화하지 않습니다.

`passed`는 unsafe의 특정 문제와 protected의 정합성·동작을 확인한 실습 성공입니다. protected가 실패하면 `failed`, 잘못된 비교군의 효과가 관측되지 않으면 `inconclusive`입니다. 정리 실패도 최종 성공을 허용하지 않습니다.

### 트랜잭션과 오류 처리

보호된 구매는 요청 claim·재고·두 지갑·주문·원장·이벤트를 함께 commit합니다. 1205/1213의 제한적 재시도는 rollback과 close 이후만 수행합니다. commit 단계의 연결/응답 오류는 `commit_unknown`으로 남기고 자동 재구매하지 않습니다. DDL은 checkout 밖에서 수행합니다. commit-ambiguity 주입은 성공 commit 뒤의 Python 예외이며 실제 네트워크 패킷 소실이 아닙니다.

## 3. 격리·자원·정리

로컬 Linux Docker + Compose v2 및 기존 engine pin, 프로젝트, 실제 API 설정, 정상 의존성을 검사합니다. 원본 MariaDB와 같은 실제 image ID의 새 DB에 빈 `txlab`를 생성합니다. 원본 DB 데이터는 읽어 복사하지 않습니다. 정상 스택의 health/preflight 읽기와 컨테이너 metadata 조회는 수행하므로 원본에 아무 요청도 없다는 의미는 아닙니다.

새 DB는 network none, datadir 512MiB tmpfs, memory768MiB이며 client는 해당 DB loopback namespace만 공유하고 memory256MiB입니다. 원본 network/named volume/bind mount를 연결하지 않습니다. 총 메모리 검사(6GiB 이상)는 여유 메모리 인증이 아니고 tmpfs도 메모리를 소비합니다.

동시 SQL client 2~8, helper75초, SQL timeout3초, lock-wait2초, trace4096건, 테이블당 case별1000행, helper출력8MiB 상한을 적용합니다. seed는 fixture 가격만 정하며 UUID·스레드 순서·실제 시간을 고정하지 않습니다.

기존 `drill-active.json`에 생성 의도를 먼저 기록하고, 정리 전에 engine/project/run/container/image/network/mount/resource 한도를 재검사합니다. 삭제는 소유한 임시 DB/client만 대상으로 하며 원본 볼륨 삭제·prune·강제 reset은 없습니다. SIGKILL/호스트 종료 뒤에는 helper/clone이 남을 수 있어 `drills recover --yes`로 소유권을 확인해 정리합니다. 외부 watchdog이나 `--keep` 임시 DB 유지 기능은 추가하지 않았습니다.

## 4. 실행한 검증

| 묶음 | 테스트 수 | 결과/범위 |
|---|---:|---|
| 루트 | 91 | 라우팅/보호/제한된 Go chart 계약, 실제 Helm 아님 |
| MVP | 489 | v4 385 + 신규104; mock/HTTP/SQLite 어댑터 |
| Elasticsearch | 77 | 기존 호스트 검사 |
| Kafka | 94 | 기존 호스트 검사 |
| MariaDB | 198 | 기존 호스트 검사 |
| Redis | 184 | 기존 호스트 검사 |
| **전체 호스트 회귀** | **1,133** | **실패·skip 없음** |
| 독립 기존 V01~V08 및 흐름 재검사 | 19 | 별도 통과, 위 합계에 포함하지 않음 |

신규104개는 SQL 경로33, 순수 행 감사/입력33, 시나리오 실행기14, CLI/격리/결과 검사24입니다. 반복 실행과 subtest를 부풀려 합산하지 않았습니다. 최종 `full/summary.json`이 전체 집계 기준입니다.

정적 검사는 Python108개, Bash57개(그중 POSIX sh5개 추가 검사), 일반 YAML24개, UI JavaScript1개 통과입니다. 일반 YAML 파싱은 Compose/Kubernetes schema 검증이 아니며 Helm templates는 제외했습니다. MariaDB `SHA256SUMS`와 Redis `MANIFEST.sha256`도 통과했습니다.

여섯 시나리오를 같은 시험 어댑터로 다시 실행해 예시 보고서를 보관했습니다. `evidence_kind=TEST-ONLY-SQLITE-SQL-AND-SCHEDULE-ADAPTER-NOT-MARIADB` 표시와 Markdown 첫 경고가 있으며 테스트 수에는 다시 더하지 않았습니다.

### 명령 사전 검사

임시 소스 사본에서 수행했으며 시험용 `.env`/engine pin/marker는 배포하지 않습니다.

| 명령 | 관측 |
|---|---|
| drills/simulate/messages list | 종료0, 각11/13/6개 |
| 같은 stock-race plan 두 번 | 종료0, 출력 동일 |
| --yes 누락 | 종료2, 거부 |
| clients9 | 종료1, 범위 거부 |
| init 두 번 | 종료0/0, 기존 .env 해시 유지 |
| doctor / stock-race run --yes | 종료1, 런타임 없음 |
| 신규 test-runtime-transactions.sh --yes | **종료127, Docker 없음** |
| 기존 test-runtime-mvp/messages 및 test-helm | 종료127, 도구 없음 |

### 환경과 초기 오류

호스트 Python3.13.5에서 수행했습니다. 대상 앱 이미지 Python3.12의 의존성 설치/실행 호환성은 확인하지 못했습니다. 환경의 Python startup hook 영향을 배제하려 `-S`, 설치 site-packages의 PYTHONPATH, python3 PATH shim을 사용했습니다. shell/driver/DB 호출을 실제 성공으로 바꾸는 runtime fallback은 없습니다.

초기 runner 시험의 refund-race 1건은 SQLite 스케줄 어댑터가 FOR UPDATE를 제거하면서 보호된 읽기에 대응하는 잠금도 만들지 않아 실패했습니다. 어댑터에서 해당 읽기에 SQLite의 coarse write-intent lock을 적용한 뒤 통과했습니다. 이 수정이 실제 InnoDB 행 잠금 검증이라는 뜻은 아닙니다. 최초 로그를 보존하고 production 판정 기준은 완화하지 않았습니다.

첫 SQL 시험 호출은 잘못된 작업 디렉터리로 import 오류가 났고 올바른 `mvp-lab`에서 재실행했습니다. 전체 묶음 한 번은 도구 시간 제한으로 Kafka 검사 중 종료되어 해당 로그를 보존한 뒤 Kafka를 별도 재실행했습니다. 초기 YAML 검사기는 multi-document YAML에 단일 문서 loader를 써서 두 파일을 오류로 분류했고 `safe_load_all`로 바로잡았습니다. 이들은 프로젝트 DB 실패를 숨긴 것이 아니라 호출/시험 어댑터/검사기 문제이며 초기·최종 자료를 구분합니다.

## 5. 실제로 확인하지 못한 것

Docker·Podman·Helm·kubectl·ShellCheck·MariaDB 서버가 없습니다. 패키지 저장소 접속도 DNS 오류로 실패했으며 패키지 설치나 외부 호스트 연결은 하지 않았습니다. 실제 이미지 pull/build, PyMySQL 대 MariaDB 연결, schema DDL, UNIQUE 키 경합, InnoDB READ COMMITTED/MVCC, FOR UPDATE, 실제1205/1213, commit 응답 소실, fsync·재시작 내구성, 임시 컨테이너 cleanup은 미검증입니다.

SQLite 어댑터는 placeholder/DDL/session 명령을 변환하고 FOR UPDATE 대신 SQLite DB 단위 write-intent lock을 사용합니다. 읽기 barrier를 위한 일부 SELECT는 첫 DML 전 트랜잭션 밖에서 실행합니다. 이는 실제 MariaDB의 transaction/row-lock 의미가 아닙니다. fake 1062·commit 적용/미적용 오류 역시 실제 서버/네트워크 관측이 아닙니다.

따라서 호스트 통과 수나 예시의 시간 값을 DB 성능·인수 완료·HA·RTO/RPO로 해석할 수 없습니다. 정상 실행은 실제 MariaDB만 사용하며 없는 경우 실패합니다.

## 6. 적용·실환경 인수

기존 `.env/.state/volumes`를 보존하고 전체 소스에서 API/worker 이미지를 다시 빌드합니다. 기존 messages의 보존 자원은 API 재생성 전에 확인·정리합니다. 기존 engine pin을 삭제하거나 새 암호로 바꿔 보호를 우회하지 않습니다.

```bash
bash ./all.sh mvp doctor
bash ./all.sh mvp up
bash ./all.sh mvp smoke
bash ./all.sh mvp drills plan stock-race --clients 4 --seed 42
bash ./all.sh mvp drills run stock-race --clients 4 --seed 42 --yes
# 준비된 로컬 스택에서 새 여섯 실습 순차 인수
bash ./scripts/test-runtime-transactions.sh --yes
```

실제 시험에서 각 negative control의 목표 증상을 관측하고 protected의 전체 행/동작 계약, server version/image ID, 정상 cleanup을 확인해야 합니다. 실패나 inconclusive면 그 자리에서 중단하며 원본 초기화로 문제를 가리지 않습니다.

## 7. 산출물과 변경 범위

v4 원본408개 파일은 삭제 없이 보존합니다. 기존3개 파일(루트README, MVP README, tools/advanced.py)을 수정하고 신규11개를 추가해 최종419개 파일입니다. 새 runtime script는 실행 권한을 포함합니다. API/worker의 정상 서비스 코드, 기존 DB별 실습과 Helm 서비스 파일은 그대로입니다. ZIP 재압축 해제 후 MVP를 재실행하고, 원본에 patch 적용한 파일 내용·권한이 ZIP과 일치하는지도 검사합니다. 정확한 hash와 최종 재검사 결과는 증거의 `archive-integrity.json`, `source-diff.json`, `archive-retest/`가 기준입니다.

가이드: `mvp-lab/docs/TRANSACTION-DRILLS.md`. 핵심 구현: transaction_model.py(판정), transaction_sql.py(실SQL), transaction_drill.py(실습), tools/advanced.py(격리 실행). 단위 테스트/예시/로그/정적 검사/CLI 결과와 초기 실패를 별도 증거 ZIP에 보존했습니다.

실제 외부 결제·분산 정합성·원본 웹 API에 재고 연결·재처리 UI·physical backup/PITR·multi-host HA·in-place upgrade·ES legacy 현대화와 기존 runtime 검증 공백은 여전히 별도 작업입니다. 이 v5는 작고 격리된 트랜잭션 비교 실습에 집중합니다.

공식 동작 계약 출처는 가이드 [S1]~[S4]의 MariaDB 문서입니다. 문서 확인은 고정 이미지 실행 증거가 아닙니다.
