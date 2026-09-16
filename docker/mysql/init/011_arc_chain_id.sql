-- Multi-chain support, step 1: tag every chain-scoped ledger row with chain_id.
--
-- The runtime was single-chain (Robinhood Chain, 4663). Adding Arc (5042) means
-- rows for two chains share these tables, so each needs a chain discriminator.
-- (execution_nonce_reservations already carries chain_id from migration 003.)
--
-- This migration is intentionally ADDITIVE ONLY: it appends a NOT NULL column
-- with DEFAULT 4663 to each table. On MySQL 8 an end-of-table add with a literal
-- default uses ALGORITHM=INSTANT -- it is near-instant and does not lock the
-- table, so it is safe to apply while `sm-copy run` is live. Existing rows
-- backfill to 4663 (they are all Robinhood Chain).
--
-- DEFERRED to the Arc-ingestion migration (a maintenance window, before any Arc
-- row is written), because they rebuild tables / change keys and are NOT online:
--   * canonical_blocks: PRIMARY KEY (block_number) -> (chain_id, block_number)
--       Arc and Robinhood block numbers collide; the PK MUST change before Arc
--       backfill writes any canonical_blocks row.
--   * chain_cursors: PRIMARY KEY (name) -> (chain_id, name).
--   * paper_budgets: PRIMARY KEY (cycle_id, wallet, bucket) -> prefix chain_id;
--       also widen the bucket domain to include Arc's 'USDC' when Arc execution
--       lands (today's buckets are 'USDG'/'ETH_WETH').
--   * copy_relationships: uq_copy_relationship (follower_wallet, smart_wallet)
--       -> (chain_id, follower_wallet, smart_wallet); and hot index
--       ix_copy_relationship_enabled_smart (enabled, smart_wallet)
--       -> (enabled, chain_id, smart_wallet). Until then a smart wallet can be
--       followed on only one chain, which fails safe (a duplicate is rejected,
--       not silently merged).
--   * paper_positions: no key change needed (keyed by generated lot_id), but the
--       code that aggregates lots by (wallet, token) must add a chain_id filter
--       before Arc positions exist, or PnL would commingle across chains.
--
-- MySQL has no ADD COLUMN IF NOT EXISTS: apply this migration EXACTLY ONCE on an
-- existing database. New volumes run it automatically via docker init.

ALTER TABLE copy_relationships
  ADD COLUMN chain_id BIGINT UNSIGNED NOT NULL DEFAULT 4663,
  ALGORITHM=INSTANT;

ALTER TABLE paper_budgets
  ADD COLUMN chain_id BIGINT UNSIGNED NOT NULL DEFAULT 4663,
  ALGORITHM=INSTANT;

ALTER TABLE paper_positions
  ADD COLUMN chain_id BIGINT UNSIGNED NOT NULL DEFAULT 4663,
  ALGORITHM=INSTANT;

ALTER TABLE canonical_blocks
  ADD COLUMN chain_id BIGINT UNSIGNED NOT NULL DEFAULT 4663,
  ALGORITHM=INSTANT;

ALTER TABLE chain_cursors
  ADD COLUMN chain_id BIGINT UNSIGNED NOT NULL DEFAULT 4663,
  ALGORITHM=INSTANT;
