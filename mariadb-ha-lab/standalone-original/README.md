# MariaDB Advanced Practice Lab

기존 Sakila 예제는 그대로 유지하고, 실제 서비스에 가까운 `commerce_lab`을 추가한 학습 환경입니다.

## 포함 데이터

- 고객: 50,000
- 주소: 50,000
- 상품: 10,000
- 창고별 재고: 80,000
- 주문: 200,000
- 주문상품: 600,000
- 결제: 200,000
- 배송: 약 170,000+
- 상품 리뷰: 120,000
- API access log: 500,000 (range partition)
- 고객 잔액: 50,000
- 기존 Sakila 전체 데이터

최초 초기화 때 SQL로 생성되므로 ZIP 파일은 작지만 DB 내부 데이터는 충분히 큽니다.

## 실행

Docker Compose:

```bash
docker compose -f mariadb.yaml up -d
```

Podman Compose:

```bash
podman compose -f mariadb.yaml up -d
```

초기 데이터 생성에는 머신 성능에 따라 시간이 걸릴 수 있습니다. 준비 여부는:

```bash
podman ps
podman logs -f mariadb-learning
```

## 접속

```bash
podman exec -it mariadb-learning mariadb -ulab -plabpassword commerce_lab
```

계정:

- root / `rootpassword`
- lab / `labpassword` — `commerce_lab` 전체 권한 + Sakila SELECT
- readonly / `readonlypass` — 조회 전용

CloudBeaver: `http://localhost:8080`

CloudBeaver에서 MariaDB host는 `mariadb`, port는 `3306`으로 지정합니다.

## 학습 범위

`labs/` 순서대로 진행하면 됩니다.

- 복잡 JOIN / 집계
- CTE / Window Function
- JSON / FULLTEXT
- `EXPLAIN`, SARGable query, 복합 인덱스
- 파티셔닝 / partition pruning
- 트랜잭션 / row lock / `FOR UPDATE`
- lock wait / deadlock
- trigger / view / generated column
- 사용자 / 권한
- performance_schema / InnoDB 상태 확인
- slow query log
- binary log
- backup / restore / PITR 개념

## 중요한 점

`config/advanced.cnf`는 **학습용** 설정입니다. slow log, performance_schema, binlog 등을 관찰하기 쉽게 켜두었으며 운영 서버에 그대로 복사하는 설정 파일이 아닙니다.

기존 볼륨이 있으면 `/docker-entrypoint-initdb.d`가 다시 실행되지 않습니다. 새 데이터셋으로 처음부터 다시 만들려면:

```bash
podman compose -f mariadb.yaml down -v
podman compose -f mariadb.yaml up -d
```

추가로 `sp_transfer_balance` 저장 프로시저가 포함되어 있어 `SIGNAL`, 명시적 transaction, `FOR UPDATE`, lock ordering도 연습할 수 있습니다.
