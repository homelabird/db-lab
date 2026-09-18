-- root로 실행
SELECT User, Host FROM mysql.user ORDER BY User, Host;
SHOW GRANTS FOR 'lab'@'%';
SHOW GRANTS FOR 'readonly'@'%';

-- readonly 계정으로 접속해서 SELECT는 되고 UPDATE는 실패하는지 확인한다.
-- mariadb -h 127.0.0.1 -u readonly -preadonlypass commerce_lab
