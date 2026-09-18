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
SMART = "0x89909912c58e2182d92b1a8638d6ff8d965e173b"


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


class ChainScopedPolicyTests(unittest.TestCase):
    """A relationship declares its chain, and its buckets must exist there."""

    def policy_row(self, chain_id, limits, rules):
        return {
            "wallet": "0x89909912c58e2182d92b1a8638d6ff8d965e173b",
            "label": "test", "follower_wallet": WALLET, "relationship_id": "9",
            "run_mode": "paper", "chain_id": chain_id,
            "budget_limits": limits, "buy_rules": rules,
            "sell_rule": {"mode": "proportional", "ratio_ppm": 1000000},
        }

    def parse(self, row):
        from smart_money.paper_config import parse_paper_config
        return parse_paper_config({
            "version": 1, "strategy_version": "v1", "trigger_mode": "evidenced",
            "quote_policy": {},
            "allowed_protocols": ["v4"], "allowed_assets": [TOKEN],
            "allowed_routes": [], "wallets": [row]})

    def test_an_arc_relationship_uses_the_usdc_bucket(self):
        config = self.parse(self.policy_row(
            R.ARC.chain_id, {"USDC": "10000000"},
            {"USDC": {"mode": "fixed", "fixed_amount_raw": "100000"}}))
        policy = config.relationships[0]
        self.assertEqual(policy.chain_id, R.ARC.chain_id)
        self.assertEqual(set(policy.budget_limits), {"USDC"})
        # The chain survives snapshot hashing, which rebuilds every policy.
        self.assertTrue(policy.snapshot_hash)

    def test_robinhood_keeps_its_own_buckets(self):
        policy = self.parse(self.policy_row(
            R.CHAIN_ID, {"USDG": "10000000", "ETH_WETH": "1"},
            {"USDG": {"mode": "fixed", "fixed_amount_raw": "100000"},
             "ETH_WETH": {"mode": "fixed", "fixed_amount_raw": "1"}})).relationships[0]
        self.assertEqual(policy.chain_id, R.CHAIN_ID)
        self.assertEqual(set(policy.budget_limits), {"USDG", "ETH_WETH"})

    def test_a_bucket_the_chain_does_not_have_is_refused(self):
        # Arc has no USDG and no wrapped native, so neither bucket exists there.
        for limits, rules in (
                ({"USDG": "10000000"}, {"USDG": {"mode": "fixed", "fixed_amount_raw": "1"}}),
                ({"ETH_WETH": "1"}, {"ETH_WETH": {"mode": "fixed", "fixed_amount_raw": "1"}})):
            with self.subTest(bucket=list(limits)[0]):
                with self.assertRaisesRegex(ValueError, "supports buckets"):
                    self.parse(self.policy_row(R.ARC.chain_id, limits, rules))

    def test_an_unknown_chain_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unsupported chain id"):
            self.parse(self.policy_row(
                999999, {"USDG": "1"}, {"USDG": {"mode": "fixed", "fixed_amount_raw": "1"}}))


class MySqlRelationshipLoadingTests(unittest.TestCase):
    """The same pair on two chains is two relationships, not a duplicate."""

    def rows(self):
        import json
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        template = json.loads((root / "config/paper.example.json").read_text())
        policy = template["wallets"][0]
        base = {
            "follower_wallet": WALLET, "follower_label": "follower",
            "smart_wallet": "0x" + "22" * 20, "smart_wallet_label": "smart-a",
            "run_mode": "paper", "strategy_version": template["strategy_version"],
            "trigger_mode": template["trigger_mode"],
            "shadow_trigger_modes": template["shadow_trigger_modes"],
            "quote_policy": template["quote_policy"],
            "allowed_protocols": template["allowed_protocols"],
            "allowed_assets": template["allowed_assets"],
            "allowed_routes": template["allowed_routes"],
            "usdg_rule_mode": policy["buy_rules"]["USDG"]["mode"],
            "usdg_fixed_amount_raw": policy["buy_rules"]["USDG"]["fixed_amount_raw"],
            "usdg_ratio_ppm": None,
            "usdg_budget_limit_raw": policy["budget_limits"]["USDG"],
            "eth_rule_mode": policy["buy_rules"]["ETH_WETH"]["mode"],
            "eth_fixed_amount_raw": None,
            "eth_ratio_ppm": policy["buy_rules"]["ETH_WETH"]["ratio_ppm"],
            "eth_budget_limit_raw": policy["budget_limits"]["ETH_WETH"],
            "sell_rule_mode": policy["sell_rule"]["mode"],
            "sell_fixed_amount_raw": None,
            "sell_ratio_ppm": policy["sell_rule"]["ratio_ppm"],
        }
        return [dict(base, id=1, chain_id=R.CHAIN_ID),
                dict(base, id=2, chain_id=R.ARC.chain_id,
                     allowed_assets=[R.ARC.usdc_erc20], allowed_routes=[])]

    def load(self, rows):
        import unittest.mock as mock
        from smart_money import mysql_config

        class Cursor:
            def execute(self, *args): pass
            def fetchall(self): return rows
            def __enter__(self): return self
            def __exit__(self, *args): return False

        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass

        with mock.patch.object(mysql_config, "mysql_connection",
                               return_value=Connection()):
            return mysql_config.load_mysql_paper_config()

    def test_one_pair_on_two_chains_loads_as_two_relationships(self):
        config = self.load(self.rows())
        by_chain = {p.chain_id: p for p in config.relationships}
        self.assertEqual(set(by_chain), {R.CHAIN_ID, R.ARC.chain_id})
        self.assertEqual(set(by_chain[R.CHAIN_ID].budget_limits), {"USDG", "ETH_WETH"})
        self.assertEqual(set(by_chain[R.ARC.chain_id].budget_limits), {"USDC"})
        # Their ledgers are separate, so neither can spend the other's budget.
        self.assertNotEqual(by_chain[R.CHAIN_ID].ledger_scope,
                            by_chain[R.ARC.chain_id].ledger_scope)

    def test_the_same_pair_twice_on_one_chain_is_still_refused(self):
        rows = self.rows()
        rows[1]["chain_id"] = R.CHAIN_ID
        rows[1]["allowed_assets"] = rows[0]["allowed_assets"]
        with self.assertRaisesRegex(ValueError, "duplicate enabled"):
            self.load(rows)


class ChainScopedPolicyLookupTests(unittest.TestCase):
    """The same smart wallet on two chains must never share a policy."""

    def config(self):
        from smart_money.paper_config import PaperConfig, WalletPaperPolicy
        from smart_money.paper import AmountRule
        from smart_money.quotes import QuotePolicy

        def policy(rel, chain, run_mode, bucket, asset):
            return WalletPaperPolicy(
                wallet=SMART, label="s", follower_wallet=WALLET, relationship_id=rel,
                run_mode=run_mode, budget_limits={bucket: "5000000"},
                buy_rules={bucket: AmountRule("fixed", fixed_amount_raw="100000")},
                sell_rule=AmountRule("proportional", ratio_ppm=1_000_000),
                strategy_version=f"v-{rel}", trigger_mode="evidenced",
                shadow_trigger_modes=(), quote_policy=QuotePolicy(),
                allowed_protocols=frozenset({"v4"}), allowed_assets=frozenset({asset}),
                allowed_routes=frozenset(), route_definitions=(), snapshot_hash="h" * 64,
                execution_providers=("zeroex",), chain_id=chain)

        relationships = (policy("9", R.CHAIN_ID, "mainnet_live", "USDG", R.USDG),
                         policy("10", R.ARC.chain_id, "paper", "USDC", R.ARC.usdc_erc20))
        return PaperConfig(
            strategy_version="v", trigger_mode="evidenced", shadow_trigger_modes=(),
            quote_policy=QuotePolicy(), allowed_protocols=frozenset({"v4"}),
            allowed_assets=frozenset(), allowed_routes=frozenset(), route_definitions=(),
            wallets={SMART: relationships[0]}, relationships=relationships,
            snapshot_hash="h" * 64)

    def test_a_signal_selects_only_its_own_chain_policy(self):
        config = self.config()
        arc = config.policies_for(SMART, R.ARC.chain_id)
        self.assertEqual([p.relationship_id for p in arc], ["10"])
        # The decisive property: an Arc signal must not reach the live RH policy.
        self.assertNotIn("mainnet_live", [p.run_mode for p in arc])
        rh = config.policies_for(SMART, R.CHAIN_ID)
        self.assertEqual([p.relationship_id for p in rh], ["9"])

    def test_chain_id_is_required_not_defaulted(self):
        with self.assertRaises(TypeError):
            self.config().policies_for(SMART)
        with self.assertRaises(ValueError):
            self.config().policies_for(SMART, "4663")

    def test_unknown_chain_selects_nothing(self):
        self.assertEqual(self.config().policies_for(SMART, 999999), ())
