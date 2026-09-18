SELECT @@hostname, @@server_id, VERSION();
SHOW GLOBAL VARIABLES WHERE Variable_name IN ('wsrep_on','wsrep_provider','wsrep_cluster_address',
 'wsrep_node_name','wsrep_node_address','wsrep_sst_method','binlog_format',
 'innodb_autoinc_lock_mode','innodb_buffer_pool_size');
SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_cluster_size','wsrep_cluster_status',
 'wsrep_cluster_state_uuid','wsrep_local_state','wsrep_local_state_comment',
 'wsrep_connected','wsrep_ready','wsrep_last_committed');
-- Normal: Primary / Synced / 4 / ON / ON / size 3 / same state UUID on all nodes.
