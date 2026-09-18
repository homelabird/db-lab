USE commerce_lab;

SELECT * FROM order_status_history ORDER BY history_id DESC LIMIT 10;

START TRANSACTION;
UPDATE orders SET status='PAID', updated_at=NOW() WHERE order_id=10;
SELECT * FROM order_status_history WHERE order_id=10 ORDER BY history_id DESC;
ROLLBACK;

-- 트리거에 의해 생긴 history row도 rollback되는지 확인해본다.
SHOW TRIGGERS FROM commerce_lab;
SHOW CREATE VIEW v_customer_order_summary\G
