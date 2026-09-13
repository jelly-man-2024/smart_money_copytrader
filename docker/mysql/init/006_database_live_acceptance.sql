-- One-time migration for databases created before database-owned live acceptance.
-- Existing relationships remain unaccepted until an operator explicitly updates them.
ALTER TABLE copy_relationships
  ADD COLUMN live_risk_accepted_at TIMESTAMP(6) NULL
    AFTER sell_ratio_ppm;
