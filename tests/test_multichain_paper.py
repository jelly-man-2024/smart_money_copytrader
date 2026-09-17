"""Paper policy resolves budgets and execution routes on the signal's own chain."""
import unittest

from smart_money import registry as R
from smart_money.models import Signal
from smart_money.paper import (
    BUDGET_BUCKETS, aggregator_route_definition, aggregator_routers, budget_bucket,
    execution_quote_signal)

HASH = "0x" + "33" * 32
WALLET = "0x3004ab92565deeea0a2eaa27e40e297bb457e1a6"
TOKEN = "0x1111111111111111111111111111111111111111"


def signal_on(chain_id, token_in, **kwargs):
    return Signal(HASH, WALLET, "third_party", "BUY", "incoming", TOKEN, "0x0a2b8f36",
                  stage="swap_evidenced", execution_status="success",
                  canonical_status="confirmed", protocol="relay_solver", exact_in=True,
                  token_in=token_in, token_out=TOKEN, chain_id=chain_id, **kwargs)


class ChainScopedBudgetTests(unittest.TestCase):
    def test_buckets_are_resolved_per_chain(self):
        self.assertEqual(budget_bucket(R.USDG, R.CHAIN_ID), "USDG")
        self.assertEqual(budget_bucket(R.WETH, R.CHAIN_ID), "ETH_WETH")
        self.assertEqual(budget_bucket(R.NATIVE, R.CHAIN_ID), "ETH_WETH")
        # Arc has no WETH: its gas asset and its quote asset are both USDC.
        self.assertEqual(budget_bucket(R.ARC.usdc_erc20, R.ARC.chain_id), "USDC")
        self.assertEqual(budget_bucket(R.NATIVE, R.ARC.chain_id), "USDC")
        self.assertIn("USDC", BUDGET_BUCKETS)

    def test_an_asset_from_another_chain_has_no_bucket(self):
        # Same sentinel, different meaning per chain; a Robinhood-only asset must
        # not fall into an Arc budget just because the address parses.
        self.assertIsNone(budget_bucket(R.USDG, R.ARC.chain_id))
        self.assertIsNone(budget_bucket(R.ARC.usdc_erc20, R.CHAIN_ID))
        self.assertIsNone(budget_bucket(TOKEN, R.ARC.chain_id))

    def test_unsupported_chain_is_rejected(self):
        with self.assertRaises(ValueError):
            budget_bucket(R.USDG, 999999)


class ChainScopedRouteTests(unittest.TestCase):
    def test_aggregator_routers_only_expose_what_the_chain_has(self):
        self.assertEqual(set(aggregator_routers(R.CHAIN_ID)), {"kyber", "zeroex"})
        # 0x routes Arc (its AllowanceHolder is deployed there with the same
        # bytecode), but no Arc Kyber router is registered yet, so Arc exposes
        # 0x only rather than borrowing Robinhood's Kyber router.
        self.assertEqual(aggregator_routers(R.ARC.chain_id),
                         {"zeroex": R.ARC.zero_x_allowance_holder})
        self.assertEqual(R.ARC.zero_x_allowance_holder, R.ZERO_X_ALLOWANCE_HOLDER)

    def test_aggregator_route_on_a_chain_without_that_router_is_refused(self):
        definition = aggregator_route_definition(R.USDG, TOKEN, "kyber", R.CHAIN_ID)
        self.assertEqual(definition["router"], R.KYBER_META_AGGREGATION_ROUTER_V2)
        with self.assertRaisesRegex(ValueError, "5042 has no kyber router"):
            aggregator_route_definition(R.ARC.usdc_erc20, TOKEN, "kyber", R.ARC.chain_id)
        arc_zeroex = aggregator_route_definition(
            R.ARC.usdc_erc20, TOKEN, "zeroex", R.ARC.chain_id)
        self.assertEqual(arc_zeroex["router"], R.ARC.zero_x_allowance_holder)

    def test_arc_v4_execution_route_uses_the_arc_quoter_contract(self):
        signal = signal_on(R.ARC.chain_id, R.ARC.usdc_erc20)
        route = {"protocol": "v4", "assets": [R.ARC.usdc_erc20, TOKEN], "fees": [3000],
                 "tick_spacings": [60], "hooks": [R.NATIVE], "hook_data": ["0x"]}
        selected = execution_quote_signal(signal, (route,))
        self.assertEqual(selected.protocol, "v4")
        self.assertEqual(selected.contract, R.ARC.v4_quoter)
        self.assertEqual(selected.evidence["v4_hops"][0]["pool_key"][:2],
                         sorted([R.ARC.usdc_erc20, TOKEN]))

    def test_arc_v2_execution_route_is_refused_because_arc_has_no_v2(self):
        signal = signal_on(R.ARC.chain_id, R.ARC.usdc_erc20)
        route = {"protocol": "v2", "assets": [R.ARC.usdc_erc20, TOKEN]}
        with self.assertRaisesRegex(ValueError, "5042 has no v2_router"):
            execution_quote_signal(signal, (route,))

    def test_robinhood_v2_route_still_resolves_to_the_robinhood_router(self):
        signal = signal_on(R.CHAIN_ID, R.USDG)
        route = {"protocol": "v2", "assets": [R.USDG, TOKEN]}
        self.assertEqual(execution_quote_signal(signal, (route,)).contract, R.V2_ROUTER)


if __name__ == "__main__":
    unittest.main()
