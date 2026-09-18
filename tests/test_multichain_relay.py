"""Relay attribution resolves the funding asset on the delivering chain."""
import unittest
import unittest.mock

from smart_money import registry as R
from smart_money.models import Signal
from smart_money.solver import (
    _funding_normalization, relay_confirmed_sell, relay_delivery_evidence)

SOLANA_CHAIN = 792703809
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def signal_on(chain_id, **kwargs):
    return Signal("0x" + "44" * 32, "0x3004ab92565deeea0a2eaa27e40e297bb457e1a6",
                  "third_party", "BUY", "incoming",
                  "0x1111111111111111111111111111111111111111", "0x0a2b8f36",
                  chain_id=chain_id, **kwargs)


class RelayChainScopeTests(unittest.TestCase):
    def test_arc_carries_relay_and_matches_robinhood_bytecode_addresses(self):
        # Verified on-chain 2026-09-17: same addresses, byte-identical code.
        self.assertEqual(R.ARC.relay_proxy, R.ROBINHOOD.relay_proxy)
        self.assertEqual(R.ARC.relay_router, R.ROBINHOOD.relay_router)
        # The depository was read as a different contract because its code hash
        # differs. It is the same contract: the runtimes differ in 34 bytes, two
        # of which are the chain id itself, so the hashes cannot match while the
        # contract is identical. Comparing hashes alone was the wrong test.
        self.assertEqual(R.ARC.depository, R.ROBINHOOD.depository)
        self.assertEqual(R.ARC.entrypoint, R.ROBINHOOD.entrypoint)

    def test_settlement_asset_is_per_chain(self):
        self.assertEqual(R.ROBINHOOD.settlement_asset, R.USDG)
        self.assertEqual(R.ARC.settlement_asset, R.ARC.usdc_erc20)

    def test_solana_usdc_funding_is_recognised_on_both_chains(self):
        for chain in (R.ROBINHOOD, R.ARC):
            with self.subTest(chain=chain.chain_id):
                self.assertIn((SOLANA_CHAIN, SOLANA_USDC), chain.relay_usdg_equivalents)

    def test_funding_normalization_names_the_assumption_per_chain(self):
        # Robinhood converts between two different assets: operator-approved.
        self.assertEqual(
            _funding_normalization(R.ROBINHOOD, R.USDG, SOLANA_USDC),
            "solana_usdc_6_to_robinhood_usdg_6_operator_approved")
        # Arc settles in USDC itself, so the mapping only renames the asset.
        self.assertEqual(
            _funding_normalization(R.ARC, R.ARC.usdc_erc20, SOLANA_USDC),
            "relay_source_currency_to_chain_5042_settlement_asset")
        # No mapping at all stays an identity on every chain.
        self.assertEqual(_funding_normalization(R.ARC, SOLANA_USDC, SOLANA_USDC), "identity")

    def test_our_own_deposit_flow_still_refuses_other_chains_explicitly(self):
        # relay_delivery_evidence describes a deposit WE make, which needs more
        # than a verified address, so it stays Robinhood-only for now.
        with self.assertRaisesRegex(ValueError, "only evidenced on Robinhood"):
            relay_delivery_evidence({}, signal_on(R.ARC.chain_id))

    def test_a_relay_sell_is_gated_by_that_chain_having_a_depository(self):
        # Arc has a verified depository now, so the chain gate no longer refuses
        # it; the signal's own shape does.
        with self.assertRaisesRegex(ValueError, "not an unclosed successful relay sell"):
            relay_confirmed_sell({}, signal_on(R.ARC.chain_id))
        # A chain without one cannot evidence a relay sell at all.
        bare = R.ChainRegistry(chain_id=7777, name="bare")
        with unittest.mock.patch.dict(R.CHAINS, {7777: bare}):
            with self.assertRaisesRegex(ValueError, "no verified relay depository"):
                relay_confirmed_sell({}, signal_on(7777))


class NativeScaleTests(unittest.TestCase):
    def test_native_and_erc20_scales_are_declared_per_chain(self):
        # Robinhood's ETH and WETH are both 18 decimals: no rescaling.
        self.assertEqual(R.ROBINHOOD.native_to_erc20_divisor, 1)
        # Arc's gas asset is USDC at 18 decimals natively, 6 through the
        # enshrined ERC-20 — a factor of a trillion between the two forms.
        self.assertEqual(R.ARC.native_to_erc20_divisor, 10**12)

    def test_an_inverted_scale_is_rejected_rather_than_silently_inverted(self):
        broken = R.ChainRegistry(chain_id=7777, name="broken",
                                 native_decimals=6, native_erc20_decimals=18)
        with self.assertRaisesRegex(ValueError, "inverted native scale"):
            broken.native_to_erc20_divisor


if __name__ == "__main__":
    unittest.main()
