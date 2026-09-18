USE commerce_lab;

-- 1. 최근 30일 주문 상태별 건수와 매출
SELECT status, COUNT(*) order_count, ROUND(SUM(grand_total),2) revenue
FROM orders
WHERE ordered_at >= (SELECT MAX(ordered_at) - INTERVAL 30 DAY FROM orders)
GROUP BY status
ORDER BY revenue DESC;

-- 2. 카테고리별 매출 Top 10
SELECT c.name category, COUNT(*) lines, ROUND(SUM(oi.line_total),2) sales
FROM order_item oi
JOIN product p ON p.product_id = oi.product_id
JOIN category c ON c.category_id = p.category_id
JOIN orders o ON o.order_id = oi.order_id
WHERE o.status NOT IN ('CANCELLED','REFUNDED')
GROUP BY c.category_id, c.name
ORDER BY sales DESC
LIMIT 10;

-- 3. 고객별 누적 매출과 최근 주문일
SELECT * FROM v_customer_order_summary
ORDER BY lifetime_value DESC
LIMIT 20;

-- 4. 아직 한 번도 팔리지 않은 상품을 찾아보라.
-- 5. 고객 tier별 평균 객단가를 구해보라.
-- 6. 월별 신규 고객수와 구매 고객수를 함께 출력해보라.
