"""Offline latency-path contracts; no keys, database services or live RPC."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from smart_money import registry as R
from smart_money.kyber import KyberRoute
from smart_money.models import Signal
from smart_money.paper import PaperEngine, aggregator_route_definition, execution_quote_signal
from smart_money.quotes import LiveQuoter, Quote, QuotePolicy, assess_market_quote
from smart_money.relay_api import RelayApiError, RelayNotReady, RelayPublicClient

A = "0x" + "11" * 20
B = "0x" + "22" * 20
H = "0x" + "ab" * 32
ORDER = "0x" + "cd" * 32


class RelayOrderLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_hint_and_order_id_use_distinct_fields(self):
        client = RelayPublicClient()
        doc = {"requests": [{"id": H, "protocol": {"orderId": ORDER}}]}
        with patch.object(client, "_fetch", return_value=doc) as fetch:
            self.assertEqual(await client.lookup_by_order(ORDER), doc)
            fetch.assert_called_with(ORDER, 2, field="orderId")
            self.assertEqual(await client.lookup_by_order(ORDER, H), doc)
            fetch.assert_called_with(H, 2, field="id")

    async def test_identity_ambiguity_and_errors_do_not_fall_back_to_wrong_order(self):
        client = RelayPublicClient()
        for requests in ([{"id": H, "protocol": {"orderId": H}}],
                         [{"id": ORDER, "protocol": {"orderId": ORDER}}],
                         [{}, {}], [None]):
            with self.subTest(requests=requests), patch.object(
                    client, "_fetch", return_value={"requests": requests}):
                with self.assertRaises(RelayApiError):
                    await client.lookup_by_order(ORDER, H)
        with patch.object(client, "_fetch", side_effect=RelayNotReady("not yet")) as fetch:
            with self.assertRaises(RelayNotReady):
                await client.lookup_by_order(ORDER)
            self.assertEqual(fetch.call_count, 1)
        with patch.object(client, "_fetch") as fetch:
            with self.assertRaises(ValueError):
                await client.lookup_by_order("0x1234")
            fetch.assert_not_called()


class QuoteReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        self.clock = patch("smart_money.quotes.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.signal = Signal(H, A, "third_party", "BUY", "incoming", R.RELAY_PROXY,
                             "0x0a2b8f36", stage="relay_buy_evidenced",
                             execution_status="success", protocol="kyber", exact_in=True,
                             token_in=R.USDG, token_out=B)
        test = self

        class Rpc:
            async def call(self, method, params=None):
                if method == "eth_gasPrice":
                    return "0x1"
                return {"number": "0xa", "hash": H}

        class Aggregator:
            def __init__(self):
                self.amounts = []
                self.builds = []
                self.concurrent = 0
                self.peak = 0

            async def route(self, token_in, token_out, amount):
                self.amounts.append(amount)
                self.concurrent += 1
                self.peak = max(self.peak, self.concurrent)
                await asyncio.sleep(0)
                self.concurrent -= 1
                return KyberRoute(token_in, token_out, amount, str(int(amount) * 2),
                                  350000, R.KYBER_META_AGGREGATION_ROUTER_V2,
                                  {"amountIn": amount}, test.now, H)

            async def build(self, route, follower, slip, deadline):
                self.builds.append((route, follower, slip, deadline))
                return SimpleNamespace(route_response_hash=route.response_hash)

        self.client = Aggregator()
        self.quoter = LiveQuoter(Rpc(), {"kyber": self.client})

    async def test_two_concurrent_routes_one_build_and_three_reuses(self):
        with self.quoter.execution_context(H, A, "policy", 2) as context:
            original = await self.quoter.quote_with_reference(self.signal, "100000")
            for _ in range(3):
                self.assertEqual(await self.quoter.quote_with_reference(
                    deepcopy(self.signal), "100000"), original)
            await self.quoter.build_aggregator_transaction(self.signal, "100000", A, 300, 220)
            self.assertEqual(context["route_requests"], 2)
            self.assertEqual(context["build_requests"], 1)
            self.assertEqual(context["quote_reuses"], 3)
            self.assertEqual(self.client.peak, 2)
            self.assertEqual(self.client.builds[0][0].amount_in_raw, "100000")

    async def test_expiry_and_changed_amount_refresh_without_resetting_timestamps(self):
        with self.quoter.execution_context(H, A, "policy", 2) as context:
            old = await self.quoter.quote_with_reference(self.signal, "100000")
            self.now = 103
            new = await self.quoter.quote_with_reference(self.signal, "100000")
            self.assertEqual(old[0].observed_at, 100)
            self.assertEqual(new[0].observed_at, 103)
            await self.quoter.quote_with_reference(self.signal, "200000")
            self.assertEqual(context["route_requests"], 6)
            self.assertEqual(context["refreshes"], 2)

    async def test_follower_mismatch_and_build_expiry_fail_closed(self):
        with self.quoter.execution_context(H, A, "policy", 2):
            await self.quoter.quote_with_reference(self.signal, "100000")
            with self.assertRaisesRegex(ValueError, "follower mismatch"):
                await self.quoter.build_aggregator_transaction(self.signal, "100000", B, 300, 220)
            self.now = 103
            with self.assertRaisesRegex(ValueError, "expired"):
                await self.quoter.build_aggregator_transaction(self.signal, "100000", A, 300, 220)
            self.assertFalse(self.client.builds)

    async def test_contexts_are_not_shared_between_relationship_tasks(self):
        async def operation(wallet):
            with self.quoter.execution_context(H, wallet, "policy", 2) as context:
                await self.quoter.quote_with_reference(self.signal, "100000")
                await asyncio.sleep(0)
                await self.quoter.build_aggregator_transaction(
                    self.signal, "100000", wallet, 300, 220)
                return context["route_requests"]
        self.assertEqual(await asyncio.gather(operation(A), operation(B)), [2, 2])
        self.assertIsNone(self.quoter._context.get())

    async def test_outside_context_keeps_fresh_lookup_behavior(self):
        await self.quoter.quote_with_reference(self.signal, "100000")
        await self.quoter.quote_with_reference(self.signal, "100000")
        self.assertEqual(len(self.client.amounts), 4)

    async def test_build_wait_does_not_renew_route_age(self):
        async def slow_build(*args):
            self.now = 103
            return object()
        with self.quoter.execution_context(H, A, "policy", 2):
            await self.quoter.quote_with_reference(self.signal, "100000")
            with patch.object(self.client, "build", side_effect=slow_build):
                with self.assertRaisesRegex(ValueError, "expired during build"):
                    await self.quoter.build_aggregator_transaction(
                        self.signal, "100000", A, 300, 220)

    async def test_explicit_aggregator_overrides_source_local_route(self):
        source = replace(self.signal, protocol="v3", evidence={"local_execution_route":
                         aggregator_route_definition(R.USDG, B, "kyber", R.CHAIN_ID)})
        local = {"protocol": "v3", "assets": [R.USDG, B], "fees": [500]}
        self.assertEqual(execution_quote_signal(source, (local,)).protocol, "kyber")

    async def test_kyber_only_does_not_call_local_discovery(self):
        class Store:
            def put(self, signal):
                pass
        with patch.object(self.quoter, "discover_v3_route", side_effect=AssertionError("local")):
            engine = PaperEngine(Store(), self.quoter, QuotePolicy(), "test", "evidenced",
                                 execution_providers=("kyber",))
            selected = await engine._select_buy_execution_signal(
                replace(self.signal, protocol="relay_solver"), "100000")
            self.assertEqual(selected.protocol, "kyber")

    async def test_expired_reference_rejected_even_when_full_quote_is_fresh(self):
        quote = Quote("kyber", R.KYBER_META_AGGREGATION_ROUTER_V2, 10, H, 100,
                      R.USDG, B, "100000", "200000", "350000")
        reference = replace(quote, observed_at=97, amount_in_raw="1000", amount_out_raw="2000")
        self.assertEqual(assess_market_quote(quote, reference, QuotePolicy(), "1", 100)[1],
                         "reference_quote_missing_or_expired")
