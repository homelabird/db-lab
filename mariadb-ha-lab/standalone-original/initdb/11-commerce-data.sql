USE commerce_lab;
SET foreign_key_checks = 0;

DROP TABLE IF EXISTS _digits;
CREATE TABLE _digits (n TINYINT UNSIGNED NOT NULL PRIMARY KEY) ENGINE=MEMORY;
INSERT INTO _digits VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

DROP TABLE IF EXISTS _numbers;
CREATE TABLE _numbers (n INT UNSIGNED NOT NULL PRIMARY KEY) ENGINE=InnoDB;
INSERT INTO _numbers(n)
SELECT d0.n + d1.n*10 + d2.n*100 + d3.n*1000 + d4.n*10000 + d5.n*100000
FROM _digits d0
CROSS JOIN _digits d1
CROSS JOIN _digits d2
CROSS JOIN _digits d3
CROSS JOIN _digits d4
CROSS JOIN _digits d5;

INSERT INTO customer
SELECT
  n + 1,
  CONCAT('user', LPAD(n + 1, 6, '0'), '@example.com'),
  ELT((n % 12) + 1,'Minjun','Seojun','Jiho','Doyun','Hajun','Seoah','Jiyu','Yuna','Jiwon','Sujin','Hyunwoo','Eunji'),
  ELT(((n DIV 12) % 10) + 1,'Kim','Lee','Park','Choi','Jung','Kang','Cho','Yoon','Jang','Lim'),
  CONCAT('010-', LPAD((n*17)%10000,4,'0'), '-', LPAD((n*31)%10000,4,'0')),
  CASE WHEN n % 100 < 3 THEN 'VIP' WHEN n % 100 < 15 THEN 'GOLD' WHEN n % 100 < 45 THEN 'SILVER' ELSE 'BRONZE' END,
  (n % 3 = 0),
  DATE_ADD('2022-01-01 00:00:00', INTERVAL (n % 1400) DAY) + INTERVAL (n % 86400) SECOND,
  DATE_ADD('2026-06-01 00:00:00', INTERVAL (n % 100) DAY) + INTERVAL (n % 86400) SECOND
FROM _numbers WHERE n < 50000;

INSERT INTO customer_address
SELECT
  n + 1,
  n + 1,
  'HOME',
  ELT((n % 10) + 1,'Seoul','Incheon','Busan','Daegu','Daejeon','Gwangju','Suwon','Seongnam','Ulsan','Changwon'),
  CONCAT('District-', LPAD((n % 35) + 1,2,'0')),
  LPAD(10000 + (n % 89999), 5, '0'),
  CONCAT((n % 300) + 1, ' Example-ro ', (n % 80) + 1),
  IF(n % 4 = 0, CONCAT('Unit ', (n % 2000) + 1), NULL),
  TRUE,
  DATE_ADD('2022-01-01', INTERVAL (n % 1400) DAY)
FROM _numbers WHERE n < 50000;

INSERT INTO category(category_id, parent_category_id, name) VALUES
(1,NULL,'Electronics'),(2,NULL,'Home'),(3,NULL,'Fashion'),(4,NULL,'Sports'),(5,NULL,'Books'),
(6,NULL,'Beauty'),(7,NULL,'Food'),(8,NULL,'Office'),(9,NULL,'Pet'),(10,NULL,'Automotive');

INSERT INTO category(category_id, parent_category_id, name)
SELECT n + 11, (n % 10) + 1, CONCAT('Category-', LPAD(n + 1, 2, '0'))
FROM _numbers WHERE n < 50;

INSERT INTO product
SELECT
  n + 1,
  (n % 50) + 11,
  CONCAT('SKU-', LPAD(n + 1, 7, '0')),
  CONCAT(ELT((n % 8)+1,'Alpha','Beta','Gamma','Delta','Nova','Prime','Core','Edge'),' Product ',n + 1),
  ELT((n % 12)+1,'Acme','Globex','Initech','Umbrella','Stark','Wayne','Wonka','Soylent','Hooli','Vehement','MassiveDynamic','Cyberdyne'),
  5000 + ((n * 37) % 2000000),
  3000 + ((n * 19) % 900000),
  CASE WHEN n % 100 < 92 THEN 'ACTIVE' WHEN n % 100 < 97 THEN 'INACTIVE' ELSE 'DISCONTINUED' END,
  JSON_OBJECT('color', ELT((n%6)+1,'black','white','blue','red','green','silver'), 'weight_g', 100 + (n%4900), 'fragile', IF(n%7=0, TRUE, FALSE)),
  CONCAT('Synthetic catalog item ', n + 1, ' for full text, JSON and index practice. Brand=', ELT((n % 12)+1,'Acme','Globex','Initech','Umbrella','Stark','Wayne','Wonka','Soylent','Hooli','Vehement','MassiveDynamic','Cyberdyne')),
  DATE_ADD('2023-01-01', INTERVAL (n % 1000) DAY),
  DATE_ADD('2026-01-01', INTERVAL (n % 240) DAY)
FROM _numbers WHERE n < 10000;

INSERT INTO warehouse VALUES
(1,'Seoul-East','SEOUL'),(2,'Seoul-West','SEOUL'),(3,'Incheon-1','INCHEON'),(4,'Busan-1','BUSAN'),
(5,'Daegu-1','DAEGU'),(6,'Daejeon-1','DAEJEON'),(7,'Gwangju-1','GWANGJU'),(8,'Suwon-1','GYEONGGI');

INSERT INTO inventory
SELECT
  w.warehouse_id,
  p.product_id,
  20 + ((p.product_id * w.warehouse_id * 13) % 500),
  (p.product_id + w.warehouse_id) % 20,
  10 + (p.product_id % 40),
  DATE_ADD('2026-09-01', INTERVAL ((p.product_id + w.warehouse_id) % 15) DAY)
FROM warehouse w CROSS JOIN product p;

INSERT INTO orders
SELECT
  n + 1,
  (n % 50000) + 1,
  (n % 50000) + 1,
  CASE
    WHEN n % 100 < 3 THEN 'CANCELLED'
    WHEN n % 100 < 5 THEN 'REFUNDED'
    WHEN n % 100 < 10 THEN 'PENDING'
    WHEN n % 100 < 18 THEN 'PAID'
    WHEN n % 100 < 25 THEN 'PACKING'
    WHEN n % 100 < 45 THEN 'SHIPPED'
    ELSE 'DELIVERED'
  END,
  ELT((n % 3)+1,'WEB','MOBILE','PARTNER'),
  0,0,0,0,0,
  DATE_ADD('2024-01-01 00:00:00', INTERVAL (n % 989) DAY) + INTERVAL ((n*97)%86400) SECOND,
  DATE_ADD('2024-01-01 00:00:00', INTERVAL (n % 989) DAY) + INTERVAL (((n*97)%86400)+120) SECOND
FROM _numbers WHERE n < 200000;

INSERT INTO order_item(order_item_id, order_id, product_id, quantity, unit_price, discount_amount)
SELECT
  (o.n * 3) + i.item_no,
  o.n + 1,
  ((o.n * 17 + i.item_no * 997) % 10000) + 1,
  1 + ((o.n + i.item_no) % 4),
  p.price,
  ROUND(CASE WHEN (o.n + i.item_no) % 10 = 0 THEN p.price * 0.10 ELSE 0 END, 2)
FROM (SELECT n FROM _numbers WHERE n < 200000) o
CROSS JOIN (SELECT 1 item_no UNION ALL SELECT 2 UNION ALL SELECT 3) i
JOIN product p ON p.product_id = ((o.n * 17 + i.item_no * 997) % 10000) + 1;

UPDATE orders o
JOIN (
  SELECT order_id,
         SUM(quantity * unit_price) gross,
         SUM(discount_amount) discount_total,
         SUM(line_total) net
  FROM order_item
  GROUP BY order_id
) x ON x.order_id = o.order_id
SET o.subtotal = x.gross,
    o.discount_total = x.discount_total,
    o.tax_total = ROUND(x.net * 0.10, 2),
    o.shipping_fee = IF(x.net >= 50000, 0, 3000),
    o.grand_total = x.net + ROUND(x.net * 0.10, 2) + IF(x.net >= 50000, 0, 3000);

INSERT INTO payment
SELECT
  o.order_id,
  o.order_id,
  ELT((o.order_id % 4)+1,'CARD','BANK','WALLET','POINT'),
  CASE
    WHEN o.status = 'CANCELLED' THEN 'CANCELLED'
    WHEN o.status = 'REFUNDED' THEN 'REFUNDED'
    WHEN o.status = 'PENDING' THEN 'READY'
    ELSE 'CAPTURED'
  END,
  o.grand_total,
  ELT((o.order_id % 5)+1,'PAYCO','TOSS','KAKAO','NAVER','BANK-GW'),
  CONCAT('TX-', LPAD(o.order_id, 12, '0')),
  o.ordered_at,
  IF(o.status IN ('CANCELLED','PENDING'), NULL, o.ordered_at + INTERVAL 3 SECOND)
FROM orders o;

INSERT INTO shipment
SELECT
  o.order_id,
  o.order_id,
  (o.order_id % 8) + 1,
  ELT((o.order_id % 5)+1,'CJ','LOTTE','HANJIN','POST','LOGEN'),
  CONCAT('TRK-', LPAD(o.order_id, 12, '0')),
  CASE WHEN o.status='DELIVERED' THEN 'DELIVERED' WHEN o.status='SHIPPED' THEN 'IN_TRANSIT' ELSE 'READY' END,
  IF(o.status IN ('SHIPPED','DELIVERED'), o.ordered_at + INTERVAL 1 DAY, NULL),
  IF(o.status='DELIVERED', o.ordered_at + INTERVAL 3 DAY, NULL)
FROM orders o
WHERE o.status NOT IN ('CANCELLED','REFUNDED','PENDING');

INSERT INTO product_review
SELECT
  n + 1,
  ((n * 41) % 10000) + 1,
  ((n * 17) % 50000) + 1,
  1 + (n % 5),
  CONCAT('Review ', n + 1),
  CONCAT('Synthetic review body ', n + 1, '. Useful for aggregation and text-search exercises.'),
  DATE_ADD('2024-06-01', INTERVAL (n % 800) DAY) + INTERVAL ((n*53)%86400) SECOND
FROM _numbers WHERE n < 120000;

INSERT INTO account_balance
SELECT customer_id, ROUND(((customer_id * 73) % 5000000) / 100, 2), 0, '2026-09-01 00:00:00'
FROM customer;

INSERT INTO api_request_log
SELECT
  n + 1,
  DATE_ADD('2025-10-01 00:00:00', INTERVAL (n % 350) DAY) + INTERVAL ((n * 29) % 86400) SECOND,
  IF(n % 12 = 0, NULL, (n % 50000) + 1),
  ELT((n % 4)+1,'GET','POST','PUT','DELETE'),
  ELT((n % 8)+1,'/api/products','/api/orders','/api/cart','/api/payments','/api/search','/api/profile','/api/reviews','/api/inventory'),
  CASE WHEN n % 100 < 88 THEN 200 WHEN n % 100 < 92 THEN 201 WHEN n % 100 < 95 THEN 400 WHEN n % 100 < 98 THEN 404 WHEN n % 100 < 99 THEN 429 ELSE 500 END,
  5 + ((n * 31) % 2500),
  CONCAT('10.', (n%250)+1, '.', ((n DIV 250)%250)+1, '.', ((n DIV 62500)%250)+1),
  ELT((n % 5)+1,'Chrome','Firefox','Safari','MobileApp/1.0','PartnerClient/2.2'),
  JSON_OBJECT('trace_id', CONCAT('trace-', LPAD(n+1, 12, '0')), 'region', ELT((n%4)+1,'kr-seoul','kr-busan','jp-tokyo','sg'))
FROM _numbers WHERE n < 500000;

DROP TABLE _numbers;
DROP TABLE _digits;
SET foreign_key_checks = 1;

ANALYZE TABLE customer, product, inventory, orders, order_item, payment, shipment, product_review, api_request_log;
