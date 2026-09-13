ALTER TABLE copy_relationships
  DROP CHECK chk_copy_relationship_trigger;

ALTER TABLE copy_relationships
  ADD CONSTRAINT chk_copy_relationship_trigger CHECK
    (trigger_mode IN ('feed_intent', 'receipt_success', 'swap_evidenced',
                      'relay_sell_evidenced', 'relay_buy_evidenced', 'evidenced'));
