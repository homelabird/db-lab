CREATE DATABASE IF NOT EXISTS lab_ops CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE lab_ops;
CREATE TABLE IF NOT EXISTS probe (
  id CHAR(36) PRIMARY KEY,
  source_node VARCHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  payload VARCHAR(200) NOT NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS counter (
  id INT NOT NULL PRIMARY KEY,
  value BIGINT NOT NULL DEFAULT 0
) ENGINE=InnoDB;
INSERT IGNORE INTO counter VALUES (1,0),(2,0);
CREATE TABLE IF NOT EXISTS account (
  id INT NOT NULL PRIMARY KEY,
  balance BIGINT NOT NULL,
  CONSTRAINT positive_balance CHECK (balance >= 0)
) ENGINE=InnoDB;
INSERT IGNORE INTO account SELECT seq, 1000000 FROM seq_1_to_100;
CREATE TABLE IF NOT EXISTS transfer (
  request_id CHAR(36) NOT NULL PRIMARY KEY,
  from_id INT NOT NULL,
  to_id INT NOT NULL,
  amount BIGINT NOT NULL,
  handled_by VARCHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  FOREIGN KEY (from_id) REFERENCES account(id),
  FOREIGN KEY (to_id) REFERENCES account(id),
  CONSTRAINT transfer_amount CHECK (amount > 0 AND from_id <> to_id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS dataset_manifest (
  id INT NOT NULL PRIMARY KEY,
  size_name VARCHAR(20) NOT NULL,
  state ENUM('loading','complete') NOT NULL,
  loaded_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;
