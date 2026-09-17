-- Multi-chain support, step 3: let a relationship exist per chain, and let a
-- chain settle in USDC.
--
-- Why each change is needed before Arc can be configured:
--   * uq_copy_relationship keyed (follower_wallet, smart_wallet) alone means the
--     same follower cannot copy the same smart wallet on a second chain; the
--     insert is rejected outright. The key becomes chain-scoped.
--   * The budget bucket enums cannot store 'USDC'. Arc settles in USDC and has
--     no wrapped native asset, so it has no USDG or ETH_WETH bucket to use.
--
-- 'USDC' is APPENDED to each enum so the existing members keep their ordinals
-- and no stored row changes meaning; MySQL can then do these in place.
--
-- PRECONDITIONS FOR AN EXISTING DATABASE:
--   * Migrations 011 and 012 have completed.
--   * Take a backup. The tables are small, but the unique key is rebuilt.
--   * The runtime must already resolve bucket names per chain, otherwise an Arc
--     relationship would be read with Robinhood's bucket names.
--
-- Apply exactly once. New Docker volumes run 003 -> 011 -> 012 -> 013 in order.

ALTER TABLE copy_relationships
  DROP INDEX uq_copy_relationship,
  ADD UNIQUE KEY uq_copy_relationship (chain_id, follower_wallet, smart_wallet);

ALTER TABLE paper_budgets
  MODIFY bucket ENUM('USDG','ETH_WETH','USDC') NOT NULL;

ALTER TABLE paper_positions
  MODIFY budget_bucket ENUM('USDG','ETH_WETH','USDC') NOT NULL;

ALTER TABLE paper_proposals
  MODIFY budget_bucket ENUM('USDG','ETH_WETH','USDC') NOT NULL;

ALTER TABLE paper_reservations
  MODIFY bucket ENUM('USDG','ETH_WETH','USDC') NOT NULL;
