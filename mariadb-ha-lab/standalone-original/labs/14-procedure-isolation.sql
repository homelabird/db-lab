USE commerce_lab;

SHOW CREATE PROCEDURE sp_transfer_balance\G
SELECT * FROM account_balance WHERE customer_id IN (301,302);
CALL sp_transfer_balance(301,302,1000);
SELECT * FROM account_balance WHERE customer_id IN (301,302);

-- 격리수준 확인
SELECT @@transaction_isolation;

-- MariaDB 버전에 따라 tx_isolation 이름도 확인해본다.
SHOW VARIABLES LIKE '%isolation%';

-- 실습 과제:
-- 1) 두 세션에서 동일 customer의 balance를 먼저 SELECT 한 뒤 UPDATE해서 lost update를 재현할 수 있는지 확인한다.
-- 2) SELECT ... FOR UPDATE를 넣어 결과를 비교한다.
-- 3) READ COMMITTED / REPEATABLE READ에서 같은 SELECT를 두 번 수행하고 phantom/non-repeatable read 여부를 관찰한다.
