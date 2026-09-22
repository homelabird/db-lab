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

-- Orders follow a realistic business lifecycle: status is correlated with order age.
-- Old orders are terminal (DELIVERED/REFUNDED), recent orders are still in PENDING/PAID/PACKING.
-- The exact 10% cancelled+refunded+pending (no shipment) share is preserved for verify().
INSERT INTO orders (order_id, customer_id, shipping_address_id, status, channel,
                    subtotal, discount_total, tax_total, shipping_fee, grand_total,
                    ordered_at, updated_at)
SELECT order_id, customer_id, shipping_address_id, status, channel,
       0, 0, 0, 0, 0,
       ordered_at,
       CASE WHEN status = 'PENDING' THEN ordered_at
            WHEN status = 'PAID' THEN ordered_at + INTERVAL 3 MINUTE
            WHEN status = 'PACKING' THEN ordered_at + INTERVAL 12 MINUTE
            WHEN status = 'SHIPPED' THEN ordered_at + INTERVAL 1 DAY
            WHEN status = 'CANCELLED' THEN ordered_at + INTERVAL 30 MINUTE
            WHEN status IN ('DELIVERED','REFUNDED') THEN ordered_at + INTERVAL 3 DAY
            ELSE ordered_at END
FROM (
  SELECT n + 1 AS order_id,
         (n % 50000) + 1 AS customer_id,
         (n % 50000) + 1 AS shipping_address_id,
         CASE
           WHEN n % 100 < 10 THEN
             CASE WHEN n < 120000 THEN IF(n % 100 < 4, 'CANCELLED', 'REFUNDED')
                  WHEN n % 100 < 5 THEN 'CANCELLED'
                  WHEN n % 100 < 6 THEN 'REFUNDED'
                  ELSE 'PENDING' END
           WHEN n < 120000 THEN IF(n % 100 < 13, 'SHIPPED', 'DELIVERED')
           WHEN n >= 185000 THEN 'PAID'
           WHEN n >= 165000 THEN 'PACKING'
           WHEN n >= 140000 THEN 'SHIPPED'
           ELSE 'DELIVERED'
         END AS status,
         ELT((n % 3)+1,'WEB','MOBILE','PARTNER') AS channel,
         -- Recency-skewed order time: the newest ~40% of orders arrive in the last ~3 weeks.
         DATE_SUB('2026-09-18 00:00:00', INTERVAL
           IF(n < 120000, 30 + (119999 - n) DIV 240, (199999 - n) DIV 4000) DAY)
           + INTERVAL ((n*97)%86400) SECOND AS ordered_at
  FROM _numbers WHERE n < 200000
) o;

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
  o.ordered_at + INTERVAL (o.order_id % 7) SECOND,
  IF(o.status IN ('CANCELLED','PENDING'), NULL, o.ordered_at + INTERVAL 3 MINUTE)
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

-- API traffic is realistic per endpoint: method, status mix and latency vary by endpoint,
-- and the newest ~20% of requests are concentrated in the last ~3 weeks (199k of 500k).
INSERT INTO api_request_log (log_id, request_at, customer_id, method, endpoint, status_code,
                             latency_ms, remote_ip, user_agent, request_meta)
SELECT log_id, request_at, customer_id, method, endpoint, status_code,
       -- Latency follows endpoint profile + outcome, never a flat uniform draw.
       CASE
         WHEN endpoint = '/api/search' THEN 20 + ((log_id * 97) % 9000)
         WHEN endpoint = '/api/payments' THEN 15 + ((log_id * 41) % 6000)
         WHEN status_code IN (500, 503) THEN 800 + ((log_id * 13) % 4000)
         WHEN status_code = 429 THEN 3 + ((log_id * 7) % 400)
         WHEN status_code IN (400, 404, 422) THEN 2 + ((log_id * 11) % 300)
         ELSE 4 + ((log_id * 31) % 1500)
       END AS latency_ms,
       remote_ip, user_agent,
       JSON_OBJECT('trace_id', CONCAT('trace-', LPAD(log_id, 12, '0')),
                   'region', ELT((log_id % 4)+1,'kr-seoul','kr-busan','jp-tokyo','sg'))
FROM (
  SELECT n + 1 AS log_id,
         DATE_SUB('2026-09-18 00:00:00', INTERVAL
           IF(n < 400000, (399999 - n) DIV 1360, (499999 - n) DIV 3333) DAY)
           + INTERVAL ((n * 29) % 86400) SECOND AS request_at,
         IF(n % 12 = 0, NULL, (n % 50000) + 1) AS customer_id,
         CASE e
           WHEN 1 THEN IF(n % 10 = 0, 'POST', 'GET')
           WHEN 2 THEN ELT((n % 4)+1,'GET','POST','PUT','DELETE')
           WHEN 3 THEN 'POST'
           WHEN 6 THEN IF(n % 10 < 4, 'POST', 'GET')
           ELSE 'GET'
         END AS method,
         ELT(e + 1, '/api/products','/api/orders','/api/cart','/api/payments',
                    '/api/search','/api/profile','/api/reviews','/api/inventory') AS endpoint,
         CASE e
           WHEN 0 THEN IF(n % 100 < 95, 200, IF(n % 100 < 98, 404, 500))
           WHEN 1 THEN IF(n % 100 < 80, 200, IF(n % 100 < 88, 201, IF(n % 100 < 95, 400, IF(n % 100 < 98, 404, 500))))
           WHEN 2 THEN IF(n % 100 < 85, 200, IF(n % 100 < 93, 400, IF(n % 100 < 95, 404, IF(n % 100 < 97, 429, 500))))
           WHEN 3 THEN IF(n % 100 < 75, 200, IF(n % 100 < 85, 201, IF(n % 100 < 93, 400, IF(n % 100 < 97, 422, 500))))
           WHEN 4 THEN IF(n % 100 < 90, 200, IF(n % 100 < 94, 404, IF(n % 100 < 97, 429, 500)))
           WHEN 5 THEN IF(n % 100 < 97, 200, 404)
           WHEN 6 THEN IF(n % 100 < 80, 200, IF(n % 100 < 90, 201, IF(n % 100 < 96, 400, 500)))
           ELSE        IF(n % 100 < 70, 200, IF(n % 100 < 90, 404, IF(n % 100 < 97, 500, 503)))
         END AS status_code,
         CONCAT('10.', (n%250)+1, '.', ((n DIV 250)%250)+1, '.', ((n DIV 62500)%250)+1) AS remote_ip,
         ELT((n % 5)+1,'Chrome','Firefox','Safari','MobileApp/1.0','PartnerClient/2.2') AS user_agent,
         n
  FROM (
    SELECT n, n % 8 AS e FROM _numbers WHERE n < 500000
  ) t
) api;

DROP TABLE _numbers;
DROP TABLE _digits;
SET foreign_key_checks = 1;

ANALYZE TABLE customer, product, inventory, orders, order_item, payment, shipment, product_review, api_request_log;
