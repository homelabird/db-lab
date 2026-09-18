-- 현재 세션 / 대기 관찰
SHOW FULL PROCESSLIST;

SELECT * FROM information_schema.INNODB_TRX\G

-- 잠금 대기 관계(MariaDB 버전에 따라 컬럼 차이가 있을 수 있음)
SELECT * FROM information_schema.INNODB_LOCK_WAITS;

SHOW ENGINE INNODB STATUS\G

-- performance_schema가 켜졌는지 확인
SHOW VARIABLES LIKE 'performance_schema';

-- 느린 쿼리 설정
SHOW VARIABLES LIKE 'slow_query%';
SHOW VARIABLES LIKE 'long_query_time';

-- 테이블 / 인덱스 사용량을 탐색
SELECT *
FROM performance_schema.table_io_waits_summary_by_index_usage
WHERE OBJECT_SCHEMA='commerce_lab'
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 20;
