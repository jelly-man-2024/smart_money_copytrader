CREATE TABLE IF NOT EXISTS wallet_keys (
    wallet_address CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    private_key_hex CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (wallet_address),
    CONSTRAINT chk_wallet_key_address CHECK
      (wallet_address REGEXP '^0x[0-9a-f]{40}$'),
    CONSTRAINT chk_wallet_private_key CHECK
      (private_key_hex REGEXP '^0x[0-9a-f]{64}$')
) ENGINE=InnoDB;

CREATE USER IF NOT EXISTS 'smart_money_key_runtime'@'%'
  IDENTIFIED BY 'local-key-runtime-only';
GRANT SELECT (wallet_address, private_key_hex, enabled)
  ON smart_money_keys.wallet_keys TO 'smart_money_key_runtime'@'%';
FLUSH PRIVILEGES;
