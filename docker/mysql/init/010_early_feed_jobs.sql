-- Evidence lane only; does not enable any mode or start another Feed connection.
CREATE TABLE IF NOT EXISTS early_feed_jobs (
    tx_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    status ENUM('queued','done','expired','failed','interrupted') NOT NULL,
    received_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    result_payload LONGTEXT,
    KEY ix_early_feed_jobs_status (status)
) ENGINE=InnoDB;
GRANT SELECT, INSERT, UPDATE ON smart_money.early_feed_jobs TO 'smart_money_runtime'@'%';
