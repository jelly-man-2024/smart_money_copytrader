"""Arc round-trip pool gate: no network, deterministic fake quoter responses."""
import unittest
from types import SimpleNamespace

from eth_abi import encode

from smart_money.arc_pool_safety import (
    ArcPoolSafetyPolicy, TAX_HOOK_CODE_SIZE, TAX_HOOK_RATE_OFFSET,
    read_hook_tax_bps, verify_arc_exit_via_aggregator, verify_arc_pool_sellable)
from smart_money.registry import ARC
from smart_money.rpc import RpcError
from smart_money.zeroex import ZeroExApiError

USDC = ARC.usdc_erc20
TOKEN = "0x1111111111111111111111111111111111111111"
HOOK = "0x2222222222222222222222222222222222222044"
ZERO_HOOK = "0x0000000000000000000000000000000000000000"
# currency0 < currency1 by address order, and USDC (0x36..) sorts below TOKEN? No:
# 0x11.. < 0x36.., so the token is currency0 and USDC is currency1 here.
KEY = [TOKEN, USDC, 3000, 60, HOOK]


def tax_code(bps):
    body = bytearray(TAX_HOOK_CODE_SIZE)
    body[TAX_HOOK_RATE_OFFSET:TAX_HOOK_RATE_OFFSET + 2] = bps.to_bytes(2, "big")
    return "0x" + body.hex()


def quote_result(amount_out):
    return "0x" + encode(["uint256", "uint256"], [amount_out, 50_000]).hex()


class FakeRpc:
    """eth_call returns the next scripted quote; a revert is an RpcError with a code."""
    def __init__(self, quotes, code="0x"):
        self.quotes, self.code, self.calls = list(quotes), code, []

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_getCode":
            return self.code
        if method != "eth_call":
            raise AssertionError(f"unexpected read: {method}")
        outcome = self.quotes.pop(0)
        if outcome == "revert":
            raise RpcError("execution reverted", diagnostic={"kind": "rpc_error", "code": 3})
        if outcome == "transport":
            raise RpcError("RPC transport failure", diagnostic={
                "kind": "transport_or_response_error", "code": None})
        return quote_result(outcome)


class ArcPoolSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def verify(self, rpc, **kwargs):
        policy = kwargs.pop("policy", ArcPoolSafetyPolicy())
        return await verify_arc_pool_sellable(
            rpc, KEY, chain_id=ARC.chain_id, policy=policy, **kwargs)

    async def test_symmetric_pool_within_policy_is_accepted(self):
        rpc = FakeRpc([10**18, 940_000], code=tax_code(300))
        result = await self.verify(rpc)
        self.assertTrue(result["accepted"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["tax_bps"], 300)
        self.assertEqual(result["round_trip_loss_bps"], 600)
        # Amounts are decimal strings, and the buy output feeds the sell probe.
        self.assertEqual(result["bought_raw"], str(10**18))
        self.assertEqual(result["returned_raw"], "940000")

    async def test_pool_that_cannot_be_sold_out_of_is_refused(self):
        rpc = FakeRpc([10**18, "revert"], code=tax_code(300))
        result = await self.verify(rpc)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "pool_cannot_be_sold_out_of")
        self.assertEqual(result["bought_raw"], str(10**18))
        self.assertIsNone(result["returned_raw"])

    async def test_buy_side_revert_is_refused_without_probing_the_sell(self):
        rpc = FakeRpc(["revert"])
        result = await self.verify(rpc)
        self.assertEqual(result["reason"], "buy_not_quotable")
        self.assertEqual(len([c for c in rpc.calls if c[0] == "eth_call"]), 1)

    async def test_zero_output_buy_is_refused(self):
        result = await self.verify(FakeRpc([0]))
        self.assertEqual(result["reason"], "buy_not_quotable")

    async def test_tax_above_policy_is_refused_and_threshold_is_configurable(self):
        rpc = FakeRpc([10**18, 990_000], code=tax_code(900))
        self.assertEqual((await self.verify(rpc))["reason"], "tax_rate_above_policy")
        rpc = FakeRpc([10**18, 990_000], code=tax_code(900))
        widened = ArcPoolSafetyPolicy(max_tax_bps=1000)
        self.assertTrue((await self.verify(rpc, policy=widened))["accepted"])

    async def test_round_trip_loss_above_policy_is_refused(self):
        rpc = FakeRpc([10**18, 500_000], code=tax_code(100))
        result = await self.verify(rpc)
        self.assertEqual(result["reason"], "round_trip_loss_above_policy")
        self.assertEqual(result["round_trip_loss_bps"], 5000)
        rpc = FakeRpc([10**18, 500_000], code=tax_code(100))
        widened = ArcPoolSafetyPolicy(max_round_trip_loss_bps=6000)
        self.assertTrue((await self.verify(rpc, policy=widened))["accepted"])

    async def test_probe_amount_is_configurable_and_drives_the_buy_quote(self):
        rpc = FakeRpc([10**18, 5_000_000], code=tax_code(100))
        policy = ArcPoolSafetyPolicy(probe_amount_raw=5_000_000)
        result = await self.verify(rpc, policy=policy)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["probe_amount_raw"], "5000000")
        self.assertEqual(result["round_trip_loss_bps"], 0)
        self.assertIn(b"\x00" * 15 + b"\x4c\x4b\x40", bytes.fromhex(
            rpc.calls[0][1][0]["data"][2:]))  # 5_000_000 encoded as the uint128 input

    async def test_transport_failure_is_never_reported_as_a_pool_verdict(self):
        rpc = FakeRpc([10**18, "transport"])
        with self.assertRaises(RpcError) as caught:
            await self.verify(rpc)
        self.assertEqual(caught.exception.diagnostic["kind"], "transport_or_response_error")

    async def test_pool_not_holding_the_quote_asset_is_refused(self):
        rpc = FakeRpc([])
        result = await verify_arc_pool_sellable(
            rpc, [TOKEN, "0x3333333333333333333333333333333333333333", 3000, 60, HOOK],
            chain_id=ARC.chain_id, policy=ArcPoolSafetyPolicy())
        self.assertEqual(result["reason"], "pool_not_quoted_in_quote_asset")
        self.assertEqual(rpc.calls, [])

    async def test_unknown_hook_shape_reports_unknown_tax_not_zero(self):
        rpc = FakeRpc([10**18, 990_000], code="0x6080")
        result = await self.verify(rpc)
        self.assertTrue(result["accepted"])
        self.assertIsNone(result["tax_bps"])  # unknown, bounded by the loss check instead
        self.assertIsNone(await read_hook_tax_bps(FakeRpc([]), ZERO_HOOK))

    async def test_policy_bounds_are_validated(self):
        for kwargs in [{"max_tax_bps": -1}, {"max_tax_bps": 10_001},
                       {"max_round_trip_loss_bps": 10_001}, {"probe_amount_raw": 0},
                       {"max_tax_bps": 1.5}]:
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                ArcPoolSafetyPolicy(**kwargs)

    async def test_direction_follows_currency_order(self):
        # USDC is currency1 in KEY, so the BUY must be oneForZero (zero_for_one=False)
        # and the sell-back must flip it; getting this backwards would quote garbage.
        rpc = FakeRpc([10**18, 990_000], code=tax_code(100))
        await self.verify(rpc)
        # selector | outer tuple offset | 5 poolKey words | bool word
        directions = [bytes.fromhex(c[1][0]["data"][2:])[4 + 32 + 32*5 + 31]
                      for c in rpc.calls if c[0] == "eth_call"]
        self.assertEqual(directions, [0, 1])


if __name__ == "__main__":
    unittest.main()


class AggregatorExitTests(unittest.IsolatedAsyncioTestCase):
    """The exit test that binds is the one on the venue we would execute on."""

    def client(self, *outcomes):
        from smart_money.zeroex import ZeroExLiquidityUnavailable

        class Client:
            def __init__(self):
                self.calls = []
                self.outcomes = list(outcomes)

            async def route(self, token_in, token_out, amount, *, chain_id):
                self.calls.append((token_in, token_out, amount, chain_id))
                outcome = self.outcomes.pop(0)
                if outcome == "no_liquidity":
                    raise ZeroExLiquidityUnavailable("0x liquidity unavailable")
                if outcome == "transport":
                    raise ZeroExApiError("0x request failed: TimeoutError")
                return SimpleNamespace(amount_out_raw=str(outcome))

        return Client()

    async def verify(self, client, **kwargs):
        policy = kwargs.pop("policy", ArcPoolSafetyPolicy())
        return await verify_arc_exit_via_aggregator(
            client, TOKEN, chain_id=ARC.chain_id, policy=policy, **kwargs)

    async def test_a_routable_round_trip_within_policy_is_accepted(self):
        client = self.client(10**18, 950_000)
        result = await self.verify(client)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["round_trip_loss_bps"], 500)
        # The sell leg is quoted for exactly what the buy leg returned.
        self.assertEqual(client.calls, [
            (USDC, TOKEN, "1000000", ARC.chain_id),
            (TOKEN, USDC, str(10**18), ARC.chain_id)])

    async def test_a_token_0x_will_not_route_back_is_refused(self):
        client = self.client(10**18, "no_liquidity")
        result = await self.verify(client)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "token_cannot_be_sold_out_of")
        self.assertEqual(result["bought_raw"], str(10**18))
        self.assertIsNone(result["returned_raw"])

    async def test_an_unroutable_buy_is_refused_without_a_sell_probe(self):
        client = self.client("no_liquidity")
        result = await self.verify(client)
        self.assertEqual(result["reason"], "buy_not_quotable")
        self.assertEqual(len(client.calls), 1)

    async def test_loss_above_policy_is_refused_and_configurable(self):
        result = await self.verify(self.client(10**18, 700_000))
        self.assertEqual(result["reason"], "round_trip_loss_above_policy")
        self.assertEqual(result["round_trip_loss_bps"], 3000)
        widened = ArcPoolSafetyPolicy(max_round_trip_loss_bps=3500)
        self.assertTrue(
            (await self.verify(self.client(10**18, 700_000), policy=widened))["accepted"])

    async def test_a_failed_request_is_never_a_verdict_about_the_token(self):
        with self.assertRaises(ZeroExApiError):
            await self.verify(self.client("transport"))
        with self.assertRaises(ZeroExApiError):
            await self.verify(self.client(10**18, "transport"))

    async def test_the_quote_asset_defaults_to_the_chain_settlement_asset(self):
        client = self.client(10**18, 1_000_000)
        result = await self.verify(client)
        self.assertEqual(result["quote_asset"], ARC.settlement_asset)
        self.assertEqual(result["quote_asset"], USDC)
        self.assertEqual(result["venue"], "zeroex")
