USE commerce_lab;

-- EXPLAIN / EXPLAIN FORMAT=JSON 로 전후를 비교한다.
-- 이 쿼리는 customer_id + ordered_at 복합 인덱스가 없어서 개선 여지가 있다.
EXPLAIN
SELECT order_id, status, grand_total, ordered_at
FROM orders
WHERE customer_id = 12345
  AND ordered_at >= '2026-01-01'
ORDER BY ordered_at DESC
LIMIT 20;

-- 실습:
-- CREATE INDEX idx_orders_customer_ordered ON orders(customer_id, ordered_at DESC);
-- 생성 전/후 rows, key, Extra를 비교하고 인덱스를 다시 제거해본다.

-- 함수 적용으로 인덱스 활용을 방해하는 예
EXPLAIN
SELECT COUNT(*)
FROM orders
WHERE DATE(ordered_at) = '2026-05-01';

-- SARGable 형태로 직접 고쳐본다.

-- SELECT * 남용 + 정렬 + LIMIT 실습
EXPLAIN
SELECT *
FROM api_request_log
WHERE endpoint='/api/orders' AND status_code=500
ORDER BY latency_ms DESC
LIMIT 100;
