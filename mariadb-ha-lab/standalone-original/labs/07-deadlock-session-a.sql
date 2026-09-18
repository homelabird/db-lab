USE commerce_lab;
SET autocommit=0;
START TRANSACTION;
UPDATE account_balance SET balance=balance-100 WHERE customer_id=201;
-- session B에서 첫 UPDATE 실행 후 아래를 실행
UPDATE account_balance SET balance=balance+100 WHERE customer_id=202;
COMMIT;
