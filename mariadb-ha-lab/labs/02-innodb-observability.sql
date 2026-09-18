SHOW ENGINE INNODB STATUS\G
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool%';
SHOW GLOBAL STATUS LIKE 'Innodb_row_lock%';
SHOW GLOBAL STATUS LIKE 'Threads%';
SHOW FULL PROCESSLIST;
SELECT * FROM information_schema.INNODB_TRX\G
SELECT * FROM information_schema.INNODB_LOCK_WAITS\G
SELECT schema_name, digest_text, count_star,
 ROUND(sum_timer_wait/1000000000000,3) AS total_seconds,
 ROUND(avg_timer_wait/1000000000,3) AS avg_ms
FROM performance_schema.events_statements_summary_by_digest
WHERE schema_name IN ('commerce_lab','lab_ops')
ORDER BY sum_timer_wait DESC LIMIT 10;
