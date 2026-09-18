USE lab_ops;
START TRANSACTION;
UPDATE counter SET value=value+1 WHERE id=2;
SELECT SLEEP(10);
UPDATE counter SET value=value+1 WHERE id=1;
COMMIT;
