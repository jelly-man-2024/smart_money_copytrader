"""Quoting resolves venues from the signal's chain, never from a pinned chain."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from eth_abi import encode

from smart_money import registry as R
from smart_money.models import Signal
from smart_money.quotes import LiveQuoter, _erc20, _venue

HASH = "0x" + "11" * 32
WALLET = "0x3004ab92565deeea0a2eaa27e40e297bb457e1a6"
TOKEN = "0x1111111111111111111111111111111111111111"
HEADER = {"number": "0x10", "hash": "0x" + "22" * 32}


def swap_signal(chain_id, protocol, **kwargs):
    return Signal(HASH, WALLET, "third_party", "BUY", "incoming", TOKEN, "0x0a2b8f36",
                  stage="swap_evidenced", execution_status="success",
                  canonical_status="confirmed", protocol=protocol, exact_in=True,
                  token_in=R.ARC.usdc_erc20 if chain_id == R.ARC.chain_id else R.USDG,
                  token_out=TOKEN, chain_id=chain_id, **kwargs)


class ChainResolutionTests(unittest.IsolatedAsyncioTestCase):
    def test_native_sentinel_maps_to_each_chain_own_erc20(self):
        # Robinhood wraps ETH; Arc's native gas asset IS USDC, so the sentinel
        # must not resolve to the Robinhood WETH address on an Arc signal.
        self.assertEqual(_erc20(R.NATIVE, R.ROBINHOOD), R.ROBINHOOD.weth)
        self.assertEqual(_erc20(R.NATIVE, R.ARC), R.ARC.usdc_erc20)
        self.assertNotEqual(_erc20(R.NATIVE, R.ARC), _erc20(R.NATIVE, R.ROBINHOOD))
        self.assertEqual(_erc20(TOKEN, R.ARC), TOKEN)  # plain ERC-20 is untouched

    def test_missing_venue_is_refused_rather_than_borrowed(self):
        with self.assertRaises(ValueError) as caught:
            _venue(R.ARC, "v3_quoter")
        self.assertIn("5042", str(caught.exception))
        self.assertEqual(_venue(R.ROBINHOOD, "v3_quoter"), R.ROBINHOOD.v3_quoter)

    async def test_arc_v4_quote_uses_the_arc_registry_quoter(self):
        rpc = SimpleNamespace(call=AsyncMock(return_value="0x" + encode(
            ["uint256", "uint256"], [777, 21000]).hex()))
        signal = swap_signal(R.ARC.chain_id, "v4", evidence={
            "pool_key": [R.ARC.usdc_erc20, TOKEN, 3000, 60, R.NATIVE], "hook_data": "0x"})
        quote = await LiveQuoter(rpc)._quote_at(signal, "1000000", HEADER)
        self.assertEqual(quote.amount_out_raw, "777")
        self.assertEqual(quote.source, R.ARC.v4_quoter)
        self.assertEqual(rpc.call.await_args.args[1][0]["to"], R.ARC.v4_quoter)

    async def test_arc_v3_quote_refuses_instead_of_hitting_the_robinhood_quoter(self):
        # Arc has no v3 deployment yet. Borrowing the Robinhood address would be
        # actively dangerous: these chains already share a CREATE2 v4 address, so
        # an unrelated contract can answer at the same address on Arc.
        rpc = SimpleNamespace(call=AsyncMock())
        signal = swap_signal(R.ARC.chain_id, "v3", evidence={"fee": 3000})
        with self.assertRaises(ValueError) as caught:
            await LiveQuoter(rpc)._quote_at(signal, "1000000", HEADER)
        self.assertIn("v3_quoter", str(caught.exception))
        rpc.call.assert_not_awaited()

    async def test_unsupported_chain_id_is_rejected(self):
        rpc = SimpleNamespace(call=AsyncMock())
        signal = swap_signal(R.ARC.chain_id, "v2", evidence={"route": [R.USDG, TOKEN]})
        signal = type(signal)(**{**signal.__dict__, "chain_id": 999999})
        with self.assertRaises(ValueError) as caught:
            await LiveQuoter(rpc)._quote_at(signal, "1000000", HEADER)
        self.assertIn("unsupported chain id", str(caught.exception))
        rpc.call.assert_not_awaited()

    async def test_robinhood_v3_quote_still_uses_the_robinhood_quoter(self):
        rpc = SimpleNamespace(call=AsyncMock(
            return_value="0x" + (555).to_bytes(32, "big").hex()))
        signal = swap_signal(R.ROBINHOOD.chain_id, "v3", evidence={"fee": 3000})
        quote = await LiveQuoter(rpc)._quote_at(signal, "1000000", HEADER)
        self.assertEqual(quote.amount_out_raw, "555")
        self.assertEqual(quote.source, R.ROBINHOOD.v3_quoter)
        self.assertEqual(rpc.call.await_args.args[1][0]["to"], R.ROBINHOOD.v3_quoter)


if __name__ == "__main__":
    unittest.main()
