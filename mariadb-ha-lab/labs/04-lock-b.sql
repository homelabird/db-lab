USE lab_ops;
SET SESSION innodb_lock_wait_timeout=30;
START TRANSACTION;
UPDATE counter SET value=value+1 WHERE id=1;
COMMIT;
-- This waits for A. Cross-node behavior is certification/BF-abort, not a shared local row lock.
