USE commerce_lab;

-- 상품별 매출 순위
WITH sales AS (
  SELECT p.product_id, p.name, SUM(oi.line_total) sales
  FROM product p
  JOIN order_item oi ON oi.product_id = p.product_id
  GROUP BY p.product_id, p.name
)
SELECT product_id, name, sales,
       DENSE_RANK() OVER (ORDER BY sales DESC) sales_rank
FROM sales
ORDER BY sales_rank
LIMIT 50;

-- 고객별 이전 주문 대비 주문액 변화
SELECT customer_id, order_id, ordered_at, grand_total,
       LAG(grand_total) OVER (PARTITION BY customer_id ORDER BY ordered_at) prev_total,
       grand_total - LAG(grand_total) OVER (PARTITION BY customer_id ORDER BY ordered_at) delta
FROM orders
WHERE customer_id BETWEEN 1 AND 20
ORDER BY customer_id, ordered_at;

-- 직접 풀기:
-- 1) 카테고리별 매출 Top 3 상품
-- 2) 월별 매출 + 전월 대비 증감률
-- 3) 고객별 최근 주문 3건만 추출
