CREATE TABLE IF NOT EXISTS lab_ops.auto_id_demo (
 id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
 source_node VARCHAR(64) NOT NULL,
 created_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;
SELECT @@hostname,@@auto_increment_increment,@@auto_increment_offset;
INSERT INTO lab_ops.auto_id_demo(source_node,created_at) VALUES(@@hostname,NOW(6));
SET SESSION wsrep_sync_wait=1;
SELECT * FROM lab_ops.auto_id_demo ORDER BY id;
-- Run on every node. Gaps are normal; IDs are not a gap-free business sequence or a global clock.
