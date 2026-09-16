-- Multi-chain support, step 2: make canonical chain state keys chain-scoped.
--
-- PRECONDITIONS FOR AN EXISTING DATABASE:
--   * Stop every writer that calls chain_cursor/record_chain_block/rewind_chain.
--   * Confirm migration 011 has completed and all existing rows are chain 4663.
--   * Take a backup and allow a maintenance window. Both ALTERs replace primary
--     keys; canonical_blocks can be large and InnoDB may rebuild the table.
--   * Do not start shared-ledger Arc ingestion until the runtime's chain-scoped
--     Store methods have landed. The Phase-1 `arc-monitor` intentionally uses
--     its own SQLite ledger and does not depend on this migration.
--
-- Apply exactly once. New Docker volumes run 003 -> 011 -> 012 in order.

ALTER TABLE chain_cursors
  DROP PRIMARY KEY,
  ADD PRIMARY KEY (chain_id, name);

ALTER TABLE canonical_blocks
  DROP INDEX uq_canonical_block_hash,
  DROP PRIMARY KEY,
  ADD PRIMARY KEY (chain_id, block_number),
  ADD UNIQUE KEY uq_canonical_block_hash (chain_id, block_hash);
