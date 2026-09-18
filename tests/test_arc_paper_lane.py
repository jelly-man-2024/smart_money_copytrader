"""The Arc paper lane refuses to start in any configuration it cannot honour."""
import asyncio
import os
import unittest
from argparse import Namespace
from dataclasses import replace
from unittest.mock import patch

from smart_money import registry as R
from smart_money.cli import arc_paper
from smart_money.paper import AmountRule
from smart_money.paper_config import PaperConfig, WalletPaperPolicy
from smart_money.quotes import QuotePolicy

SMART = "0x89909912c58e2182d92b1a8638d6ff8d965e173b"
FOLLOWER = "0x3004ab92565deeea0a2eaa27e40e297bb457e1a6"


def policy(rel, chain, run_mode, bucket, asset):
    return WalletPaperPolicy(
        wallet=SMART, label="s", follower_wallet=FOLLOWER, relationship_id=rel,
        run_mode=run_mode, budget_limits={bucket: "5000000"},
        buy_rules={bucket: AmountRule("fixed", fixed_amount_raw="100000")},
        sell_rule=AmountRule("proportional", ratio_ppm=1_000_000),
        strategy_version=f"v-{rel}", trigger_mode="evidenced", shadow_trigger_modes=(),
        quote_policy=QuotePolicy(), allowed_protocols=frozenset({"v4"}),
        allowed_assets=frozenset({asset}), allowed_routes=frozenset(),
        route_definitions=(), snapshot_hash="h" * 64,
        execution_providers=("zeroex",), chain_id=chain)


def config(*relationships):
    return PaperConfig(
        strategy_version="v", trigger_mode="evidenced", shadow_trigger_modes=(),
        quote_policy=QuotePolicy(), allowed_protocols=frozenset({"v4"}),
        allowed_assets=frozenset(), allowed_routes=frozenset(), route_definitions=(),
        wallets={SMART: relationships[0]} if relationships else {},
        relationships=relationships, snapshot_hash="h" * 64)


ARC_PAPER = policy("10", R.ARC.chain_id, "paper", "USDC", R.ARC.usdc_erc20)
RH_LIVE = policy("9", R.CHAIN_ID, "mainnet_live", "USDG", R.USDG)


def args(**overrides):
    base = dict(watchlist="data/fomo_watchlist.csv", no_relay=True, seconds=1,
                backfill_interval=15.0, backfill_batch=500,
                paper_cycle_action="reuse", paper_cycle_id=None,
                paper_cycle_reason=None, max_tax_bps=500,
                max_round_trip_loss_bps=1500, probe_amount_raw=1_000_000)
    base.update(overrides)
    return Namespace(**base)


class StartupGateTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ARC_RPC_URL": "https://arc.invalid/rpc",
            "ARC_WS_URL": "wss://arc.invalid/ws",
            "0X_API_KEY": "synthetic-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Nothing below may reach the ledger or the network.
        self.store = patch("smart_money.cli.MySqlStore",
                           side_effect=AssertionError("ledger opened despite a refusal"))
        self.store.start()
        self.addCleanup(self.store.stop)

    def run_lane(self, paper_config, **overrides):
        with patch("smart_money.cli.load_mysql_paper_config", return_value=paper_config), \
             patch("smart_money.cli.load_endpoint_env"):
            return asyncio.run(arc_paper(args(**overrides)))

    def test_refuses_a_live_arc_relationship(self):
        # The lane has no execution surface, so a live Arc relationship would
        # otherwise be silently downgraded to paper instead of refused.
        live_arc = replace(ARC_PAPER, run_mode="mainnet_live")
        with self.assertRaisesRegex(ValueError, "not paper mode"):
            self.run_lane(config(live_arc, RH_LIVE))

    def test_refuses_when_no_relationship_copies_arc(self):
        with self.assertRaisesRegex(ValueError, "copies Arc"):
            self.run_lane(config(RH_LIVE))

    def test_requires_both_arc_endpoints(self):
        for missing in ("ARC_RPC_URL", "ARC_WS_URL"):
            with self.subTest(missing=missing), patch.dict(os.environ, {missing: ""}):
                with self.assertRaisesRegex(ValueError, "ARC_RPC_URL and ARC_WS_URL"):
                    self.run_lane(config(ARC_PAPER, RH_LIVE))

    def test_requires_the_0x_key_because_arc_executes_through_0x(self):
        with patch.dict(os.environ, {"0X_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "0X_API_KEY"):
                self.run_lane(config(ARC_PAPER, RH_LIVE))

    def test_a_live_robinhood_relationship_alone_never_enters_the_arc_lane(self):
        # Same smart wallet, two chains: the Arc lane must carry only rel 10.
        selected = config(ARC_PAPER, RH_LIVE).policies_for(SMART, R.ARC.chain_id)
        self.assertEqual([p.relationship_id for p in selected], ["10"])


if __name__ == "__main__":
    unittest.main()
