SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_received','wsrep_replicated',
 'wsrep_received_bytes','wsrep_replicated_bytes','wsrep_local_recv_queue',
 'wsrep_local_send_queue','wsrep_flow_control_paused','wsrep_flow_control_paused_ns',
 'wsrep_flow_control_sent','wsrep_flow_control_recv',
 'wsrep_local_cert_failures','wsrep_local_bf_aborts',
 'wsrep_local_cached_downto','wsrep_last_committed','wsrep_cert_deps_distance');
-- Counts are cumulative. Compare deltas across two samples without assuming equal process uptime.
-- Desync ON does not stop applying replication, and is not a quorum bypass.
SHOW GLOBAL VARIABLES LIKE 'wsrep_desync';
SHOW GLOBAL VARIABLES LIKE 'wsrep_slave_threads';
