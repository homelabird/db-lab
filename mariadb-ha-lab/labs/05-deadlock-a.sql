-- Start A and B within 10 seconds, both on galera1. One is expected to fail with 1213.
USE lab_ops;
START TRANSACTION;
UPDATE counter SET value=value+1 WHERE id=1;
SELECT SLEEP(10);
UPDATE counter SET value=value+1 WHERE id=2;
COMMIT;
