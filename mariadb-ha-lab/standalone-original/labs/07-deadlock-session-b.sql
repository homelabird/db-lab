USE commerce_lab;
SET autocommit=0;
START TRANSACTION;
UPDATE account_balance SET balance=balance-100 WHERE customer_id=202;
-- session A에서 customer_id=201을 잡은 상태에서 아래 실행
UPDATE account_balance SET balance=balance+100 WHERE customer_id=201;
COMMIT;
-- 둘 중 하나가 ERROR 1213 Deadlock found... 를 받는지 확인.
