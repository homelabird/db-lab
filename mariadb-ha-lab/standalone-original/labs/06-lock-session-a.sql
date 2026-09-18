USE commerce_lab;
SET autocommit=0;
START TRANSACTION;
SELECT * FROM account_balance WHERE customer_id=100 FOR UPDATE;
UPDATE account_balance SET balance=balance-1000, version_no=version_no+1 WHERE customer_id=100;
-- 여기서 COMMIT 하지 말고 session-b.sql을 실행한다.
-- 관찰 후 COMMIT;
