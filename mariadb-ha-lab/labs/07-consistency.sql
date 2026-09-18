SELECT @@hostname;
SHOW SESSION VARIABLES LIKE 'wsrep_sync_wait';
SET SESSION wsrep_sync_wait=1;
SELECT id,source_node,created_at,payload FROM lab_ops.probe ORDER BY created_at DESC LIMIT 10;
SET SESSION wsrep_sync_wait=0;
-- Compare visibility on another node AFTER a committed write; observe, don't assume lag always occurs.
-- This barrier does not convert an existing snapshot into a globally serializable transaction.
