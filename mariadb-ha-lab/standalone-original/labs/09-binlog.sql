SHOW VARIABLES LIKE 'log_bin';
SHOW VARIABLES LIKE 'binlog_format';
SHOW BINARY LOGS;
SHOW MASTER STATUS;

-- 컨테이너 shell에서 예시:
-- mariadb-binlog /var/lib/mysql/mysql-bin.000001 | less
-- ROW 포맷은 사람이 바로 읽기 어려우므로:
-- mariadb-binlog --base64-output=DECODE-ROWS -vv /var/lib/mysql/mysql-bin.000001 | less
