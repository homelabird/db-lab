CREATE USER IF NOT EXISTS 'lab'@'%' IDENTIFIED BY 'labpassword';
GRANT ALL PRIVILEGES ON commerce_lab.* TO 'lab'@'%';
GRANT SELECT ON sakila.* TO 'lab'@'%';

CREATE USER IF NOT EXISTS 'readonly'@'%' IDENTIFIED BY 'readonlypass';
GRANT SELECT ON commerce_lab.* TO 'readonly'@'%';
GRANT SELECT ON sakila.* TO 'readonly'@'%';

FLUSH PRIVILEGES;
