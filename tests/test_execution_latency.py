"""Offline latency-path regressions. Synthetic keys/RPC only; no public network."""
import asyncio
import ast
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
import json
import inspect
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

from smart_money import registry as R
from smart_money.broadcast import MainnetBroadcaster
from smart_money.execution_pipeline import ExecutionPreparer, OfflineExecutionSigner, ReadOnlyPreBroadcastReviewer
from smart_money.models import Signal
from smart_money.quotes import LiveQuoter, QuotePolicy
from smart_money.store import Store
from smart_money.zeroex import (ZeroExAggregatorClient, ZeroExApiError, decode_zeroex_swap,
                               EXEC_TYPES, SETTLER_TYPES, SETTLER_SELECTOR, SETTLER_REGISTRY,
                               verify_settler)

SMART = "0x" + "22"*20
TOKEN = "0x" + "33"*20
SETTLER = "0x" + "44"*20
H = "0x" + "ab"*32
# Public deterministic test key with no production key database access.
ACCOUNT = Account.from_key(bytes.fromhex("11"*32))
FOLLOWER = ACCOUNT.address.lower()


def calldata(amount=10000, minimum=19400, recipient=FOLLOWER, target=SETTLER,
             sell_token=R.USDG, buy_token=TOKEN):
    inner = SETTLER_SELECTOR + encode(SETTLER_TYPES, [(recipient, buy_token, minimum), [b"\x01\x02\x03\x04"], bytes(32)])
    return "0x2213bc0b" + encode(EXEC_TYPES, [target, sell_token, amount, target, inner]).hex()


def response(endpoint, query):
    amount = int(query["sellAmount"])
    result = dict(liquidityAvailable=True, sellToken=R.USDG, buyToken=TOKEN,
                  sellAmount=str(amount), buyAmount=str(amount*2), gas="200000")
    if endpoint == "quote":
        minimum = amount*2*(10000-query["slippageBps"])//10000
        result.update(minBuyAmount=str(minimum), issues=dict(allowance=None, balance=None,
                      simulationIncomplete=False, invalidSourcesPassed=[]),
                      transaction=dict(to=R.ZERO_X_ALLOWANCE_HOLDER, value="0", gas="200000",
                                       data=calldata(amount, minimum, query["recipient"])))
    return result


class AtomicQuoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_mixed_local_zeroex_policy_binds_operation_context(self):
        # Execute the actual nested monitor callback without starting monitor,
        # opening a database or connecting to public RPC/aggregator endpoints.
        from smart_money import cli
        from smart_money.paper import AGGREGATOR_PROVIDERS
        module = ast.parse(inspect.getsource(cli.monitor))
        callback = next(node for node in ast.walk(module)
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == "observe_policy")
        isolated = ast.fix_missing_locations(ast.Module(body=[callback], type_ignores=[]))
        client = ZeroExAggregatorClient("synthetic-api-key")
        self.addCleanup(client.close)
        rpc = SimpleNamespace(call=AsyncMock(side_effect=lambda method, params=None:
            {"eth_getBlockByNumber": {"number": "0xa", "hash": H}, "eth_gasPrice": "0x1"}[method]))
        quoter = LiveQuoter(rpc, {"zeroex": client})
        signal = Signal(H, SMART, "direct", "BUY", "call", R.ZERO_X_ALLOWANCE_HOLDER, "0x",
                        protocol="zeroex", token_in=R.USDG, token_out=TOKEN)
        async def paper_observe(selected, policies):
            if "zeroex" not in policies[0].execution_providers:
                self.assertIsNone(quoter._context.get())
                return None
            context = quoter._context.get()
            self.assertIsNotNone(context)
            self.assertEqual(context["binding"], (signal.event_id, FOLLOWER, "snapshot"))
            return await quoter.quote_with_reference(selected, "10000")
        namespace = dict(early_trial_id=None, AGGREGATOR_PROVIDERS=AGGREGATOR_PROVIDERS,
                         quoter=quoter, paper_observe=paper_observe, report=Mock())
        exec(compile(isolated, "<monitor observe_policy>", "exec"), namespace)
        with patch.object(client, "_request", side_effect=response):
            for providers in (("local", "zeroex", "kyber"), ("local", "zeroex"),
                              ("zeroex", "kyber"), ("local",)):
                policy = SimpleNamespace(execution_providers=providers, run_mode="mainnet_live",
                    follower_wallet=FOLLOWER, snapshot_hash="snapshot", relationship_id="1",
                    quote_policy=QuotePolicy(max_age_seconds=6))
                result = await namespace["observe_policy"](signal, policy)
                if "zeroex" in providers:
                    self.assertEqual(result[0].protocol, "zeroex")
                self.assertIsNone(quoter._context.get())

    async def test_sell_token_to_usdg_preserves_input_amount_and_recipient(self):
        client = ZeroExAggregatorClient("synthetic-api-key")
        self.addCleanup(client.close)
        document = response("quote", dict(sellAmount="10000", slippageBps=300, recipient=FOLLOWER))
        document.update(sellToken=TOKEN, buyToken=R.USDG)
        document["transaction"]["data"] = calldata(sell_token=TOKEN, buy_token=R.USDG)
        with patch.object(client, "_request", return_value=document):
            swap = await client.quote(TOKEN, R.USDG, "10000", FOLLOWER, 300, int(time.time())+120)
        self.assertEqual(swap.input_asset, TOKEN)
        self.assertEqual(swap.output_asset, R.USDG)
        self.assertEqual(swap.amount_in_raw, "10000")
        self.assertEqual(swap.minimum_amount_out_raw, "19400")

    async def test_full_quote_reference_price_no_build_and_no_cross_operation_reuse(self):
        client = ZeroExAggregatorClient("synthetic-api-key")
        self.addCleanup(client.close)
        rpc = SimpleNamespace(call=AsyncMock(side_effect=lambda method, params=None:
            {"eth_getBlockByNumber": {"number": "0xa", "hash": H}, "eth_gasPrice": "0x1"}[method]))
        quoter = LiveQuoter(rpc, {"zeroex": client})
        signal = Signal(H, SMART, "direct", "BUY", "call", R.ZERO_X_ALLOWANCE_HOLDER, "0x",
                        protocol="zeroex", token_in=R.USDG, token_out=TOKEN)
        with patch.object(client, "_request", side_effect=response) as request:
            with quoter.execution_context("op", FOLLOWER, "snapshot", 6, 300) as context:
                first = await quoter.quote_with_reference(signal, "10000")
                second = await quoter.quote_with_reference(signal, "10000")
                swap = await quoter.build_aggregator_transaction(signal, "10000", FOLLOWER, 300, int(time.time())+120)
                self.assertEqual(first, second)
                self.assertEqual(swap.minimum_amount_out_raw, "19400")
                self.assertEqual(Counter(c.args[0] for c in request.call_args_list), {"quote": 1, "price": 1})
                self.assertEqual(context["build_requests"], 0)
                with self.assertRaises(ValueError):
                    await quoter.build_aggregator_transaction(signal, "10000", SMART, 300, int(time.time())+120)
                with self.assertRaises(ValueError):
                    await quoter.build_aggregator_transaction(signal, "10000", FOLLOWER, 400, int(time.time())+120)
            with quoter.execution_context("op2", FOLLOWER, "snapshot", 6):
                await quoter.quote_with_reference(signal, "10000")
                self.assertEqual(request.call_count, 4)

    async def test_quote_identity_calldata_and_issues_fail_closed(self):
        client = ZeroExAggregatorClient("synthetic-api-key")
        self.addCleanup(client.close)
        original = response("quote", dict(sellAmount="10000", slippageBps=300, recipient=FOLLOWER))
        changes = [lambda d: d.update(sellAmount="10001"),
                   lambda d: d["transaction"].update(to=SETTLER),
                   lambda d: d["transaction"].update(value="1"),
                   lambda d: d["transaction"].update(data=calldata(recipient=SMART)),
                   lambda d: d["transaction"].update(data=calldata(minimum=1)),
                   lambda d: d["issues"].update(simulationIncomplete=True),
                   lambda d: d["issues"].update(allowance={"spender": SETTLER}),
                   lambda d: d.update(allowanceTarget=SETTLER)]
        for change in changes:
            document = deepcopy(original)
            change(document)
            with patch.object(client, "_request", return_value=document):
                with self.assertRaises(ValueError):
                    await client.quote(R.USDG, TOKEN, "10000", FOLLOWER, 300, int(time.time())+120)

    async def test_registry_current_previous_unknown_paused_and_failure(self):
        for current, previous, accepted in [(SETTLER, SMART, True), (SMART, SETTLER, True),
                                             (SMART, TOKEN, False), (R.NATIVE, SETTLER, False)]:
            rpc = SimpleNamespace(call=AsyncMock(side_effect=[
                "0x"+encode(["address"], [current]).hex(), "0x"+encode(["address"], [previous]).hex()]))
            if accepted:
                self.assertEqual((await verify_settler(rpc, calldata()))["settler"], SETTLER)
            else:
                with self.assertRaises(ValueError): await verify_settler(rpc, calldata())
        rpc = SimpleNamespace(call=AsyncMock(side_effect=ValueError("registry unavailable")))
        with self.assertRaises(ValueError): await verify_settler(rpc, calldata())

    def test_reject_unknown_selector_offsets_and_recipient(self):
        for data in ["0xdeadbeef", calldata(recipient=R.NATIVE), calldata()+"aa"*65,
                     "0x2213bc0b"+"00"*256]:
            with self.assertRaises(ZeroExApiError): decode_zeroex_swap(data)


class SinglePreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.start_paper_budget_cycle("cycle", "test")
        self.store.configure_paper_budget(SMART, "USDG", "1000000")
        self.signal = Signal(H, SMART, "direct", "BUY", "call", R.ZERO_X_ALLOWANCE_HOLDER, "0x",
            protocol="zeroex", token_in=R.USDG, token_out=TOKEN, exact_in=True,
            stage="swap_evidenced", execution_status="success", evidence={
                "actual_input_debit_raw": "10000", "actual_output_credit_raw": "20000"})
        self.store.put(self.signal)
        self.store.reserve_paper_proposal(dict(proposal_id="p", source_event_id=self.signal.event_id,
            source_tx_hash=H, wallet=SMART, trigger_mode="evidenced", strategy_version="test",
            input_asset=R.USDG, output_asset=TOKEN, budget_bucket="USDG", amount_in_raw="10000",
            attribution=dict(smart_wallet=SMART, follower_wallet=FOLLOWER, relationship_id="1", config_snapshot_hash="s")))
        self.calls = []
        self.active = self.peak = 0
        async def call(method, params=None):
            self.calls.append((method, params))
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            if method == "eth_call":
                if params[0]["to"] == SETTLER_REGISTRY:
                    return "0x"+encode(["address"], [SETTLER]).hex()
                if params[0]["to"] == R.ZERO_X_ALLOWANCE_HOLDER:
                    return "0x"+encode(["bytes"], [encode(["bool"], [True])]).hex()
                return hex(10**18)
            return dict(eth_getBlockByNumber={"number": "0xa", "hash": H}, eth_gasPrice="0x1",
                        eth_getTransactionCount="0x7", eth_getBalance=hex(10**18))[method]
        self.rpc = SimpleNamespace(call=call)
        self.client = ZeroExAggregatorClient("synthetic-api-key")
        self.addCleanup(self.client.close)
        self.quoter = LiveQuoter(self.rpc, {"zeroex": self.client})
        self.policy = QuotePolicy(max_age_seconds=6)
        self.preparer = ExecutionPreparer(self.store, self.quoter, self.rpc, self.policy,
                        {"0x"}, {R.USDG}, (), "s", single_preflight=True)
        signer_factory = lambda wallet: SimpleNamespace(sign_transaction=lambda tx:
            Account.sign_transaction(tx, ACCOUNT.key).raw_transaction)
        self.signer = OfflineExecutionSigner(self.store, self.quoter, self.rpc, self.policy, "s",
                                            signer_factory=signer_factory, relationship_gate=Mock())
        self.reviewer = ReadOnlyPreBroadcastReviewer(self.store, self.quoter, self.rpc, self.policy, Mock())
        for target in [patch.object(self.client, "_request", side_effect=response),
                       patch("smart_money.broadcast._stop_controls"),
                       patch.object(self.signer, "_authorize"), patch.object(self.reviewer, "_authorize")]:
            target.start()
            self.addCleanup(target.stop)

    async def prepared(self):
        with self.quoter.execution_context("op", FOLLOWER, "s", 6):
            return await self.preparer.prepare(self.signal, "p")

    async def test_one_parallel_round_then_sign_review_and_mock_send(self):
        prepared = await self.prepared()
        self.assertGreaterEqual(self.peak, 5)
        counts = Counter(m for m, _ in self.calls)
        self.assertEqual(counts["eth_getTransactionCount"], 1)
        self.assertEqual(counts["eth_getBalance"], 1)
        self.assertEqual(counts["eth_call"], 5)  # balance+allowance+registry x2+simulation
        count = len(self.calls)
        signed = await self.signer.sign(self.signal, "p", ticket=prepared.ticket)
        review = await self.reviewer.review(self.signal, "p", signed.raw_transaction, ticket=signed.ticket)
        self.assertEqual(len(self.calls), count)
        broadcaster = MainnetBroadcaster("https://example.invalid")
        self.addCleanup(broadcaster.close)
        def request(*args, **kwargs):
            kwargs["before_send"]()
            return {"jsonrpc": "2.0", "id": 1, "result": signed.signed_tx_hash}
        with patch("smart_money.broadcast.require_mainnet_broadcast_enabled"), patch.object(
                broadcaster.transport, "request", side_effect=request) as send:
            await broadcaster.broadcast(review, signed.raw_transaction,
                follower_wallet=FOLLOWER, relationship_id="1", config_snapshot_hash="s")
            with self.assertRaises(ValueError):
                await broadcaster.broadcast(review, signed.raw_transaction,
                    follower_wallet=FOLLOWER, relationship_id="1", config_snapshot_hash="s")
        persisted = json.dumps(self.store.execution_plan("p"))
        self.assertNotIn(signed.raw_transaction.hex(), persisted)
        self.assertNotIn(ACCOUNT.key.hex(), persisted)

    async def test_expired_before_sign_renews_once_and_expired_after_sign_rejects(self):
        prepared = await self.prepared()
        stale = replace(prepared.ticket, started_monotonic=time.monotonic()-3)
        before = len(self.calls)
        signed = await self.signer.sign(self.signal, "p", ticket=stale)
        self.assertEqual(len(self.calls)-before, 8)
        before = len(self.calls)
        with self.assertRaisesRegex(ValueError, "expired"):
            await self.reviewer.review(self.signal, "p", signed.raw_transaction,
                ticket=replace(signed.ticket, started_monotonic=time.monotonic()-3))
        self.assertEqual(len(self.calls), before)

    async def test_ticket_binding_and_recovery_do_not_trust_persisted_evidence(self):
        prepared = await self.prepared()
        for ticket in [replace(prepared.ticket, snapshot="other"),
                       replace(prepared.ticket, deadline=prepared.ticket.deadline+1),
                       replace(prepared.ticket, transaction_hash="bad"),
                       replace(prepared.ticket, pid=-1), replace(prepared.ticket, proposal_id="other")]:
            with self.assertRaises(ValueError): await self.signer.sign(self.signal, "p", ticket=ticket)
        existing = await self.preparer.prepare(self.signal, "p")
        self.assertTrue(existing.existing)
        self.assertIsNone(existing.ticket)

    async def test_send_delay_checks_ticket_before_network_and_keeps_signature(self):
        prepared = await self.prepared()
        signed = await self.signer.sign(self.signal, "p", ticket=prepared.ticket)
        review = await self.reviewer.review(self.signal, "p", signed.raw_transaction, ticket=signed.ticket)
        stale = replace(review, ticket=replace(review.ticket, started_monotonic=time.monotonic()-3))
        broadcaster = MainnetBroadcaster("https://example.invalid")
        self.addCleanup(broadcaster.close)
        actual_send = Mock()
        def pool_request(*args, **kwargs):
            kwargs["before_send"]()
            actual_send()
        with patch("smart_money.broadcast.require_mainnet_broadcast_enabled"), patch.object(
                broadcaster.transport, "request", side_effect=pool_request):
            with self.assertRaises(ValueError):
                await broadcaster.broadcast(stale, signed.raw_transaction, follower_wallet=FOLLOWER,
                                            relationship_id="1", config_snapshot_hash="s")
        actual_send.assert_not_called()
        self.assertEqual(self.store.execution_plan("p")["status"], "signed")

    async def test_stop_after_connection_before_send_does_not_consume_ticket(self):
        prepared = await self.prepared()
        signed = await self.signer.sign(self.signal, "p", ticket=prepared.ticket)
        review = await self.reviewer.review(self.signal, "p", signed.raw_transaction, ticket=signed.ticket)
        broadcaster = MainnetBroadcaster("https://example.invalid")
        self.addCleanup(broadcaster.close)
        actual_send = Mock()
        def request(*args, **kwargs):
            with patch("smart_money.broadcast._stop_controls", side_effect=PermissionError("stopped")):
                kwargs["before_send"]()
            actual_send()
        with patch("smart_money.broadcast.require_mainnet_broadcast_enabled"), patch.object(
                broadcaster.transport, "request", side_effect=request):
            with self.assertRaisesRegex(RuntimeError, "PermissionError"):
                await broadcaster.broadcast(review, signed.raw_transaction, follower_wallet=FOLLOWER,
                                            relationship_id="1", config_snapshot_hash="s")
        actual_send.assert_not_called()
        self.assertFalse(signed.ticket._used)
