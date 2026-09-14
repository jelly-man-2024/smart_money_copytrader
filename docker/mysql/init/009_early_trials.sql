-- Passive schema only. Does not start trials, enable relationships or reset budgets.
CREATE TABLE IF NOT EXISTS early_trials (
    trial_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    follower_wallet CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
    relationships_payload TEXT NOT NULL,
    started_at DOUBLE NOT NULL,
    expires_at DOUBLE NOT NULL,
    consumed_slots INT NOT NULL DEFAULT 0 CHECK(consumed_slots>=0 AND consumed_slots<=100),
    status ENUM('active','stopped') NOT NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS early_trial_operations (
    operation_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    trial_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    proposal_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
    attempted_at DOUBLE NOT NULL,
    FOREIGN KEY(trial_id) REFERENCES early_trials(trial_id)
) ENGINE=InnoDB;
GRANT SELECT, INSERT, UPDATE ON smart_money.early_trials TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT ON smart_money.early_trial_operations TO 'smart_money_runtime'@'%';
