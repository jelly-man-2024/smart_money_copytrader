-- Apply once to databases created before mainnet_live was introduced.
ALTER TABLE copy_relationships
  DROP CHECK chk_copy_relationship_mode,
  DROP CHECK chk_copy_relationship_trigger;

ALTER TABLE copy_relationships
  ADD CONSTRAINT chk_copy_relationship_mode
    CHECK (run_mode IN ('paper','mainnet_live')),
  ADD CONSTRAINT chk_copy_relationship_trigger
    CHECK (trigger_mode IN ('feed_intent','receipt_success','swap_evidenced',
                            'relay_sell_evidenced','relay_buy_evidenced'));
