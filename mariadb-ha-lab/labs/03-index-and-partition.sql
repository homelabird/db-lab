USE commerce_lab;
-- Compare identical time ranges, not DATE(ordered_at) with a different range.
EXPLAIN FORMAT=JSON
SELECT order_id,customer_id,grand_total FROM orders WHERE DATE(ordered_at)='2026-06-01';
EXPLAIN FORMAT=JSON
SELECT order_id,customer_id,grand_total FROM orders
WHERE ordered_at>='2026-06-01' AND ordered_at<'2026-06-02';
-- MariaDB ANALYZE actually executes the SELECT. Keep destructive statements out.
ANALYZE FORMAT=JSON
SELECT order_id,customer_id,grand_total FROM orders
WHERE ordered_at>='2026-06-01' AND ordered_at<'2026-06-02';
EXPLAIN PARTITIONS SELECT COUNT(*) FROM api_request_log
WHERE request_at>='2026-04-01' AND request_at<'2026-07-01';
SELECT product_id,JSON_VALUE(attributes,'$.color') AS color FROM product LIMIT 10;
SELECT product_id,name FROM product
WHERE MATCH(name,description) AGAINST('Synthetic catalog' IN NATURAL LANGUAGE MODE) LIMIT 10;
