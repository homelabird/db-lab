USE commerce_lab;

-- 파티션 목록
SELECT PARTITION_NAME, TABLE_ROWS
FROM information_schema.PARTITIONS
WHERE TABLE_SCHEMA='commerce_lab' AND TABLE_NAME='api_request_log';

-- EXPLAIN PARTITIONS로 pruning 확인
EXPLAIN PARTITIONS
SELECT COUNT(*)
FROM api_request_log
WHERE request_at >= '2026-07-01'
  AND request_at <  '2026-08-01';

EXPLAIN PARTITIONS
SELECT COUNT(*)
FROM api_request_log
WHERE DATE(request_at) = '2026-07-15';

-- 두 쿼리의 partitions 차이를 비교한다.
