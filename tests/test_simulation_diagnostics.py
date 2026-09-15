"""Offline failure evidence: mocked RPC only, no keys or live state changes."""
import asyncio
from contextlib import ExitStack, redirect_stderr
from dataclasses import replace
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from eth_abi import encode

from smart_money import cli, registry as R
from smart_money.execution_prep import UnsignedExecutionPlan, simulate_aggregator_execution
from smart_money.rpc import ALLOWED_METHODS, ReadOnlyRpc, RpcError
from smart_money.simulation_diagnostics import (
    AggregatorSimulationError, MAX_CALLDATA_BYTES, MAX_REVERT_BYTES,
    rpc_error_diagnostic,
)


def error_string(reason):
    return "0x08c379a0" + encode(["string"], [reason]).hex()


def plan():
    return UnsignedExecutionPlan(
        follower_wallet="0x" + "11" * 20, relationship_id="1", proposal_id="synthetic",
        to=R.KYBER_META_AGGREGATION_ROUTER_V2, data="0xe21fd0e9" + "00" * 64,
        value_raw="0", input_asset=R.USDG, amount_in_raw="100000",
        minimum_amount_out_raw="194", gas_limit=600000, max_fee_per_gas="200",
        max_priority_fee_per_gas="0", quote_observed_at=100.0,
        quote_block_number=10, quote_block_hash="0x" + "ab" * 32,
        deadline=220, execution_provider="kyber")


class RpcDiagnosticTests(unittest.TestCase):
    def test_revert_reason_is_preserved_without_provider_message(self):
        details = rpc_error_diagnostic("eth_call", {
            "code": 3, "message": "execution reverted: https://provider.invalid/secret-token",
            "data": error_string("Return amount is not enough"),
            "private_key": "never-log-this-field"})
        self.assertEqual(details["revert_reason"], "Return amount is not enough")
        self.assertEqual(details["message_category"], "execution_reverted")
        self.assertFalse(details["revert_reason_redacted"])
        self.assertNotIn("secret", json.dumps(details))
        self.assertNotIn("never-log", json.dumps(details))

    def test_rpc_exception_keeps_legacy_safe_text_and_structured_details(self):
        rpc = ReadOnlyRpc("https://provider.invalid/secret-token")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"id": 1, "error": {
            "code": 3, "message": rpc.url,
            "data": error_string("ERC20: insufficient allowance")}}).encode()
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(RpcError) as caught:
                rpc._request("eth_call", [], 1)
        self.assertEqual(str(caught.exception), "RPC eth_call error code 3")
        self.assertEqual(caught.exception.diagnostic["revert_reason"], "ERC20: insufficient allowance")
        self.assertNotIn("secret", json.dumps(caught.exception.diagnostic))

    def test_secrets_in_abi_strings_are_redacted(self):
        endpoint = "https://user:shortpw@provider.invalid/v2/secret-token?apiKey=smallkey"
        reasons = [endpoint, "password=hunter2", "Bearer credential-value", "api_key=abc",
                   "0x" + "12" * 32, "secret-token", "smallkey", "shortpw",
                   "provider.invalid said no", "embedded\nlog line", "x" * 513]
        for reason in reasons:
            with self.subTest(reason=reason):
                details = rpc_error_diagnostic("eth_call", {"code": 3, "data": error_string(reason)}, endpoint)
                self.assertTrue(details["revert_reason_redacted"])
                self.assertNotIn(reason, json.dumps(details))
                self.assertNotIn("revert_data", details)

    def test_panic_code_is_decimal_and_custom_data_is_not_dumped(self):
        details = rpc_error_diagnostic("eth_call", {"code": 3, "data":
            "0x4e487b71" + encode(["uint256"], [17]).hex()})
        self.assertEqual(details["panic_code_raw"], "17")
        custom = "0xdeadbeef" + b"provider-secret-echo".hex()
        details = rpc_error_diagnostic("eth_call", {"code": 3, "data": custom})
        self.assertEqual(details["revert_selector"], "0xdeadbeef")
        self.assertEqual(details["revert_kind"], "custom_or_unknown")
        self.assertNotIn(custom, json.dumps(details))

    def test_missing_empty_invalid_large_and_nested_data(self):
        for data, status in [(None, "missing_or_unsupported"), ({"error": "secret"}, "missing_or_unsupported"),
                             ("0xz0", "invalid_hex"), ("0x1", "invalid_hex"),
                             ("0x" + "00" * (MAX_REVERT_BYTES + 1), "size_limit")]:
            self.assertEqual(rpc_error_diagnostic("eth_call", {"code": 3, "data": data})[
                "revert_data_status"], status)
        self.assertEqual(rpc_error_diagnostic("eth_call", {"code": 3, "data": "0x"})[
            "revert_kind"], "empty_or_short")
        details = rpc_error_diagnostic("eth_call", {"code": 3, "data": {
            "data": error_string("STF"), "secret": "never-log"}})
        self.assertEqual(details["revert_reason"], "STF")
        self.assertNotIn("never-log", json.dumps(details))

    def test_malformed_abi_offsets_lengths_and_utf8_do_not_mask_rpc_error(self):
        for data in ["0x08c379a0", "0x08c379a0" + "ff" * 64,
                     "0x08c379a0" + encode(["uint256", "uint256"], [32, 9999]).hex(),
                     "0x08c379a0" + encode(["uint256", "uint256"], [32, 1]).hex() + "ff"]:
            self.assertEqual(rpc_error_diagnostic("eth_call", {"code": 3, "data": data})[
                "revert_decode_status"], "malformed")

    def test_untrusted_error_codes_and_non_call_errors_are_bounded(self):
        for error in [None, "url-secret", {"code": "url-secret"}, {"code": True}, {"code": 2 ** 90}]:
            self.assertIsNone(rpc_error_diagnostic("eth_call", error)["code"])
        result = rpc_error_diagnostic("eth_getLogs", {"code": -32000,
            "message": "secret", "data": error_string("provider-secret")})
        self.assertEqual(result, {"kind": "rpc_error", "code": -32000})

    def test_message_only_gas_error_gets_a_fixed_category(self):
        result = rpc_error_diagnostic("eth_call", {"code": 3, "message":
            "out of gas (https://provider.invalid/secret)", "data": "0x"})
        self.assertEqual(result["message_category"], "out_of_gas")
        self.assertNotIn("secret", json.dumps(result))

    def test_transport_error_still_hides_endpoint_and_response(self):
        rpc = ReadOnlyRpc("https://provider.invalid/secret-token")
        with patch("urllib.request.urlopen", side_effect=OSError(rpc.url)):
            with self.assertRaises(RpcError) as caught:
                rpc._request("eth_call", [], 1)
        self.assertEqual(str(caught.exception), "RPC transport failure: OSError")
        self.assertEqual(caught.exception.diagnostic["exception_type"], "OSError")
        self.assertNotIn("secret", json.dumps(caught.exception.diagnostic))

    def test_rpc_allowlist_still_forbids_mutations_and_other_tracers(self):
        self.assertNotIn("eth_sendRawTransaction", ALLOWED_METHODS)
        rpc = ReadOnlyRpc("https://provider.invalid")
        with patch("urllib.request.urlopen") as request:
            with self.assertRaises(PermissionError):
                asyncio.run(rpc.call("eth_sendRawTransaction", ["0x00"]))
            with self.assertRaises(PermissionError):
                asyncio.run(rpc.call("debug_traceTransaction", ["0x" + "ab" * 32, {"tracer": "callTracer"}]))
            request.assert_not_called()


class SimulationDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_retains_exact_unsigned_call_and_separate_block_context(self):
        p = plan()
        details = rpc_error_diagnostic("eth_call", {"code": 3, "data": error_string("STF")})
        rpc = SimpleNamespace(call=AsyncMock(side_effect=RpcError("safe", diagnostic=details)))
        with self.assertRaises(AggregatorSimulationError) as caught:
            await simulate_aggregator_execution(rpc, p)
        record = caught.exception.diagnostic
        rpc.call.assert_awaited_once()
        self.assertEqual(record["call"], rpc.call.await_args.args[1][0])
        self.assertEqual(record["block_parameter"], "pending")
        self.assertEqual(record["quote_block_hash"], p.quote_block_hash)
        self.assertFalse(record["pending_state_pinned"])
        self.assertFalse(record["quote_block_is_simulation_block"])
        self.assertEqual(record["amount_in_raw"], "100000")
        self.assertEqual(record["rpc_error"]["revert_reason"], "STF")
        self.assertGreaterEqual(record["completed_at"], record["started_at"])
        self.assertFalse(record["broadcast_performed_by_simulation"])
        self.assertNotIn("signature", json.dumps(record))
        self.assertNotIn("nonce", record["call"])

    async def test_success_still_uses_exactly_one_call_and_same_result(self):
        rpc = SimpleNamespace(call=AsyncMock(return_value="0x" + encode(
            ["uint256", "uint256"], [200, 300000]).hex()))
        with patch("smart_money.execution_prep.simulation_failure") as diagnostic:
            result = await simulate_aggregator_execution(rpc, plan())
            diagnostic.assert_not_called()
        rpc.call.assert_awaited_once()
        self.assertEqual(result, {"simulated": True, "simulated_return_amount_raw": "200",
            "simulated_gas_used": "300000", "simulation_block": "pending"})

    async def test_all_result_rejections_get_diagnostics_without_retry(self):
        for raw, kind in [(None, "missing_output"), ("0x", "missing_output"),
                ("0x" + "zz" * 64, "undecodable_output"),
                ("0x" + encode(["uint256", "uint256"], [193, 123]).hex(), "below_minimum")]:
            with self.subTest(kind=kind):
                rpc = SimpleNamespace(call=AsyncMock(return_value=raw))
                with self.assertRaises(AggregatorSimulationError) as caught:
                    await simulate_aggregator_execution(rpc, plan())
                d = caught.exception.diagnostic
                self.assertEqual(d["failure_kind"], kind)
                if kind == "below_minimum":
                    self.assertEqual(d["result"]["return_amount_raw"], "193")
                rpc.call.assert_awaited_once()

    async def test_large_calldata_has_hash_and_explicit_omission_not_partial_replay(self):
        p = replace(plan(), data="0xe21fd0e9" + "00" * MAX_CALLDATA_BYTES)
        rpc = SimpleNamespace(call=AsyncMock(side_effect=RpcError("failed")))
        with self.assertRaises(AggregatorSimulationError) as caught:
            await simulate_aggregator_execution(rpc, p)
        d = caught.exception.diagnostic
        self.assertTrue(d["calldata_omitted_size_limit"])
        self.assertIsNone(d["call"]["data"])
        self.assertEqual(d["calldata_sha256"], hashlib.sha256(bytes.fromhex(p.data[2:])).hexdigest())
        self.assertLess(len(json.dumps(d)), 4000)

    async def test_plain_rpc_failure_text_does_not_leak_and_jsonl_is_parseable(self):
        rpc = SimpleNamespace(call=AsyncMock(side_effect=RpcError("https://provider.invalid/secret")))
        with self.assertRaises(AggregatorSimulationError) as caught:
            await simulate_aggregator_execution(rpc, plan())
        error = caught.exception
        self.assertNotIn("secret", str(error))
        stream = io.StringIO()
        with redirect_stderr(stream):
            cli.report("live_execution_abandoned", stage="prepare", simulation_failure=error.diagnostic)
        row = json.loads(stream.getvalue())
        self.assertEqual(row["simulation_failure"]["call"]["data"], plan().data)
        self.assertNotIn("secret", stream.getvalue())


class MonitorDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_sign_review_failures_log_context_and_keep_cleanup(self):
        """Exercise the actual monitor catch path with inert pipeline mocks only."""
        from smart_money.paper import AmountRule
        from smart_money.paper_config import WalletPaperPolicy
        from smart_money.quotes import QuotePolicy
        from smart_money.store import Store
        from test_early_shadow import fixture

        for stage in ("prepare", "sign", "review"):
            with self.subTest(stage=stage):
                tx, wallet = fixture()
                p = plan()
                policy = WalletPaperPolicy(wallet, "synthetic", p.follower_wallet, "1", "mainnet_live",
                    {"USDG": "1000"}, {"USDG": AmountRule("fixed", fixed_amount_raw="100")},
                    AmountRule("proportional", ratio_ppm=1000000), "test", "evidenced", (), QuotePolicy(),
                    frozenset({"relay_solver", "kyber"}), frozenset({R.USDG}), frozenset(), (), "a"*64, ("kyber",))
                config = SimpleNamespace(relationships=(policy,), wallets={wallet: policy},
                                         policies_for=lambda w: (policy,))
                db = Store(":memory:")
                db.start_early_trial("trial", p.follower_wallet, [1])
                db.start_paper_budget_cycle("test", "synthetic")
                db.configure_paper_budget(policy.ledger_scope, "USDG", "1000")
                completed = asyncio.Event()
                signal = SimpleNamespace(protocol="kyber", token_in=R.USDG, event_id="original-event",
                                         tx_hash=tx.hash)
                proposal = {"amount_in_raw": "100", "attribution": {
                    "early_trial_id": "trial", "source_event_id": "attributed-original-event"}}
                async def rpc_call(method, params=None):
                    if method == "eth_chainId":
                        return hex(R.CHAIN_ID)
                    if method == "eth_call":
                        return "0x" + encode(["uint256"], [1000000]).hex()
                    raise AssertionError("unexpected mocked RPC method")

                rpc = SimpleNamespace(call=AsyncMock(side_effect=rpc_call),
                                      receipt=AsyncMock(return_value=None))
                preparer = SimpleNamespace(prepare=AsyncMock(return_value=SimpleNamespace(plan_id="plan", preflight={})))
                signer = SimpleNamespace(sign=AsyncMock(return_value=SimpleNamespace(
                    raw_transaction=b"synthetic-signed-bytes-never-log")))
                reviewer = SimpleNamespace(review=AsyncMock())
                broadcaster = SimpleNamespace(broadcast=AsyncMock())

                async def fail(*args, **kwargs):
                    failing_rpc = SimpleNamespace(call=AsyncMock(side_effect=RpcError("failed",
                        diagnostic=rpc_error_diagnostic("eth_call", {"code": 3, "data": error_string("STF")}))))
                    return await simulate_aggregator_execution(failing_rpc, p)

                getattr({"prepare": preparer, "sign": signer, "review": reviewer}[stage], stage).side_effect = fail
                emitted = []

                def runtime_factory(*args, **kwargs):
                    execute = args[5]

                    async def handoff(*unused):
                        existing = None if stage == "prepare" else {"status": "prepared" if stage == "sign" else "signed"}
                        with patch.object(db, "paper_proposal", return_value=proposal), \
                             patch.object(db, "check_early_trial_proposal"), \
                             patch.object(db, "execution_plan", return_value=existing), \
                             patch.object(db, "cancel_prepared_execution_plan", return_value=True) as cancel_prepared, \
                             patch.object(db, "cancel_unbroadcast_signed_execution_plan", return_value=True) as cancel_signed, \
                             patch.object(db, "cancel_paper_proposal", return_value=True) as cancel_proposal:
                            try:
                                await execute(policy, signal, p.proposal_id, early_intent=object())
                            except AggregatorSimulationError:
                                emitted.append(True)
                            except Exception as error:
                                emitted.append((type(error).__name__, str(error)))
                            finally:
                                completed.set()
                            cancel_proposal.assert_called_once()
                            self.assertEqual(cancel_prepared.call_count, int(stage == "sign"))
                            self.assertEqual(cancel_signed.call_count, int(stage == "review"))
                    return handoff

                class Socket:
                    async def __aenter__(self): return self
                    async def __aexit__(self, *args): pass
                    def __aiter__(self): return self.frames()
                    async def frames(self):
                        yield "frame"
                        await asyncio.Event().wait()

                args = cli.parser().parse_args(["run", "--seconds", "0.05", "--early-trial-id", "trial"])
                scanner = MagicMock(scan_once=AsyncMock(return_value=SimpleNamespace(initialized=False)))
                health = MagicMock(gap=False)
                health.healthy.return_value = True
                patches = {"load_endpoint_env": MagicMock(), "runtime_paper_config": MagicMock(return_value=config),
                    "runtime_store": MagicMock(return_value=db), "ReadOnlyRpc": MagicMock(return_value=rpc),
                    "monitoring_watchlist": MagicMock(return_value={wallet: {}}), "validate_live_relationships": MagicMock(),
                    "MySqlRelationshipGate": MagicMock(), "MainnetBroadcaster": MagicMock(return_value=broadcaster),
                    "ExecutionPreparer": MagicMock(return_value=preparer), "LiveExecutionSigner": MagicMock(return_value=signer),
                    "LivePreBroadcastReviewer": MagicMock(return_value=reviewer), "FeedHealth": MagicMock(return_value=health),
                    "BlockScanner": MagicMock(return_value=scanner), "Decoder": MagicMock(return_value=MagicMock(
                        delegations={}, decode=MagicMock(return_value=[]))),
                    "envelopes": MagicMock(return_value=[(b"raw", {"fresh": True})]), "decode_raw": MagicMock(return_value=tx),
                    "EarlyEvidenceResolver": MagicMock(return_value=AsyncMock(return_value={"candidates": []})),
                    "EarlyRuntime": MagicMock(side_effect=runtime_factory), "check_early_execution_source": MagicMock(),
                    "report": MagicMock()}
                with ExitStack() as stack:
                    for name, value in patches.items():
                        stack.enter_context(patch.object(cli, name, value))
                    stack.enter_context(patch("smart_money.execution_controls._stop_controls"))
                    stack.enter_context(patch.object(cli.websockets, "connect", return_value=Socket()))
                    await asyncio.wait_for(cli.monitor(args), 2)
                self.assertTrue(completed.is_set())
                self.assertEqual(emitted, [True])
                events = [c.kwargs for c in patches["report"].call_args_list if c.args == ("live_execution_abandoned",)]
                self.assertEqual(len(events), 1)
                event = events[0]
                self.assertEqual(event["stage"], stage)
                self.assertTrue(event["proposal_released"])
                self.assertEqual(event["early_trial_id"], "trial")
                self.assertEqual(event["source_tx_hash"], tx.hash)
                self.assertEqual(event["source_event_id"], "attributed-original-event")
                self.assertEqual(event["simulation_failure"]["rpc_error"]["revert_reason"], "STF")
                self.assertNotIn("synthetic-signed-bytes", json.dumps(event))
                broadcaster.broadcast.assert_not_awaited()
                if stage == "prepare": signer.sign.assert_not_awaited()
                if stage != "review": reviewer.review.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
