-- Per-relationship execution route providers, ordered by preference.
-- "local" keeps today's behaviour: only locally verifiable V2/V3/V4 routes execute.
-- Appending "kyber" lets the follower quote and execute through the allowlisted
-- KyberSwap MetaAggregationRouterV2 when no local route exists. The field is part
-- of the configuration snapshot, so changing it requires refreshing
-- live_risk_accepted_at in the same UPDATE. Existing databases must apply this
-- migration exactly once; new volumes run it automatically.
ALTER TABLE copy_relationships
  ADD COLUMN execution_providers JSON NOT NULL DEFAULT (JSON_ARRAY('local'))
    AFTER allowed_routes;
