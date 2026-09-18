USE commerce_lab;
SET autocommit=0;
START TRANSACTION;
UPDATE account_balance SET balance=balance+500, version_no=version_no+1 WHERE customer_id=100;
-- session A가 commit 전이면 대기한다.
-- 다른 세션에서 SHOW PROCESSLIST; / information_schema.INNODB_TRX 를 관찰한다.
ROLLBACK;
