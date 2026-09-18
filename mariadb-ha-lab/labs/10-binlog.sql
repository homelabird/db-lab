SHOW GLOBAL VARIABLES WHERE Variable_name IN ('log_bin','log_slave_updates','binlog_format',
 'binlog_expire_logs_seconds','sync_binlog','innodb_flush_log_at_trx_commit');
SHOW BINARY LOGS;
SHOW MASTER STATUS;
-- Galera write sets and the binlog are different layers. log_slave_updates writes applied changes
-- to this node's binlog; it does not set up a separate asynchronous replica or PITR retention.
