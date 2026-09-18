-- Run only on one node; TOI should distribute this DDL. Use a dedicated scratch table.
SET SESSION wsrep_OSU_method='TOI';
CREATE TABLE IF NOT EXISTS lab_ops.ddl_demo(id INT PRIMARY KEY, body VARCHAR(100)) ENGINE=InnoDB;
ALTER TABLE lab_ops.ddl_demo ADD COLUMN IF NOT EXISTS note VARCHAR(100) NULL;
SHOW CREATE TABLE lab_ops.ddl_demo;
-- Then SHOW CREATE TABLE lab_ops.ddl_demo on the other nodes.
-- RSU is intentionally NOT executed here: it is local and can create incompatible schemas.
