SET NAMES utf8mb4;
DROP DATABASE IF EXISTS commerce_lab;
CREATE DATABASE commerce_lab CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE commerce_lab;

CREATE TABLE customer (
  customer_id BIGINT UNSIGNED NOT NULL,
  email VARCHAR(120) NOT NULL,
  first_name VARCHAR(40) NOT NULL,
  last_name VARCHAR(40) NOT NULL,
  phone VARCHAR(30) NULL,
  tier ENUM('BRONZE','SILVER','GOLD','VIP') NOT NULL DEFAULT 'BRONZE',
  marketing_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME NOT NULL,
  last_login_at DATETIME NULL,
  PRIMARY KEY (customer_id),
  UNIQUE KEY uk_customer_email (email),
  KEY idx_customer_created_at (created_at)
) ENGINE=InnoDB;

CREATE TABLE customer_address (
  address_id BIGINT UNSIGNED NOT NULL,
  customer_id BIGINT UNSIGNED NOT NULL,
  address_type ENUM('HOME','WORK','OTHER') NOT NULL,
  city VARCHAR(50) NOT NULL,
  district VARCHAR(50) NOT NULL,
  postal_code VARCHAR(12) NOT NULL,
  address_line1 VARCHAR(150) NOT NULL,
  address_line2 VARCHAR(150) NULL,
  is_default BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (address_id),
  KEY idx_address_customer (customer_id),
  CONSTRAINT fk_address_customer FOREIGN KEY (customer_id)
    REFERENCES customer(customer_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE category (
  category_id INT UNSIGNED NOT NULL,
  parent_category_id INT UNSIGNED NULL,
  name VARCHAR(80) NOT NULL,
  PRIMARY KEY (category_id),
  KEY idx_category_parent (parent_category_id),
  CONSTRAINT fk_category_parent FOREIGN KEY (parent_category_id)
    REFERENCES category(category_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE product (
  product_id BIGINT UNSIGNED NOT NULL,
  category_id INT UNSIGNED NOT NULL,
  sku VARCHAR(32) NOT NULL,
  name VARCHAR(150) NOT NULL,
  brand VARCHAR(80) NOT NULL,
  price DECIMAL(12,2) NOT NULL,
  cost DECIMAL(12,2) NOT NULL,
  status ENUM('ACTIVE','INACTIVE','DISCONTINUED') NOT NULL,
  attributes JSON NULL,
  description TEXT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (product_id),
  UNIQUE KEY uk_product_sku (sku),
  KEY idx_product_category (category_id),
  KEY idx_product_status_price (status, price),
  FULLTEXT KEY ft_product_text (name, description),
  CONSTRAINT fk_product_category FOREIGN KEY (category_id)
    REFERENCES category(category_id)
) ENGINE=InnoDB;

CREATE TABLE warehouse (
  warehouse_id SMALLINT UNSIGNED NOT NULL,
  name VARCHAR(80) NOT NULL,
  region VARCHAR(40) NOT NULL,
  PRIMARY KEY (warehouse_id)
) ENGINE=InnoDB;

CREATE TABLE inventory (
  warehouse_id SMALLINT UNSIGNED NOT NULL,
  product_id BIGINT UNSIGNED NOT NULL,
  on_hand INT NOT NULL,
  reserved INT NOT NULL DEFAULT 0,
  reorder_point INT NOT NULL DEFAULT 10,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (warehouse_id, product_id),
  KEY idx_inventory_product (product_id),
  CONSTRAINT fk_inventory_warehouse FOREIGN KEY (warehouse_id)
    REFERENCES warehouse(warehouse_id),
  CONSTRAINT fk_inventory_product FOREIGN KEY (product_id)
    REFERENCES product(product_id)
) ENGINE=InnoDB;

CREATE TABLE orders (
  order_id BIGINT UNSIGNED NOT NULL,
  customer_id BIGINT UNSIGNED NOT NULL,
  shipping_address_id BIGINT UNSIGNED NOT NULL,
  status ENUM('PENDING','PAID','PACKING','SHIPPED','DELIVERED','CANCELLED','REFUNDED') NOT NULL,
  channel ENUM('WEB','MOBILE','PARTNER') NOT NULL,
  subtotal DECIMAL(14,2) NOT NULL DEFAULT 0,
  discount_total DECIMAL(14,2) NOT NULL DEFAULT 0,
  tax_total DECIMAL(14,2) NOT NULL DEFAULT 0,
  shipping_fee DECIMAL(12,2) NOT NULL DEFAULT 0,
  grand_total DECIMAL(14,2) NOT NULL DEFAULT 0,
  ordered_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (order_id),
  KEY idx_orders_customer (customer_id),
  KEY idx_orders_status_ordered (status, ordered_at),
  KEY idx_orders_ordered_at (ordered_at),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id)
    REFERENCES customer(customer_id),
  CONSTRAINT fk_orders_address FOREIGN KEY (shipping_address_id)
    REFERENCES customer_address(address_id)
) ENGINE=InnoDB;

CREATE TABLE order_item (
  order_item_id BIGINT UNSIGNED NOT NULL,
  order_id BIGINT UNSIGNED NOT NULL,
  product_id BIGINT UNSIGNED NOT NULL,
  quantity SMALLINT UNSIGNED NOT NULL,
  unit_price DECIMAL(12,2) NOT NULL,
  discount_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
  line_total DECIMAL(14,2) AS ((quantity * unit_price) - discount_amount) PERSISTENT,
  PRIMARY KEY (order_item_id),
  KEY idx_order_item_order (order_id),
  KEY idx_order_item_product (product_id),
  CONSTRAINT fk_order_item_order FOREIGN KEY (order_id)
    REFERENCES orders(order_id) ON DELETE CASCADE,
  CONSTRAINT fk_order_item_product FOREIGN KEY (product_id)
    REFERENCES product(product_id)
) ENGINE=InnoDB;

CREATE TABLE payment (
  payment_id BIGINT UNSIGNED NOT NULL,
  order_id BIGINT UNSIGNED NOT NULL,
  method ENUM('CARD','BANK','WALLET','POINT') NOT NULL,
  status ENUM('READY','AUTHORIZED','CAPTURED','FAILED','CANCELLED','REFUNDED') NOT NULL,
  amount DECIMAL(14,2) NOT NULL,
  provider VARCHAR(40) NOT NULL,
  transaction_key VARCHAR(80) NOT NULL,
  requested_at DATETIME NOT NULL,
  approved_at DATETIME NULL,
  PRIMARY KEY (payment_id),
  UNIQUE KEY uk_payment_tx_key (transaction_key),
  KEY idx_payment_order (order_id),
  KEY idx_payment_status_requested (status, requested_at),
  CONSTRAINT fk_payment_order FOREIGN KEY (order_id)
    REFERENCES orders(order_id)
) ENGINE=InnoDB;

CREATE TABLE shipment (
  shipment_id BIGINT UNSIGNED NOT NULL,
  order_id BIGINT UNSIGNED NOT NULL,
  warehouse_id SMALLINT UNSIGNED NOT NULL,
  carrier VARCHAR(40) NOT NULL,
  tracking_no VARCHAR(80) NOT NULL,
  status ENUM('READY','PICKED_UP','IN_TRANSIT','DELIVERED','RETURNED') NOT NULL,
  shipped_at DATETIME NULL,
  delivered_at DATETIME NULL,
  PRIMARY KEY (shipment_id),
  UNIQUE KEY uk_shipment_tracking (tracking_no),
  KEY idx_shipment_order (order_id),
  KEY idx_shipment_status_shipped (status, shipped_at),
  CONSTRAINT fk_shipment_order FOREIGN KEY (order_id)
    REFERENCES orders(order_id),
  CONSTRAINT fk_shipment_warehouse FOREIGN KEY (warehouse_id)
    REFERENCES warehouse(warehouse_id)
) ENGINE=InnoDB;

CREATE TABLE product_review (
  review_id BIGINT UNSIGNED NOT NULL,
  product_id BIGINT UNSIGNED NOT NULL,
  customer_id BIGINT UNSIGNED NOT NULL,
  rating TINYINT UNSIGNED NOT NULL,
  title VARCHAR(120) NOT NULL,
  body TEXT NOT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (review_id),
  KEY idx_review_product_created (product_id, created_at),
  KEY idx_review_customer (customer_id),
  CONSTRAINT chk_review_rating CHECK (rating BETWEEN 1 AND 5),
  CONSTRAINT fk_review_product FOREIGN KEY (product_id)
    REFERENCES product(product_id),
  CONSTRAINT fk_review_customer FOREIGN KEY (customer_id)
    REFERENCES customer(customer_id)
) ENGINE=InnoDB;

CREATE TABLE order_status_history (
  history_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id BIGINT UNSIGNED NOT NULL,
  old_status VARCHAR(20) NULL,
  new_status VARCHAR(20) NOT NULL,
  changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  changed_by VARCHAR(80) NOT NULL DEFAULT 'system',
  PRIMARY KEY (history_id),
  KEY idx_history_order_changed (order_id, changed_at),
  CONSTRAINT fk_history_order FOREIGN KEY (order_id)
    REFERENCES orders(order_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Deliberately large log table for partition pruning practice.
CREATE TABLE api_request_log (
  log_id BIGINT UNSIGNED NOT NULL,
  request_at DATETIME NOT NULL,
  customer_id BIGINT UNSIGNED NULL,
  method ENUM('GET','POST','PUT','DELETE') NOT NULL,
  endpoint VARCHAR(160) NOT NULL,
  status_code SMALLINT UNSIGNED NOT NULL,
  latency_ms INT UNSIGNED NOT NULL,
  remote_ip VARCHAR(45) NOT NULL,
  user_agent VARCHAR(180) NOT NULL,
  request_meta JSON NULL,
  PRIMARY KEY (log_id, request_at),
  KEY idx_api_customer_time (customer_id, request_at),
  KEY idx_api_endpoint_status (endpoint, status_code)
) ENGINE=InnoDB
PARTITION BY RANGE COLUMNS(request_at) (
  PARTITION p2025q4 VALUES LESS THAN ('2026-01-01'),
  PARTITION p2026q1 VALUES LESS THAN ('2026-04-01'),
  PARTITION p2026q2 VALUES LESS THAN ('2026-07-01'),
  PARTITION p2026q3 VALUES LESS THAN ('2026-10-01'),
  PARTITION pmax VALUES LESS THAN (MAXVALUE)
);

CREATE TABLE account_balance (
  customer_id BIGINT UNSIGNED NOT NULL,
  balance DECIMAL(14,2) NOT NULL DEFAULT 0,
  version_no INT UNSIGNED NOT NULL DEFAULT 0,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (customer_id),
  CONSTRAINT fk_balance_customer FOREIGN KEY (customer_id)
    REFERENCES customer(customer_id)
) ENGINE=InnoDB;

DELIMITER //
CREATE TRIGGER trg_orders_status_audit
AFTER UPDATE ON orders
FOR EACH ROW
BEGIN
  IF NOT (OLD.status <=> NEW.status) THEN
    INSERT INTO order_status_history(order_id, old_status, new_status, changed_at, changed_by)
    VALUES (NEW.order_id, OLD.status, NEW.status, NOW(), CURRENT_USER());
  END IF;
END//
DELIMITER ;

CREATE OR REPLACE VIEW v_customer_order_summary AS
SELECT
  c.customer_id,
  c.email,
  c.tier,
  COUNT(o.order_id) AS order_count,
  COALESCE(SUM(o.grand_total), 0) AS lifetime_value,
  MAX(o.ordered_at) AS last_order_at
FROM customer c
LEFT JOIN orders o ON o.customer_id = c.customer_id
GROUP BY c.customer_id, c.email, c.tier;

DELIMITER //
CREATE PROCEDURE sp_transfer_balance(
  IN p_from BIGINT UNSIGNED,
  IN p_to BIGINT UNSIGNED,
  IN p_amount DECIMAL(14,2)
)
BEGIN
  DECLARE v_balance DECIMAL(14,2);
  DECLARE v_dummy DECIMAL(14,2);

  IF p_amount <= 0 OR p_from = p_to THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid transfer request';
  END IF;

  START TRANSACTION;

  -- Consistent lock ordering reduces deadlock risk.
  IF p_from < p_to THEN
    SELECT balance INTO v_balance FROM account_balance WHERE customer_id=p_from FOR UPDATE;
    SELECT balance INTO v_dummy FROM account_balance WHERE customer_id=p_to FOR UPDATE;
  ELSE
    SELECT balance INTO v_dummy FROM account_balance WHERE customer_id=p_to FOR UPDATE;
    SELECT balance INTO v_balance FROM account_balance WHERE customer_id=p_from FOR UPDATE;
  END IF;

  IF v_balance < p_amount THEN
    ROLLBACK;
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'insufficient balance';
  ELSE
    UPDATE account_balance
      SET balance=balance-p_amount, version_no=version_no+1, updated_at=NOW()
      WHERE customer_id=p_from;
    UPDATE account_balance
      SET balance=balance+p_amount, version_no=version_no+1, updated_at=NOW()
      WHERE customer_id=p_to;
    COMMIT;
  END IF;
END//
DELIMITER ;
