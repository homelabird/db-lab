# 고급 실습 과제

1. `orders(customer_id, ordered_at)` 복합 인덱스 도입 전후의 실행계획과 수행시간을 비교한다.
2. `api_request_log`에서 500 응답이 많은 endpoint와 p95 비슷한 지연시간 지표를 SQL만으로 계산한다.
3. 최근 90일 구매액 기준 고객 RFM 비슷한 세그먼트를 만든다.
4. 카테고리별 매출 상위 3개 상품을 window function으로 구한다.
5. 재고 `on_hand - reserved < reorder_point`인 상품을 창고별로 찾아 발주 후보 목록을 만든다.
6. 두 세션으로 lock wait을 만들고 `SHOW PROCESSLIST`, `INNODB_TRX`, `SHOW ENGINE INNODB STATUS`에서 원인을 찾는다.
7. 의도적으로 deadlock을 만들고, 애플리케이션 레벨에서 재시도가 왜 필요한지 설명한다.
8. `JSON_VALUE(attributes, '$.color')` 검색을 generated column + index 구조로 개선한다.
9. 월 단위 파티션 pruning이 되는 쿼리와 안 되는 쿼리를 비교한다.
10. `readonly` 사용자 권한으로 UPDATE가 차단되는지 검증하고 최소 권한 원칙을 설명한다.
11. slow query log에서 인덱스가 없는 쿼리를 찾아 튜닝한다.
12. binlog를 `mariadb-binlog --base64-output=DECODE-ROWS -vv`로 열어 UPDATE가 어떻게 기록되는지 확인한다.
13. 특정 주문의 상태를 변경하고 trigger에 의해 audit history가 생성되는지 확인한다.
14. `READ COMMITTED`와 `REPEATABLE READ`에서 동일 실습을 수행하고 결과 차이를 기록한다.
15. `SELECT ... FOR UPDATE`와 일반 SELECT가 concurrent transaction에서 어떻게 다른지 재현한다.
