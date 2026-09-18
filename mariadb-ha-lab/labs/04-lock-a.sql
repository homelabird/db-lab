-- Run on galera1, then immediately run 04-lock-b.sql in another terminal ON galera1.
USE lab_ops;
START TRANSACTION;
UPDATE counter SET value=value+1 WHERE id=1;
SELECT 'A holds row lock for 20 seconds', SLEEP(20);
COMMIT;
