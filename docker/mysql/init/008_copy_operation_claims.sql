-- Compatible, opt-in foundation. Does not enable Feed trading or change budgets.
-- Existing instances: apply explicitly before enrolling any proposal in claims.
-- No FK to proposals: the claim is acquired first in the SAME reservation
-- transaction; a failed reservation rolls both writes back.
CREATE TABLE IF NOT EXISTS copy_operation_claims (
    operation_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    proposal_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    status ENUM('held','broadcast_attempted','released') NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (operation_key),
    UNIQUE KEY uq_copy_operation_proposal (proposal_id)
) ENGINE=InnoDB;

GRANT SELECT, INSERT, UPDATE ON smart_money.copy_operation_claims
    TO 'smart_money_runtime'@'%';
