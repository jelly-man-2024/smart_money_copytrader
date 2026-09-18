"""Live execution resolves its chain-bound pieces, and refuses a wrong-chain plan."""
import unittest
from unittest.mock import patch

from smart_money import registry as R
from smart_money.approval import approval_spenders
from smart_money.broadcast import MainnetBroadcaster
from smart_money.execution_prep import (
    UnsignedExecutionPlan, aggregator_execution_targets, execution_targets,
    local_execution_targets)


def plan(chain_id, to, provider="zeroex"):
    return UnsignedExecutionPlan(
        follower_wallet="0x3004ab92565deeea0a2eaa27e40e297bb457e1a6",
        relationship_id="10", proposal_id="p" * 64, to=to, data="0x1234",
        value_raw="0", input_asset=R.ARC.usdc_erc20,
        amount_in_raw="100000", minimum_amount_out_raw="1", gas_limit=300000,
        max_fee_per_gas="1", max_priority_fee_per_gas="0", quote_observed_at=0.0,
        quote_block_number=1, quote_block_hash="0x" + "11" * 32,
        deadline=2 ** 40, chain_id=chain_id, execution_provider=provider)


class ChainBoundExecutionTests(unittest.TestCase):
    def test_targets_come_from_the_plan_chain(self):
        self.assertIn(R.ROBINHOOD.v3_router, local_execution_targets(R.CHAIN_ID))
        # Arc has a verified Universal Router but no v2/v3, so only that one.
        self.assertEqual(local_execution_targets(R.ARC.chain_id),
                         frozenset({R.ARC.universal_router}))
        self.assertNotIn(R.ROBINHOOD.v3_router, local_execution_targets(R.ARC.chain_id))
        self.assertIn(R.ARC.zero_x_allowance_holder,
                      aggregator_execution_targets(R.ARC.chain_id))

    def test_an_unknown_chain_plan_is_refused(self):
        targets = execution_targets(R.ARC.chain_id)
        with self.assertRaisesRegex(ValueError, "target or chain is not allowed"):
            plan(999999, R.ARC.zero_x_allowance_holder).validate(targets)

    def test_a_plan_targeting_another_chains_router_is_refused(self):
        # The Robinhood Kyber router is not an Arc execution target, and Arc is
        # where this plan claims to run.
        targets = execution_targets(R.ARC.chain_id)
        with self.assertRaisesRegex(ValueError, "target or chain is not allowed"):
            plan(R.ARC.chain_id, R.KYBER_META_AGGREGATION_ROUTER_V2).validate(targets)

    def test_a_provider_the_chain_lacks_is_refused(self):
        targets = execution_targets(R.ARC.chain_id) | {R.KYBER_META_AGGREGATION_ROUTER_V2}
        with self.assertRaisesRegex(ValueError, "provider does not match"):
            plan(R.ARC.chain_id, R.KYBER_META_AGGREGATION_ROUTER_V2, "kyber").validate(targets)

    def test_broadcaster_is_bound_to_one_chain_and_its_endpoint(self):
        with patch.dict("os.environ", {"ARC_RPC_URL": "https://arc.invalid/rpc",
                                       "ROBINHOOD_RPC_URL": "https://rh.invalid/rpc"}):
            arc = MainnetBroadcaster(chain_id=R.ARC.chain_id)
            self.addCleanup(arc.close)
            self.assertEqual(arc.chain_id, R.ARC.chain_id)
            self.assertEqual(arc.endpoint, "https://arc.invalid/rpc")
            rh = MainnetBroadcaster()
            self.addCleanup(rh.close)
            self.assertEqual(rh.endpoint, "https://rh.invalid/rpc")

    def test_arc_can_only_ever_approve_the_one_router_it_has(self):
        # Arc registers no v2, v3 or Kyber router, so 0x is the only spender it
        # can approve at all; each approval is still bounded by the position or
        # the proposal, never unlimited.
        self.assertEqual(approval_spenders(R.ARC.chain_id),
                         frozenset({R.ARC.zero_x_allowance_holder}))
        self.assertIn(R.ZERO_X_ALLOWANCE_HOLDER, approval_spenders(R.CHAIN_ID))


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
