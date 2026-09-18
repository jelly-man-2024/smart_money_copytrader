"""The exit allowance is granted after a buy, bounded, and never blocking."""
import unittest

from smart_money import registry as R
from smart_money.approval import approval_spenders
from smart_money.cli import EXIT_ALLOWANCE_MULTIPLE
from smart_money.paper import aggregator_routers


class ExitAllowancePolicyTests(unittest.TestCase):
    def test_the_multiple_is_bounded_and_greater_than_the_position(self):
        # Unlimited approvals are the thing this avoids; one position's worth
        # would need a fresh approval on every top-up.
        self.assertGreater(EXIT_ALLOWANCE_MULTIPLE, 1)
        self.assertLessEqual(EXIT_ALLOWANCE_MULTIPLE, 100)

    def test_the_spender_a_sell_would_use_is_approvable_on_both_chains(self):
        # Pre-approving is pointless unless it is the same contract the sell
        # quotes against, and the approval path must accept it.
        for chain in (R.ROBINHOOD, R.ARC):
            with self.subTest(chain=chain.chain_id):
                routers = aggregator_routers(chain.chain_id)
                self.assertIn("zeroex", routers)
                self.assertIn(routers["zeroex"], approval_spenders(chain.chain_id))

    def test_arc_and_robinhood_sell_through_the_same_router(self):
        # Both chains reach 0x at the same AllowanceHolder address, so one
        # approval rule covers them; the plan's chain still decides everything.
        self.assertEqual(aggregator_routers(R.ARC.chain_id)["zeroex"],
                         aggregator_routers(R.CHAIN_ID)["zeroex"])


if __name__ == "__main__":
    unittest.main()
