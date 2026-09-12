CREATE TABLE IF NOT EXISTS signals (
    event_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    tx_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    stage_rank TINYINT UNSIGNED NOT NULL,
    payload LONGTEXT NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    KEY ix_signals_tx_hash (tx_hash)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS candidates (
    tx_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    payload LONGTEXT NOT NULL,
    status ENUM('pending','queued','retry','complete','failed') NOT NULL,
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    next_attempt_at DOUBLE NOT NULL DEFAULT 0,
    last_error VARCHAR(255) NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (tx_hash),
    KEY ix_candidates_claim (status, next_attempt_at, created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chain_cursors (
    name VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    block_number BIGINT UNSIGNED NOT NULL,
    block_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS canonical_blocks (
    block_number BIGINT UNSIGNED NOT NULL,
    block_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    parent_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY (block_number),
    UNIQUE KEY uq_canonical_block_hash (block_hash)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS candidate_inclusions (
    tx_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    block_number BIGINT UNSIGNED NOT NULL,
    block_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY (tx_hash),
    KEY ix_candidate_inclusions_block (block_number, block_hash)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS solver_order_evidence (
    evidence_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    order_id CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    kind ENUM('source_deposit','destination_delivery') NOT NULL,
    wallet VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    tx_hash VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    payload LONGTEXT NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (evidence_id),
    KEY ix_solver_order (order_id, kind)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_budget_cycles (
    cycle_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    status ENUM('active','closed') NOT NULL,
    active_guard TINYINT GENERATED ALWAYS AS
      (CASE WHEN status = 'active' THEN 1 ELSE NULL END) STORED,
    reason VARCHAR(255) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    closed_at TIMESTAMP(6) NULL,
    PRIMARY KEY (cycle_id),
    UNIQUE KEY uq_one_active_paper_budget_cycle (active_guard)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_budgets (
    cycle_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    wallet VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    bucket ENUM('USDG','ETH_WETH') NOT NULL,
    limit_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    reserved_raw VARCHAR(80) CHARACTER SET ascii NOT NULL DEFAULT '0',
    invested_raw VARCHAR(80) CHARACTER SET ascii NOT NULL DEFAULT '0',
    PRIMARY KEY (cycle_id, wallet, bucket),
    CONSTRAINT fk_paper_budget_cycle FOREIGN KEY (cycle_id)
      REFERENCES paper_budget_cycles(cycle_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_proposals (
    proposal_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_event_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_tx_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    wallet VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    trigger_mode VARCHAR(32) CHARACTER SET ascii NOT NULL,
    strategy_version VARCHAR(100) NOT NULL,
    input_asset CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    output_asset CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    budget_bucket ENUM('USDG','ETH_WETH') NOT NULL,
    amount_in_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    status ENUM('reserved','rejected','cancelled','filled') NOT NULL,
    quote_payload LONGTEXT NULL,
    attribution_payload LONGTEXT NOT NULL,
    rejection_reason VARCHAR(255) NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (proposal_id),
    UNIQUE KEY uq_paper_proposal_source
      (source_event_id, trigger_mode, strategy_version)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_reservations (
    proposal_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    cycle_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    wallet VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    bucket ENUM('USDG','ETH_WETH') NOT NULL,
    amount_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    status ENUM('active','released','consumed') NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (proposal_id),
    CONSTRAINT fk_paper_reservation_proposal FOREIGN KEY (proposal_id)
      REFERENCES paper_proposals(proposal_id),
    CONSTRAINT fk_paper_reservation_cycle FOREIGN KEY (cycle_id)
      REFERENCES paper_budget_cycles(cycle_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    proposal_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    side ENUM('BUY','SELL') NOT NULL,
    status ENUM('filled','cancelled') NOT NULL,
    payload LONGTEXT NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (order_id),
    UNIQUE KEY uq_paper_order_proposal (proposal_id),
    CONSTRAINT fk_paper_order_proposal FOREIGN KEY (proposal_id)
      REFERENCES paper_proposals(proposal_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    order_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    input_asset CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    output_asset CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    amount_in_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    amount_out_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    fee_asset CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NULL,
    fee_amount_raw VARCHAR(80) CHARACTER SET ascii NULL,
    gas_cost_wei VARCHAR(80) CHARACTER SET ascii NOT NULL DEFAULT '0',
    quote_observed_at VARCHAR(64) CHARACTER SET ascii NOT NULL,
    filled_at VARCHAR(64) CHARACTER SET ascii NOT NULL,
    attribution_payload LONGTEXT NOT NULL,
    PRIMARY KEY (fill_id),
    KEY ix_paper_fills_order (order_id),
    CONSTRAINT fk_paper_fill_order FOREIGN KEY (order_id)
      REFERENCES paper_orders(order_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_positions (
    lot_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    wallet VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    token CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    budget_cycle_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    budget_bucket ENUM('USDG','ETH_WETH') NOT NULL,
    principal_asset CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    principal_initial_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    principal_remaining_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    token_initial_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    token_remaining_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    source_event_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    buy_fill_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    attribution_payload LONGTEXT NOT NULL,
    status ENUM('open','closed') NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (lot_id),
    UNIQUE KEY uq_paper_position_buy_fill (buy_fill_id),
    KEY ix_paper_positions_inventory (wallet, token, budget_bucket, status),
    CONSTRAINT fk_paper_position_buy_fill FOREIGN KEY (buy_fill_id)
      REFERENCES paper_fills(fill_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_position_reservations (
    proposal_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    lot_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    token_amount_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    status ENUM('active','consumed','released') NOT NULL,
    PRIMARY KEY (proposal_id, lot_id),
    CONSTRAINT fk_position_reservation_proposal FOREIGN KEY (proposal_id)
      REFERENCES paper_proposals(proposal_id),
    CONSTRAINT fk_position_reservation_lot FOREIGN KEY (lot_id)
      REFERENCES paper_positions(lot_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_realized_pnl (
    fill_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    lot_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    principal_asset CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    principal_released_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    proceeds_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    fee_in_principal_asset_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    realized_pnl_raw VARCHAR(81) CHARACTER SET ascii NOT NULL,
    gas_cost_wei VARCHAR(80) CHARACTER SET ascii NOT NULL,
    PRIMARY KEY (fill_id, lot_id),
    CONSTRAINT fk_realized_pnl_fill FOREIGN KEY (fill_id)
      REFERENCES paper_fills(fill_id),
    CONSTRAINT fk_realized_pnl_lot FOREIGN KEY (lot_id)
      REFERENCES paper_positions(lot_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_position_marks (
    mark_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    lot_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    principal_asset CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    token_amount_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    gross_value_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    principal_remaining_raw VARCHAR(80) CHARACTER SET ascii NOT NULL,
    unrealized_pnl_raw VARCHAR(81) CHARACTER SET ascii NOT NULL,
    gas_cost_wei VARCHAR(80) CHARACTER SET ascii NOT NULL,
    block_number BIGINT UNSIGNED NOT NULL,
    block_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    quote_source VARCHAR(100) CHARACTER SET ascii NOT NULL,
    quote_observed_at VARCHAR(64) CHARACTER SET ascii NOT NULL,
    risk_payload LONGTEXT NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (mark_id),
    UNIQUE KEY uq_paper_position_mark (lot_id, block_hash, token_amount_raw),
    CONSTRAINT fk_position_mark_lot FOREIGN KEY (lot_id)
      REFERENCES paper_positions(lot_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS paper_decisions (
    decision_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_event_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    trigger_mode VARCHAR(32) CHARACTER SET ascii NOT NULL,
    strategy_version VARCHAR(100) NOT NULL,
    accepted BOOLEAN NOT NULL,
    reason VARCHAR(255) NULL,
    payload LONGTEXT NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (decision_id),
    UNIQUE KEY uq_paper_decision_source
      (source_event_id, trigger_mode, strategy_version)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS execution_nonce_reservations (
    reservation_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    follower_wallet CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    relationship_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    proposal_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    chain_id BIGINT UNSIGNED NOT NULL,
    nonce BIGINT UNSIGNED NOT NULL,
    status ENUM('reserved','signed','broadcast','confirmed','released') NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (reservation_id),
    UNIQUE KEY uq_execution_nonce_proposal (proposal_id),
    UNIQUE KEY uq_execution_nonce_wallet (follower_wallet, chain_id, nonce)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS execution_plans (
    plan_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    proposal_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    follower_wallet CHAR(42) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    relationship_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    config_snapshot_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    nonce_reservation_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    status ENUM('prepared','signed','cancelled') NOT NULL,
    plan_payload LONGTEXT NOT NULL,
    preflight_payload LONGTEXT NOT NULL,
    signed_tx_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NULL,
    final_review_payload LONGTEXT NULL,
    plan_integrity_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (plan_id),
    UNIQUE KEY uq_execution_plan_proposal (proposal_id),
    UNIQUE KEY uq_execution_plan_nonce (nonce_reservation_id),
    CONSTRAINT fk_execution_plan_proposal FOREIGN KEY (proposal_id)
      REFERENCES paper_proposals(proposal_id),
    CONSTRAINT fk_execution_plan_nonce FOREIGN KEY (nonce_reservation_id)
      REFERENCES execution_nonce_reservations(reservation_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS execution_attempts (
    tx_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    plan_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    replaces_tx_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NULL,
    nonce BIGINT UNSIGNED NOT NULL,
    status ENUM('signed','observed_pending','confirmed','reverted','replaced','orphaned') NOT NULL,
    public_payload LONGTEXT NOT NULL,
    block_number BIGINT UNSIGNED NULL,
    block_hash CHAR(66) CHARACTER SET ascii COLLATE ascii_bin NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (tx_hash),
    KEY ix_execution_attempts_plan (plan_id, created_at),
    CONSTRAINT fk_execution_attempt_plan FOREIGN KEY (plan_id)
      REFERENCES execution_plans(plan_id),
    CONSTRAINT fk_execution_attempt_parent FOREIGN KEY (replaces_tx_hash)
      REFERENCES execution_attempts(tx_hash)
) ENGINE=InnoDB;

GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.signals
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.candidates
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.chain_cursors
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.canonical_blocks
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.candidate_inclusions
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.solver_order_evidence
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_budget_cycles
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_budgets
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_proposals
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_reservations
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_orders
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_fills
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_positions
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_position_reservations
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_realized_pnl
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_position_marks
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.paper_decisions
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.execution_nonce_reservations
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.execution_plans
  TO 'smart_money_runtime'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.execution_attempts
  TO 'smart_money_runtime'@'%';
FLUSH PRIVILEGES;
